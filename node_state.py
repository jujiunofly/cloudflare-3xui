"""Persistent per-inbound policies and Telegram notification overrides."""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)
_LOCK = threading.RLock()

MODE_AUTO = "auto"
MODE_PAUSE = "pause"
MODE_LOCKED = "locked"
VALID_MODES = {MODE_AUTO, MODE_PAUSE, MODE_LOCKED}

# Defaults used when neither config nor node_state sets a value.
NOTIFY_DEFAULTS: dict[str, bool] = {
    "notify_on_success": False,
    "notify_on_failure": True,
    "notify_on_start": True,
    "notify_on_rest": True,
}

NOTIFY_LABELS: dict[str, str] = {
    "notify_on_success": "成功消息",
    "notify_on_failure": "失败消息",
    "notify_on_start": "开始工作通知",
    "notify_on_rest": "进入休息通知",
}

DEFAULT_STATE: dict[str, Any] = {"schema_version": 1, "inbounds": {}, "telegram": {}}


def default_policy() -> dict[str, Any]:
    return {"mode": MODE_AUTO, "locked_address": None}


def _clean_telegram(raw: Any) -> dict[str, bool]:
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, bool] = {}
    for key in NOTIFY_DEFAULTS:
        if key in raw:
            cleaned[key] = bool(raw[key])
    return cleaned


def load_node_state(path: Path) -> dict[str, Any]:
    with _LOCK:
        if not path.exists():
            return json.loads(json.dumps(DEFAULT_STATE))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("Failed to read node state %s: %s; using defaults", path, exc)
            return json.loads(json.dumps(DEFAULT_STATE))
        if not isinstance(data, dict):
            return json.loads(json.dumps(DEFAULT_STATE))
        inbounds = data.get("inbounds")
        if not isinstance(inbounds, dict):
            inbounds = {}
        cleaned: dict[str, Any] = {}
        for key, value in inbounds.items():
            if not isinstance(value, dict):
                continue
            mode = str(value.get("mode", MODE_AUTO)).lower()
            if mode not in VALID_MODES:
                mode = MODE_AUTO
            locked = value.get("locked_address")
            cleaned[str(key)] = {
                "mode": mode,
                "locked_address": str(locked).strip() if locked else None,
            }
        return {
            "schema_version": 1,
            "inbounds": cleaned,
            "telegram": _clean_telegram(data.get("telegram")),
        }


def save_node_state(path: Path, state: dict[str, Any]) -> None:
    """Persist state.

    Docker often bind-mounts a single host file onto ``node_state.json``.
    Atomic rename (``*.tmp`` → target) then fails with
    ``[Errno 16] Device or resource busy``. Write in-place instead.
    """
    with _LOCK:
        if path.exists() and path.is_dir():
            raise OSError(
                f"{path} is a directory (Docker created a mount dir because the host file was missing). "
                "On the host run: rm -rf node_state.json; "
                "echo '{\"schema_version\":1,\"inbounds\":{},\"telegram\":{}}' > node_state.json"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "inbounds": state.get("inbounds", {}),
            "telegram": _clean_telegram(state.get("telegram")),
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        # In-place overwrite is required for bind-mounted files.
        with path.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass


def get_policy(state: dict[str, Any], inbound_id: int | str) -> dict[str, Any]:
    raw = (state.get("inbounds") or {}).get(str(inbound_id))
    if not isinstance(raw, dict):
        return default_policy()
    mode = str(raw.get("mode", MODE_AUTO)).lower()
    if mode not in VALID_MODES:
        mode = MODE_AUTO
    locked = raw.get("locked_address")
    return {
        "mode": mode,
        "locked_address": str(locked).strip() if locked else None,
    }


def set_policy(
    path: Path,
    inbound_id: int | str,
    *,
    mode: str,
    locked_address: str | None = None,
) -> dict[str, Any]:
    mode = mode.lower()
    if mode not in VALID_MODES:
        raise ValueError(f"invalid mode: {mode}")
    with _LOCK:
        state = load_node_state(path)
        state.setdefault("inbounds", {})[str(inbound_id)] = {
            "mode": mode,
            "locked_address": locked_address.strip() if locked_address else None,
        }
        save_node_state(path, state)
        return get_policy(state, inbound_id)


def set_notify_flag(path: Path, key: str, enabled: bool) -> dict[str, bool]:
    if key not in NOTIFY_DEFAULTS:
        raise ValueError(f"unknown notify key: {key}")
    with _LOCK:
        state = load_node_state(path)
        telegram = dict(state.get("telegram") or {})
        telegram[key] = bool(enabled)
        state["telegram"] = telegram
        save_node_state(path, state)
        return dict(state["telegram"])


def merge_telegram_settings(config_telegram: dict[str, Any], state: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return telegram settings: config base, overridden by node_state.telegram."""
    merged = dict(config_telegram or {})
    for key, default in NOTIFY_DEFAULTS.items():
        if key not in merged:
            merged[key] = default
    overrides = _clean_telegram((state or {}).get("telegram"))
    merged.update(overrides)
    return merged


def effective_telegram(config: dict[str, Any], state_path: Path) -> dict[str, Any]:
    state = load_node_state(state_path)
    return merge_telegram_settings(config.get("telegram", {}), state)


def mode_label(mode: str) -> str:
    return {
        MODE_AUTO: "🔄 自动更新",
        MODE_PAUSE: "⏸ 暂停更新",
        MODE_LOCKED: "🔒 已锁定",
    }.get(mode, mode)


def notify_flag_label(key: str, enabled: bool) -> str:
    name = NOTIFY_LABELS.get(key, key)
    return f"{'✅' if enabled else '❌'} {name}"
