"""One-way Telegram notifications (no interactive bot)."""
from __future__ import annotations

import logging
import re
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)


def telegram_enabled(cfg: dict[str, Any]) -> bool:
    return bool(
        cfg.get("enabled")
        and str(cfg.get("bot_token", "")).strip()
        and str(cfg.get("chat_id", "")).strip()
    )


def _chat_id(value: Any) -> str | int:
    text = str(value).strip()
    return int(text) if re.fullmatch(r"-?\d+", text) else text


def notify_telegram(cfg: dict[str, Any], text: str, timeout: float) -> None:
    if not telegram_enabled(cfg):
        return
    token = str(cfg.get("bot_token", "")).strip()
    chat_id = _chat_id(cfg.get("chat_id", ""))
    body = "".join(ch for ch in str(text) if ch in "\n\t" or ord(ch) >= 32).strip() or "."
    if len(body) > 4096:
        body = body[:4088] + "\n...(cut)"
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": body,
                "disable_web_page_preview": True,
            },
            timeout=timeout,
        )
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description") or "sendMessage failed")
    except Exception as exc:
        LOGGER.warning("Telegram notify failed: %s", exc)
