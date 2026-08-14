"""Minimal Telegram helpers."""
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


def _text(value: str, limit: int = 4096) -> str:
    cleaned = "".join(ch for ch in str(value) if ch in "\n\t" or ord(ch) >= 32).strip()
    if not cleaned:
        return "."
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 8] + "\n...(cut)"


def api(token: str, method: str, payload: dict[str, Any] | None = None, timeout: float = 30) -> Any:
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=payload or {},
            timeout=timeout,
        )
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise RuntimeError(f"Telegram {method}: {exc}") from exc
    if not body.get("ok"):
        raise RuntimeError(body.get("description") or f"Telegram {method} failed")
    return body.get("result")


def notify_telegram(cfg: dict[str, Any], text: str, timeout: float) -> None:
    if not telegram_enabled(cfg):
        return
    try:
        send(cfg["bot_token"], cfg["chat_id"], text, timeout=timeout)
    except Exception as exc:
        LOGGER.warning("Telegram notify failed: %s", exc)


def send(token: str, chat_id: Any, text: str, timeout: float = 20, reply_markup: dict | None = None) -> Any:
    payload: dict[str, Any] = {
        "chat_id": _chat_id(chat_id),
        "text": _text(text),
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return api(token, "sendMessage", payload, timeout=timeout)


def edit(token: str, chat_id: Any, message_id: int, text: str, reply_markup: dict | None = None) -> Any:
    payload: dict[str, Any] = {
        "chat_id": _chat_id(chat_id),
        "message_id": int(message_id),
        "text": _text(text),
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        return api(token, "editMessageText", payload)
    except RuntimeError as exc:
        msg = str(exc).lower()
        if "message is not modified" in msg:
            return None
        # Fall back to a new message.
        return send(token, chat_id, text, reply_markup=reply_markup)


def answer(token: str, callback_id: str, text: str = "") -> None:
    payload: dict[str, Any] = {"callback_query_id": callback_id}
    if text:
        payload["text"] = _text(text, 180)
    try:
        api(token, "answerCallbackQuery", payload, timeout=10)
    except Exception as exc:
        LOGGER.debug("answerCallbackQuery: %s", exc)


def get_updates(token: str, offset: int | None, timeout: int = 25) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        payload["offset"] = offset
    result = api(token, "getUpdates", payload, timeout=timeout + 10)
    return result if isinstance(result, list) else []


def setup_bot(token: str) -> None:
    try:
        api(token, "deleteWebhook", {"drop_pending_updates": False})
        api(token, "deleteMyCommands", {})
        api(
            token,
            "setMyCommands",
            {
                "commands": [
                    {"command": "start", "description": "菜单"},
                    {"command": "nodes", "description": "节点列表"},
                    {"command": "notify", "description": "通知开关"},
                ]
            },
        )
    except Exception as exc:
        LOGGER.warning("Telegram setup: %s", exc)
