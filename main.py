#!/usr/bin/env python3
"""Discover Cloudflare IPs and synchronise matching 3x-ui inbound shareAddr values."""
from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from api_client import fetch_apis, load_config
from bot import TelegramBot
from browser_capture import discover_sync
from node_state import load_node_state
from notifier import notify_telegram, telegram_enabled
from panel_client import PanelClient, PanelError, update_matching_inbounds

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
OUTPUT_PATH = BASE_DIR / "cloudflare_ip.json"
NODE_STATE_PATH = BASE_DIR / "node_state.json"
LINES = ("电信", "联通", "移动", "多线")


class DailyStats:
    def __init__(self) -> None:
        self.day = datetime.now().date()
        self.successes = 0
        self.failures = 0
        self.attempts = 0

    def record(self, succeeded: bool) -> None:
        today = datetime.now().date()
        if today != self.day:
            self.day, self.successes, self.failures, self.attempts = today, 0, 0, 0
        self.attempts += 1
        if succeeded:
            self.successes += 1
        else:
            self.failures += 1


def in_run_window(config: dict[str, Any], now: datetime | None = None) -> bool:
    schedule = config.get("schedule", {})
    if not schedule.get("enabled", False):
        return True
    now = now or datetime.now()
    start = str(schedule.get("start", "00:00"))
    end = str(schedule.get("end", "23:59"))
    try:
        current = now.hour * 60 + now.minute
        start_minutes = int(start[:2]) * 60 + int(start[3:])
        end_minutes = int(end[:2]) * 60 + int(end[3:])
    except (ValueError, IndexError):
        raise RuntimeError("schedule.start/end must use HH:MM, e.g. 08:00")
    # Equal endpoints mean full-day; a range crossing midnight is supported.
    if start_minutes == end_minutes:
        return True
    return start_minutes <= current < end_minutes if start_minutes < end_minutes else current >= start_minutes or current < end_minutes


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def write_output(data: dict[str, Any]) -> None:
    OUTPUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def addresses_from_data(data: dict[str, list[dict[str, str]]]) -> dict[str, str]:
    # 电信/联通/移动 are always required; 多线 is optional unless a MIX remark is present.
    required = ("电信", "联通", "移动")
    missing = [line for line in required if not data.get(line)]
    if missing:
        raise RuntimeError(f"Cloudflare data is missing: {', '.join(missing)}")
    addresses = {line: data[line][0]["ip"] for line in required}
    if data.get("多线"):
        addresses["多线"] = data["多线"][0]["ip"]
    return addresses


def status_lines(stats: DailyStats, now: datetime) -> str:
    return f"🕒 更新时间：{now:%Y-%m-%d %H:%M:%S}\n📒 今日第 {stats.attempts} 次｜成功 {stats.successes}｜失败 {stats.failures}"


def success_message(addresses: dict[str, str], changes: list[dict[str, Any]], stats: DailyStats, now: datetime) -> str:
    updated = [item for item in changes if item.get("changed") and not item.get("fallback") and not item.get("skipped")]
    unchanged = [item for item in changes if not item.get("changed") and not item.get("skipped")]
    fallback = [item for item in changes if item.get("fallback")]
    paused = [item for item in changes if item.get("reason") == "pause"]
    locked = [item for item in changes if item.get("reason") == "locked"]
    address_lines = "\n".join(f"  • {line}: {address}" for line, address in addresses.items())
    change_lines = "\n".join(f"  • #{item['id']} {item['remark']} → {item['address']}" for item in updated + fallback) or "  • 无需变更"
    skip_lines = []
    if paused:
        skip_lines.append("⏸ 暂停：" + "、".join(f"#{item['id']}" for item in paused))
    if locked:
        skip_lines.append("🔒 锁定：" + "、".join(f"#{item['id']}" for item in locked))
    skip_block = ("\n" + "\n".join(skip_lines)) if skip_lines else ""
    return (
        "🛰️ Cloudflare → 3x-ui 🫛泡豆🫛同步完成\n"
        "━━━━━━━━━━━━━━━━\n"
        "📡 本轮优选 IP\n"
        f"{address_lines}\n"
        "🛠️ 入站处理\n"
        f"{change_lines}{skip_block}\n"
        f"📊 更新 {len(updated)} 条｜保持 {len(unchanged)} 条｜单条回退 {len(fallback)} 条"
        f"｜暂停 {len(paused)}｜锁定 {len(locked)}\n"
        f"{status_lines(stats, now)}\n"
        "✨ 下一班车将按配置时间加随机抖动抵达。"
    )


