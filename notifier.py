"""Telegram send helpers (notifications + interactive bot API)."""
from __future__ import annotations

import logging
import re
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)
MAX_MESSAGE_LEN = 4096
MAX_CALLBACK_ANSWER = 180
MAX_BUTTON_TEXT = 64


class TelegramApiError(RuntimeError):
    def __init__(self, method: str, description: str, status_code: int | None = None):
        self.method = method
        self.description = description
        self.status_code = status_code
        super().__init__(f"Telegram {method} failed: {description}")


def telegram_enabled(config: dict[str, Any]) -> bool:
    if not config.get("enabled", False):
        return False
    return bool(str(config.get("bot_token", "")).strip() and str(config.get("chat_id", "")).strip())


def normalize_chat_id(chat_id: str | int) -> str | int:
    text = str(chat_id).strip()
    if re.fullmatch(r"-?\d+", text):
        try:
            return int(text)
        except ValueError:
            return text
    return text


def sanitize_text(text: str, limit: int = MAX_MESSAGE_LEN) -> str:
    """Strip control chars Telegram rejects; keep newlines/tabs; enforce length."""
    if text is None:
        return "."
    cleaned_chars: list[str] = []
    for char in str(text):
        code = ord(char)
        if char in "\n\t" or code >= 32:
            # Drop unpaired surrogates / non-characters that occasionally appear.
            if 0xD800 <= code <= 0xDFFF or code == 0xFFFE or code == 0xFFFF:
                continue
            cleaned_chars.append(char)
    cleaned = "".join(cleaned_chars).strip()
    if not cleaned:
        return "."
    if len(cleaned) > limit:
        suffix = "\n…(已截断)"
        cleaned = cleaned[: max(1, limit - len(suffix))] + suffix
    return cleaned


def sanitize_button_text(text: str) -> str:
    cleaned = sanitize_text(text, limit=MAX_BUTTON_TEXT)
    # Telegram button text limit is 64 characters.
    if len(cleaned) > MAX_BUTTON_TEXT:
        cleaned = cleaned[: MAX_BUTTON_TEXT - 1] + "…"
    return cleaned or "."


def _api(token: str, method: str, payload: dict[str, Any] | None = None, timeout: float = 20) -> Any:
    url = f"https://api.telegram.org/bot{token}/{method}"
    try:
        response = requests.post(url, json=payload or {}, timeout=timeout)
    except requests.RequestException as exc:
        raise TelegramApiError(method, f"network error: {exc}") from exc

    try:
        body = response.json()
    except ValueError as exc:
        raise TelegramApiError(method, f"non-JSON HTTP {response.status_code}: {response.text[:200]}", response.status_code) from exc

    if not isinstance(body, dict):
        raise TelegramApiError(method, f"unexpected body type HTTP {response.status_code}", response.status_code)

    if not body.get("ok"):
        description = str(body.get("description") or f"HTTP {response.status_code} ok=false")
        raise TelegramApiError(method, description, response.status_code or body.get("error_code"))

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
    chat_id = normalize_chat_id(config.get("chat_id", ""))
    try:
        send_telegram(token, chat_id, text, timeout=timeout, reply_markup=reply_markup)
    except (TelegramApiError, requests.RequestException, ValueError) as exc:
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
        "chat_id": normalize_chat_id(chat_id),
        "text": sanitize_text(text),
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        return _api(token, "sendMessage", payload, timeout=timeout)
    except TelegramApiError as exc:
        # Retry without markup if markup is the problem.
        desc = exc.description.lower()
        if reply_markup is not None and ("reply markup" in desc or "button" in desc or "can't parse" in desc):
            LOGGER.warning("sendMessage markup rejected (%s); retrying without markup", exc.description)
            payload.pop("reply_markup", None)
            return _api(token, "sendMessage", payload, timeout=timeout)
        raise


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
        "chat_id": normalize_chat_id(chat_id),
        "message_id": int(message_id),
        "text": sanitize_text(text),
        "disable_web_page_preview": True,
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    try:
        return _api(token, "editMessageText", payload, timeout=timeout)
    except TelegramApiError as exc:
        lower = exc.description.lower()
        # Tapping the same toggle twice, or refresh with identical content.
        if "message is not modified" in lower:
            return None
        # Fall back to a new message so the user still sees the result.
        if "message to edit not found" in lower or "message can't be edited" in lower:
            LOGGER.warning("editMessageText unavailable (%s); sending new message", exc.description)
            return send_telegram(token, chat_id, text, timeout=timeout, reply_markup=reply_markup)
        raise


def answer_callback(token: str, callback_query_id: str, text: str = "", timeout: float = 10) -> Any:
    payload: dict[str, Any] = {"callback_query_id": callback_query_id}
    cleaned = sanitize_text(text, limit=MAX_CALLBACK_ANSWER) if text else ""
    # Empty text key can cause Bad Request on some clients; omit when unused.
    if cleaned and cleaned != ".":
        payload["text"] = cleaned[:MAX_CALLBACK_ANSWER]
    try:
        return _api(token, "answerCallbackQuery", payload, timeout=timeout)
    except TelegramApiError as exc:
        # Query expired after slow panel calls — not fatal for the action itself.
        lower = exc.description.lower()
        if "query is too old" in lower or "query id is invalid" in lower:
            LOGGER.warning("answerCallbackQuery skipped: %s", exc.description)
            return None
        raise


def get_updates(token: str, offset: int | None, timeout: int, request_timeout: float) -> list[dict[str, Any]]:
    payload: dict[str, Any] = {
        "timeout": timeout,
        "allowed_updates": ["message", "callback_query"],
    }
    if offset is not None:
        payload["offset"] = offset
    result = _api(token, "getUpdates", payload, timeout=request_timeout)
    return result if isinstance(result, list) else []


def delete_webhook(token: str, *, drop_pending: bool = True, timeout: float = 20) -> None:
    """Ensure polling mode: webhooks and long-poll cannot both own the same bot."""
    _api(
        token,
        "deleteWebhook",
        {"drop_pending_updates": bool(drop_pending)},
        timeout=timeout,
    )


def set_bot_commands(token: str, commands: list[dict[str, str]], timeout: float = 20) -> None:
    try:
        # Clear stale command menus first (old bots / other apps may leave junk entries).
        _api(token, "deleteMyCommands", {}, timeout=timeout)
        _api(token, "setMyCommands", {"commands": commands}, timeout=timeout)
    except (TelegramApiError, requests.RequestException, ValueError) as exc:
        LOGGER.warning("setMyCommands failed: %s", exc)
