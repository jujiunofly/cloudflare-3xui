"""Telegram: one-way notify + interactive bot API helpers."""
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


def chat_id(value: Any) -> str | int:
    text = str(value).strip()
    return int(text) if re.fullmatch(r"-?\d+", text) else text


def clean_text(value: str, limit: int = 4096) -> str:
    text = "".join(ch for ch in str(value) if ch in "\n\t" or ord(ch) >= 32).strip()
    if not text:
        return "."
    if len(text) > limit:
        return text[: max(1, limit - 8)] + "\n...(cut)"
    return text


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
    if not isinstance(body, dict) or not body.get("ok"):
        desc = body.get("description") if isinstance(body, dict) else str(body)
        raise RuntimeError(desc or f"Telegram {method} failed")
    return body.get("result")


def notify_telegram(cfg: dict[str, Any], text: str, timeout: float) -> None:
    if not telegram_enabled(cfg):
        return
    try:
        send(str(cfg["bot_token"]).strip(), cfg["chat_id"], text, timeout=timeout)
    except Exception as exc:
        LOGGER.warning("Telegram notify failed: %s", exc)


def send(
    token: str,
    to: Any,
    text: str,
    timeout: float = 20,
    reply_markup: dict[str, Any] | None = None,
) -> Any:
    payload: dict[str, Any] = {
        "chat_id": chat_id(to),
        "text": clean_text(text),
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return api(token, "sendMessage", payload, timeout=timeout)


def edit(
    token: str,
    to: Any,
    message_id: int,
    text: str,
    reply_markup: dict[str, Any] | None = None,
) -> Any:
    payload: dict[str, Any] = {
        "chat_id": chat_id(to),
        "message_id": int(message_id),
        "text": clean_text(text),
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        return api(token, "editMessageText", payload)
    except RuntimeError as exc:
        low = str(exc).lower()
        if "message is not modified" in low:
            return None
        # Message may be too old / not editable — send a new one.
        return send(token, to, text, reply_markup=reply_markup)


def answer(token: str, callback_id: str, text: str = "") -> None:
    payload: dict[str, Any] = {"callback_query_id": callback_id}
    if text:
        payload["text"] = clean_text(text, 180)
    try:
        api(token, "answerCallbackQuery", payload, timeout=10)
    except Exception as exc:
        # Too-old query is fine after slow panel work.
        LOGGER.debug("answerCallbackQuery: %s", exc)


def get_updates(token: str, offset: int | None, timeout: int = 25) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {
        "timeout": timeout,
        "allowed_updates": ["message", "callback_query"],
    }
    if offset is not None:
        payload["offset"] = offset
    result = api(token, "getUpdates", payload, timeout=timeout + 15)
    return result if isinstance(result, list) else []


def setup_bot(token: str) -> None:
    """Prepare long-polling mode and command menu (once at startup)."""
    try:
        api(token, "deleteWebhook", {"drop_pending_updates": False})
        api(token, "deleteMyCommands", {})
        api(
            token,
            "setMyCommands",
            {
                "commands": [
                    {"command": "start", "description": "打开菜单"},
                    {"command": "nodes", "description": "节点列表"},
                    {"command": "notify", "description": "通知开关"},
                    {"command": "status", "description": "运行状态"},
                    {"command": "cancel", "description": "取消输入"},
                ]
            },
        )
    except Exception as exc:
        LOGGER.warning("Telegram setup_bot: %s", exc)
