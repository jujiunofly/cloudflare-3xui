"""Persistent node policies, notify flags, and schedule/runtime overrides."""
from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)
_LOCK = threading.RLock()

MODE_AUTO = "auto"
MODE_PAUSE = "pause"
MODE_LOCKED = "locked"
VALID_MODES = {MODE_AUTO, MODE_PAUSE, MODE_LOCKED}

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

HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

DEFAULT_STATE: dict[str, Any] = {
    "schema_version": 1,
    "inbounds": {},
    "telegram": {},
    "schedule": {},
    "runtime": {},
}


def default_policy() -> dict[str, Any]:
    return {"mode": MODE_AUTO, "locked_address": None}


def _clean_telegram(raw: Any) -> dict[str, bool]:
    if not isinstance(raw, dict):
        return {}
    return {key: bool(raw[key]) for key in NOTIFY_DEFAULTS if key in raw}


def _clean_schedule(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    if "enabled" in raw:
        out["enabled"] = bool(raw["enabled"])
    for key in ("start", "end"):
        if key in raw and raw[key] is not None:
            text = str(raw[key]).strip()
            if HHMM_RE.match(text):
                h, m = text.split(":")
                out[key] = f"{int(h):02d}:{int(m):02d}"
    return out


def _clean_runtime(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    if "interval_minutes" in raw:
        try:
            val = float(raw["interval_minutes"])
            if 1 <= val <= 24 * 60:
                out["interval_minutes"] = val
        except (TypeError, ValueError):
            pass
    if "jitter_seconds" in raw:
        try:
            val = float(raw["jitter_seconds"])
            if 0 <= val <= 3600:
                out["jitter_seconds"] = val
        except (TypeError, ValueError):
            pass
    return out


def load_node_state(path: Path) -> dict[str, Any]:
    with _LOCK:
        if not path.exists():
            return copy.deepcopy(DEFAULT_STATE)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("Failed to read node state %s: %s; using defaults", path, exc)
            return copy.deepcopy(DEFAULT_STATE)
        if not isinstance(data, dict):
            return copy.deepcopy(DEFAULT_STATE)
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
            "schedule": _clean_schedule(data.get("schedule")),
            "runtime": _clean_runtime(data.get("runtime")),
        }


def save_node_state(path: Path, state: dict[str, Any]) -> None:
    """Persist state with in-place write (Docker single-file bind mounts)."""
    with _LOCK:
        if path.exists() and path.is_dir():
            raise OSError(
                f"{path} is a directory. On host: rm -rf node_state.json; "
                "echo '{\"schema_version\":1,\"inbounds\":{},\"telegram\":{},"
                "\"schedule\":{},\"runtime\":{}}' > node_state.json"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "inbounds": state.get("inbounds", {}),
            "telegram": _clean_telegram(state.get("telegram")),
            "schedule": _clean_schedule(state.get("schedule")),
            "runtime": _clean_runtime(state.get("runtime")),
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
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


def set_schedule_override(path: Path, **fields: Any) -> dict[str, Any]:
    cleaned = _clean_schedule(fields)
    if not cleaned and fields:
        # allow explicit enabled=False only etc.; empty cleaned with junk raises
        if "enabled" in fields:
            cleaned["enabled"] = bool(fields["enabled"])
        for key in ("start", "end"):
            if key in fields:
                text = str(fields[key]).strip()
                if not HHMM_RE.match(text):
                    raise ValueError(f"{key} 格式须为 HH:MM，例如 08:00")
                h, m = text.split(":")
                cleaned[key] = f"{int(h):02d}:{int(m):02d}"
    with _LOCK:
        state = load_node_state(path)
        schedule = dict(state.get("schedule") or {})
        schedule.update(cleaned)
        state["schedule"] = schedule
        save_node_state(path, state)
        return dict(state["schedule"])


def set_runtime_override(path: Path, **fields: Any) -> dict[str, Any]:
    cleaned = _clean_runtime(fields)
    if "interval_minutes" in fields and "interval_minutes" not in cleaned:
        raise ValueError("间隔分钟须为 1～1440 的数字")
    if "jitter_seconds" in fields and "jitter_seconds" not in cleaned:
        raise ValueError("抖动秒数须为 0～3600 的数字")
    with _LOCK:
        state = load_node_state(path)
        runtime = dict(state.get("runtime") or {})
        runtime.update(cleaned)
        state["runtime"] = runtime
        save_node_state(path, state)
        return dict(state["runtime"])


def clear_schedule_runtime_overrides(path: Path) -> None:
    with _LOCK:
        state = load_node_state(path)
        state["schedule"] = {}
        state["runtime"] = {}
        save_node_state(path, state)


def merge_telegram_settings(config_telegram: dict[str, Any], state: dict[str, Any] | None = None) -> dict[str, Any]:
    merged = dict(config_telegram or {})
    for key, default in NOTIFY_DEFAULTS.items():
        if key not in merged:
            merged[key] = default
    merged.update(_clean_telegram((state or {}).get("telegram")))
    return merged


def effective_telegram(config: dict[str, Any], state_path: Path) -> dict[str, Any]:
    state = load_node_state(state_path)
    return merge_telegram_settings(config.get("telegram", {}), state)


def effective_config(config: dict[str, Any], state_path: Path) -> dict[str, Any]:
    """Base config.json merged with node_state schedule/runtime overrides."""
    state = load_node_state(state_path)
    merged = copy.deepcopy(config)
    if state.get("schedule"):
        merged.setdefault("schedule", {}).update(state["schedule"])
    if state.get("runtime"):
        merged.setdefault("runtime", {}).update(state["runtime"])
    return merged


def mode_label(mode: str) -> str:
    return {
        MODE_AUTO: "🔄 自动更新",
        MODE_PAUSE: "⏸ 暂停更新",
        MODE_LOCKED: "🔒 已锁定",
    }.get(mode, mode)


def notify_flag_label(key: str, enabled: bool) -> str:
    name = NOTIFY_LABELS.get(key, key)
    return f"{'✅' if enabled else '❌'} {name}"


def parse_hhmm(text: str) -> str:
    text = text.strip()
    if not HHMM_RE.match(text):
        raise ValueError("时间格式须为 HH:MM，例如 08:00 或 23:30")
    h, m = text.split(":")
    return f"{int(h):02d}:{int(m):02d}"