def failure_message(exc: Exception, stats: DailyStats, now: datetime) -> str:
    return (
        "🚨 Cloudflare → 3x-ui 本轮异常\n"
        "━━━━━━━━━━━━━━━━\n"
        f"⚠️ 原因：{exc}\n"
        "🛡️ 未受影响的入站保持原值；仅发生更新/校验失败的单条入站会尝试回退到默认地址。\n"
        f"{status_lines(stats, now)}\n"
        "🔁 程序会在下一轮自动重试。"
    )


def schedule_start_message(config: dict[str, Any], now: datetime) -> str:
    schedule = config.get("schedule", {})
    return (
        "🌅 工作时段开始\n"
        "━━━━━━━━━━━━━━━━\n"
        f"🕒 现在：{now:%Y-%m-%d %H:%M:%S}\n"
        f"📅 窗口：{schedule.get('start', '?')} → {schedule.get('end', '?')}\n"
        "🚀 同步任务已苏醒，开始抓取优选 IP。"
    )


def schedule_rest_message(config: dict[str, Any], now: datetime) -> str:
    schedule = config.get("schedule", {})
    return (
        "🌙 进入休息时段\n"
        "━━━━━━━━━━━━━━━━\n"
        f"🕒 现在：{now:%Y-%m-%d %H:%M:%S}\n"
        f"📅 窗口：{schedule.get('start', '?')} → {schedule.get('end', '?')}\n"
        "😴 本时段不再自动同步，节点地址保持现状。\n"
        "💬 仍可通过机器人查看节点 / 手动锁定。"
    )


def panel_client(config: dict[str, Any], timeout: float | None = None) -> PanelClient:
    panel = config.get("panel", {})
    base_url = str(panel.get("base_url", "")).strip()
    token = str(panel.get("api_token", "")).strip()
    if not base_url or not token or "example" in base_url:
        raise PanelError("configure panel.base_url and panel.api_token in config.json")
    # The panel can be slower than the lightweight Cloudflare data API.
    # These optional settings preserve backward compatibility with old config.
    panel_timeout = float(panel.get("timeout_seconds", timeout if timeout is not None else 45))
    retries = int(panel.get("retries", 3))
    return PanelClient(base_url, token, panel_timeout, retries)


def panel_client_from_config(config: dict[str, Any]) -> PanelClient:
    runtime = config.get("runtime", {})
    timeout = float(runtime.get("request_timeout_seconds", 20))
    return panel_client(config, timeout)


def maybe_notify_schedule_transition(
    config: dict[str, Any],
    now: datetime,
    active: bool,
    last_active: bool | None,
) -> bool:
    """Send start/rest notices on window edges. Returns the new last_active value."""
    schedule = config.get("schedule", {})
    if not schedule.get("enabled", False):
        return active
    if last_active is None:
        return active
    if last_active == active:
        return active
    tg = config.get("telegram", {})
    timeout = float(config.get("runtime", {}).get("request_timeout_seconds", 20))
    if active and tg.get("notify_on_start", True):
        notify_telegram(tg, schedule_start_message(config, now), timeout)
        logging.info("Sent schedule start notification")
    if not active and tg.get("notify_on_rest", True):
        notify_telegram(tg, schedule_rest_message(config, now), timeout)
        logging.info("Sent schedule rest notification")
    return active


