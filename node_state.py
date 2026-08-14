"""Persistent per-inbound update policies (auto / pause / locked)."""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)
_LOCK = threading.RLock()

MODE_AUTO = "auto"
MODE_PAUSE = "pause"
MODE_LOCKED = "locked"
VALID_MODES = {MODE_AUTO, MODE_PAUSE, MODE_LOCKED}

DEFAULT_STATE: dict[str, Any] = {"schema_version": 1, "inbounds": {}}


def default_policy() -> dict[str, Any]:
    return {"mode": MODE_AUTO, "locked_address": None}


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
        return {"schema_version": 1, "inbounds": cleaned}


def save_node_state(path: Path, state: dict[str, Any]) -> None:
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "inbounds": state.get("inbounds", {}),
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)


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


def mode_label(mode: str) -> str:
    return {
        MODE_AUTO: "🔄 自动更新",
        MODE_PAUSE: "⏸ 暂停更新",
        MODE_LOCKED: "🔒 已锁定",
    }.get(mode, mode)
