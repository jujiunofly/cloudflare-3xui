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
from bot import TelegramBot
from browser_capture import discover_sync
from node_state import effective_telegram, load_node_state
from notifier import notify_telegram, telegram_enabled
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


def maybe_schedule_notice(config: dict[str, Any], now: datetime, active: bool, last: bool | None) -> bool:
    if not config.get("schedule", {}).get("enabled", False) or last is None or last == active:
        return active
    tg = tg_of(config)
    timeout = float(config.get("runtime", {}).get("request_timeout_seconds", 20))
    start = config.get("schedule", {}).get("start", "?")
    end = config.get("schedule", {}).get("end", "?")
    if active and tg.get("notify_on_start", True):
        notify_telegram(tg, f"开始工作 {now:%H:%M}\n窗口 {start}-{end}", timeout)
    if not active and tg.get("notify_on_rest", True):
        notify_telegram(tg, f"进入休息 {now:%H:%M}\n窗口 {start}-{end}", timeout)
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
        updated = sum(1 for c in changes if c.get("changed") and not c.get("fallback") and not c.get("skipped"))
        skipped = sum(1 for c in changes if c.get("skipped"))
        msg = (
            f"同步完成 {now:%H:%M:%S}\n"
            + "\n".join(f"{k}: {v}" for k, v in addresses.items())
            + f"\n更新 {updated} / 跳过 {skipped} / 今日 {stats.successes}成功 {stats.failures}失败"
        )
        if tg.get("notify_on_success", False):
            notify_telegram(tg, msg, timeout)
        return msg
    except Exception as exc:
        stats.successes = max(0, stats.successes - 1)
        stats.failures += 1
        logging.exception("Sync failed: %s", exc)
        if tg.get("notify_on_failure", True):
            notify_telegram(tg, f"同步失败: {exc}\n今日 {stats.successes}成功 {stats.failures}失败", timeout)
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    setup_logging()
    stats = DailyStats()
    last_active: bool | None = None

    if not args.once:
        try:
            cfg = load_config(CONFIG_PATH)
            if telegram_enabled(cfg.get("telegram", {})):
                TelegramBot(CONFIG_PATH, NODE_STATE_PATH, panel_client).start()
        except Exception as exc:
            logging.warning("bot not started: %s", exc)

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