def sync_once(config: dict[str, Any], stats: DailyStats, now: datetime) -> str:
    runtime = config["runtime"]
    timeout = float(runtime["request_timeout_seconds"])
    try:
        client = panel_client(config, timeout)
        # Default behaviour is discovery on every cycle: site data changes every 10 minutes.
        if runtime.get("discover_every_cycle", True):
            logging.info("Discovering current Fetch/XHR API with Playwright")
            candidates = discover_sync(
                CONFIG_PATH,
                int(runtime["browser_wait_ms"]),
                int(runtime["browser_timeout_ms"]),
                persist=False,
            )
            # API addresses are intentionally ephemeral: each cycle discovers
            # them again instead of saving a stale address to config.json.
            config = dict(config)
            config["apis"] = candidates
        data = fetch_apis(config, timeout=(timeout, timeout), retries=int(runtime["api_retries"]))
        addresses = addresses_from_data(data)
        write_output(data)
        fallback = str(runtime.get("fallback_share_addr", "")).strip() if runtime.get("fallback_on_failure", True) else None
        node_state = load_node_state(NODE_STATE_PATH)
        changes = update_matching_inbounds(
            client,
            addresses,
            fallback,
            node_policies=node_state.get("inbounds", {}),
        )
        message = success_message(addresses, changes, stats, now)
        if config["telegram"].get("notify_on_success", False):
            notify_telegram(config["telegram"], message, timeout)
        return message
    except Exception as exc:
        # The caller pre-records an attempted success so success notifications
        # can include the current run. Correct it before failure notification.
        stats.successes = max(0, stats.successes - 1)
        stats.failures += 1
        logging.exception("Sync failed: %s", exc)
        if config["telegram"].get("notify_on_failure", True):
            notify_telegram(config["telegram"], failure_message(exc, stats, now), timeout)
        raise


def build_status_text(stats: DailyStats, last_active: bool | None) -> str:
    now = datetime.now()
    try:
        config = load_config(CONFIG_PATH)
    except Exception as exc:
        return f"❌ 读取配置失败：{exc}"
    schedule = config.get("schedule", {})
    enabled = bool(schedule.get("enabled", False))
    active = in_run_window(config, now)
    if not enabled:
        phase = "全天运行（未启用时间窗）"
        window = "关闭（全天）"
    else:
        phase = "🟢 工作中" if active else "🌙 休息中"
        window = f"{schedule.get('start')} → {schedule.get('end')}"
    if last_active is None:
        memory = "尚未观察"
    else:
        memory = "工作" if last_active else "休息"
    return (
        "📟 运行状态\n"
        "━━━━━━━━━━━━━━━━\n"
        f"当前：{phase}\n"
        f"时间：{now:%Y-%m-%d %H:%M:%S}\n"
        f"窗口：{window}\n"
        f"今日：尝试 {stats.attempts}｜成功 {stats.successes}｜失败 {stats.failures}\n"
        f"状态记忆：{memory}\n"
        "提示：发送「节点列表」可管理是否参与更新 / 锁定 IP。"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run one synchronisation cycle, then exit")
    args = parser.parse_args()
    setup_logging()
    default_delay = 10 * 60
    stats = DailyStats()
    last_active: bool | None = None
    bot: TelegramBot | None = None

    def status_provider() -> str:
        return build_status_text(stats, last_active)

    try:
        boot_config = load_config(CONFIG_PATH)
        if telegram_enabled(boot_config.get("telegram", {})) and not args.once:
            bot = TelegramBot(
                CONFIG_PATH,
                NODE_STATE_PATH,
                panel_client_from_config,
                status_provider=status_provider,
            )
            bot.start()
    except Exception as exc:
        logging.warning("Telegram bot not started: %s", exc)

    while True:
        succeeded = True
        delay = default_delay
        now = datetime.now()
        try:
            config = load_config(CONFIG_PATH)
            active = in_run_window(config, now)
            last_active = maybe_notify_schedule_transition(config, now, active, last_active)
            if active:
                # Record before notifications, so the message includes this run.
                try:
                    stats.record(True)
                    message = sync_once(config, stats, now)
                    logging.info(message)
                except Exception:
                    raise
            else:
                logging.info("Outside configured run window; skipping this cycle")
            runtime = config["runtime"]
            interval = float(runtime["interval_minutes"]) * 60
            jitter = float(runtime["jitter_seconds"])
            delay = max(1, interval + random.uniform(-jitter, jitter))
            # During rest, wake more often so start/rest edges are not delayed
            # by a full sync interval (still not second-level precision).
            if not active and config.get("schedule", {}).get("enabled", False):
                delay = min(delay, 60.0)
        except Exception as exc:
            succeeded = False  # errors have already been logged and optionally notified
            logging.error("Cycle ended with an error; the process will stay alive: %s", exc)
        if args.once:
            if bot:
                bot.stop()
            return 0 if succeeded else 1
        logging.info("Next cycle in %.0f seconds", delay)
        time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
