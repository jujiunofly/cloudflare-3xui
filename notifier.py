"""Optional Telegram notifications."""
from __future__ import annotations

import logging
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)


def notify_telegram(config: dict[str, Any], text: str, timeout: float) -> None:
    if not config.get("enabled", False):
        return
    token = str(config.get("bot_token", "")).strip()
    chat_id = str(config.get("chat_id", "")).strip()
    if not token or not chat_id:
        LOGGER.warning("Telegram enabled but bot_token/chat_id is empty")
        return
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=timeout,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        LOGGER.warning("Telegram notification failed: %s", exc)
