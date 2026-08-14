"""Telegram send helpers (notifications + interactive bot API)."""
from __future__ import annotations

import logging
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)


def telegram_enabled(config: dict[str, Any]) -> bool:
    if not config.get("enabled", False):
        return False
    return bool(str(config.get("bot_token", "")).strip() and str(config.get("chat_id", "")).strip())


def _api(token: str, method: str, payload: dict[str, Any] | None = None, timeout: float = 20) -> Any:
    response = requests.post(
        f"https://api.telegram.org/bot{token}/{method}",
        json=payload or {},
        timeout=timeout,
    )
    response.raise_for_status()
    body = response.json()
    if not body.get("ok"):
        raise RuntimeError(body.get("description") or f"Telegram {method} failed")
    return body.get("result")


def notify_telegram(
    config: dict[str, Any],
    text: str,
    timeout: float,
    *,
    reply_markup: dict[str, Any] | None = None,
) -> None:
    if not telegram_enabled(config):
        return
    token = str(config.get("bot_token", "")).strip()
    chat_id = str(config.get("chat_id", "")).strip()
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        _api(token, "sendMessage", payload, timeout=timeout)
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        LOGGER.warning("Telegram notification failed: %s", exc)


def send_telegram(
    token: str,
    chat_id: str | int,
    text: str,
    timeout: float = 20,
    *,
    reply_markup: dict[str, Any] | None = None,
) -> Any:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return _api(token, "sendMessage", payload, timeout=timeout)


def edit_telegram(
    token: str,
    chat_id: str | int,
    message_id: int,
    text: str,
    timeout: float = 20,
    *,
    reply_markup: dict[str, Any] | None = None,
) -> Any:
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        return _api(token, "editMessageText", payload, timeout=timeout)
    except RuntimeError as exc:
        # Tapping the same toggle twice, or refresh with identical content.
        if "message is not modified" in str(exc).lower():
            return None
        raise


def answer_callback(token: str, callback_query_id: str, text: str = "", timeout: float = 20) -> Any:
    return _api(
        token,
        "answerCallbackQuery",
        {"callback_query_id": callback_query_id, "text": text[:180]},
        timeout=timeout,
    )


def get_updates(token: str, offset: int | None, timeout: int, request_timeout: float) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {
        "timeout": timeout,
        "allowed_updates": ["message", "callback_query"],
    }
    if offset is not None:
        payload["offset"] = offset
    result = _api(token, "getUpdates", payload, timeout=request_timeout)
    return result if isinstance(result, list) else []


def set_bot_commands(token: str, commands: list[dict[str, str]], timeout: float = 20) -> None:
    try:
        # Clear stale command menus first (old bots / other apps may leave junk entries).
        _api(token, "deleteMyCommands", {}, timeout=timeout)
        _api(token, "setMyCommands", {"commands": commands}, timeout=timeout)
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        LOGGER.warning("setMyCommands failed: %s", exc)
