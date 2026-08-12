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
from browser_capture import discover_sync
from notifier import notify_telegram
from panel_client import PanelClient, PanelError, update_matching_inbounds

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
OUTPUT_PATH = BASE_DIR / "cloudflare_ip.json"
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
    updated = [item for item in changes if item.get("changed") and not item.get("fallback")]
    unchanged = [item for item in changes if not item.get("changed")]
    fallback = [item for item in changes if item.get("fallback")]
    address_lines = "\n".join(f"  • {line}: {address}" for line, address in addresses.items())
    change_lines = "\n".join(f"  • #{item['id']} {item['remark']} → {item['address']}" for item in updated + fallback) or "  • 无需变更"
    return (
        "🛰️ Cloudflare → 3x-ui 🫛泡豆🫛同步完成\n"
        "━━━━━━━━━━━━━━━━\n"
        "📡 本轮优选 IP\n"
        f"{address_lines}\n"
        "🛠️ 入站处理\n"
        f"{change_lines}\n"
        f"📊 更新 {len(updated)} 条｜保持 {len(unchanged)} 条｜单条回退 {len(fallback)} 条\n"
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


def panel_client(config: dict[str, Any], timeout: float) -> PanelClient:
    panel = config.get("panel", {})
    base_url = str(panel.get("base_url", "")).strip()
    token = str(panel.get("api_token", "")).strip()
    if not base_url or not token or "example" in base_url:
        raise PanelError("configure panel.base_url and panel.api_token in config.json")
    # The panel can be slower than the lightweight Cloudflare data API.
    # These optional settings preserve backward compatibility with old config.
    panel_timeout = float(panel.get("timeout_seconds", 45))
    retries = int(panel.get("retries", 3))
    return PanelClient(base_url, token, panel_timeout, retries)


def sync_once(config: dict[str, Any], stats: DailyStats, now: datetime) -> str:
    runtime = config["runtime"]
    timeout = float(runtime["request_timeout_seconds"])
    client: PanelClient | None = None
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
        changes = update_matching_inbounds(client, addresses, fallback)
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", help="run one synchronisation cycle, then exit")
    args = parser.parse_args()
    setup_logging()
    default_delay = 10 * 60
    stats = DailyStats()
    while True:
        succeeded = True
        delay = default_delay
        now = datetime.now()
        try:
            config = load_config(CONFIG_PATH)
            if in_run_window(config, now):
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
        except Exception as exc:
            succeeded = False  # errors have already been logged and optionally notified
            logging.error("Cycle ended with an error; the process will stay alive: %s", exc)
        if args.once:
            return 0 if succeeded else 1
        logging.info("Next cycle in %.0f seconds", delay)
        time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
