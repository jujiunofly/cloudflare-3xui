#!/usr/bin/env python3
"""Discover Cloudflare IPs and sync matching 3x-ui shareAddr values."""
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
from browser_capture import discover_sync
from node_state import effective_telegram, load_node_state
from notifier import notify_telegram
from panel_client import PanelClient, PanelError, update_matching_inbounds

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
OUTPUT_PATH = BASE_DIR / "cloudflare_ip.json"
NODE_STATE_PATH = BASE_DIR / "node_state.json"


class DailyStats:
    def __init__(self) -> None:
        self.day = datetime.now().date()
        self.successes = 0
        self.failures = 0
        self.attempts = 0

    def record(self, ok: bool) -> None:
        today = datetime.now().date()
        if today != self.day:
            self.day, self.successes, self.failures, self.attempts = today, 0, 0, 0
        self.attempts += 1
        if ok:
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
        cur = now.hour * 60 + now.minute
        s = int(start[:2]) * 60 + int(start[3:])
        e = int(end[:2]) * 60 + int(end[3:])
    except (ValueError, IndexError) as exc:
        raise RuntimeError("schedule.start/end must be HH:MM") from exc
    if s == e:
        return True
    return s <= cur < e if s < e else cur >= s or cur < e


def setup_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def write_output(data: dict[str, Any]) -> None:
    OUTPUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def addresses_from_data(data: dict[str, list[dict[str, str]]]) -> dict[str, str]:
    required = ("电信", "联通", "移动")
    missing = [line for line in required if not data.get(line)]
    if missing:
        raise RuntimeError(f"Cloudflare data missing: {', '.join(missing)}")
    out = {line: data[line][0]["ip"] for line in required}
    if data.get("多线"):
        out["多线"] = data["多线"][0]["ip"]
    return out


def panel_client(config: dict[str, Any]) -> PanelClient:
    panel = config.get("panel", {})
    base_url = str(panel.get("base_url", "")).strip()
    token = str(panel.get("api_token", "")).strip()
    if not base_url or not token or "example" in base_url:
        raise PanelError("configure panel.base_url and panel.api_token")
    return PanelClient(base_url, token, float(panel.get("timeout_seconds", 45)), int(panel.get("retries", 3)))


def tg_of(config: dict[str, Any]) -> dict[str, Any]:
    return effective_telegram(config, NODE_STATE_PATH)


def status_lines(stats: DailyStats, now: datetime) -> str:
    return (
        f"🕒 更新时间：{now:%Y-%m-%d %H:%M:%S}\n"
        f"📒 今日第 {stats.attempts} 次｜成功 {stats.successes}｜失败 {stats.failures}"
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
        "✨ 到点会再叫你开工。"
    )


def success_message(
    addresses: dict[str, str],
    changes: list[dict[str, Any]],
    stats: DailyStats,
    now: datetime,
) -> str:
    updated = [item for item in changes if item.get("changed") and not item.get("fallback") and not item.get("skipped")]
    unchanged = [item for item in changes if not item.get("changed") and not item.get("skipped")]
    fallback = [item for item in changes if item.get("fallback")]
    paused = [item for item in changes if item.get("reason") == "pause"]
    locked = [item for item in changes if item.get("reason") == "locked"]
    address_lines = "\n".join(f"  • {line}: {address}" for line, address in addresses.items())
    change_lines = (
        "\n".join(f"  • #{item['id']} {item['remark']} → {item['address']}" for item in updated + fallback)
        or "  • 无需变更"
    )
    skip_lines: list[str] = []
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


def maybe_schedule_notice(config: dict[str, Any], now: datetime, active: bool, last: bool | None) -> bool:
    if not config.get("schedule", {}).get("enabled", False) or last is None or last == active:
        return active
    tg = tg_of(config)
    timeout = float(config.get("runtime", {}).get("request_timeout_seconds", 20))
    if active and tg.get("notify_on_start", True):
        notify_telegram(tg, schedule_start_message(config, now), timeout)
    if not active and tg.get("notify_on_rest", True):
        notify_telegram(tg, schedule_rest_message(config, now), timeout)
    return active


def sync_once(config: dict[str, Any], stats: DailyStats, now: datetime) -> str:
    runtime = config["runtime"]
    timeout = float(runtime["request_timeout_seconds"])
    tg = tg_of(config)
    try:
        client = panel_client(config)
        work = dict(config)
        if runtime.get("discover_every_cycle", True):
            work["apis"] = discover_sync(
                CONFIG_PATH,
                int(runtime["browser_wait_ms"]),
                int(runtime["browser_timeout_ms"]),
                persist=False,
            )
        data = fetch_apis(work, timeout=(timeout, timeout), retries=int(runtime["api_retries"]))
        addresses = addresses_from_data(data)
        write_output(data)
        fallback = str(runtime.get("fallback_share_addr", "")).strip() if runtime.get("fallback_on_failure", True) else None
        state = load_node_state(NODE_STATE_PATH)
        changes = update_matching_inbounds(client, addresses, fallback, node_policies=state.get("inbounds", {}))
        message = success_message(addresses, changes, stats, now)
        if tg.get("notify_on_success", False):
            notify_telegram(tg, message, timeout)
        return message
    except Exception as exc:
        stats.successes = max(0, stats.successes - 1)
        stats.failures += 1
        logging.exception("Sync failed: %s", exc)
        if tg.get("notify_on_failure", True):
            notify_telegram(tg, failure_message(exc, stats, now), timeout)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    setup_logging()
    stats = DailyStats()
    last_active: bool | None = None

    while True:
        ok = True
        delay = 600.0
        now = datetime.now()
        try:
            config = load_config(CONFIG_PATH)
            active = in_run_window(config, now)
            last_active = maybe_schedule_notice(config, now, active, last_active)
            if active:
                stats.record(True)
                logging.info(sync_once(config, stats, now))
            else:
                logging.info("rest window, skip sync")
            runtime = config["runtime"]
            delay = max(1.0, float(runtime["interval_minutes"]) * 60 + random.uniform(
                -float(runtime["jitter_seconds"]), float(runtime["jitter_seconds"])
            ))
            if not active and config.get("schedule", {}).get("enabled"):
                delay = min(delay, 60.0)
        except Exception as exc:
            ok = False
            logging.error("cycle error (stay alive): %s", exc)
        if args.once:
            return 0 if ok else 1
        logging.info("next in %.0fs", delay)
        time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
