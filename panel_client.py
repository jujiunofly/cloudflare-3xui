"""Safely update 3x-ui inbound share addresses through its REST API."""
from __future__ import annotations

import copy
import logging
import time
from collections.abc import Iterable
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)

LINE_RULES = {"cucc": "联通", "cmcc": "移动", "ctcc": "电信", "mix": "多线"}


class PanelError(RuntimeError):
    pass


class PanelClient:
    def __init__(self, base_url: str, api_token: str, timeout: float, retries: int) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = max(1, retries)
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_token}",
            "Accept": "application/json",
        })

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.request(method, f"{self.base_url}{path}", timeout=self.timeout, **kwargs)
                response.raise_for_status()
                payload = response.json()
                if not payload.get("success"):
                    raise PanelError(f"3x-ui API rejected request: {payload.get('msg', 'unknown error')}")
                return payload.get("obj")
            except PanelError:
                raise
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt < self.retries:
                    delay = min(2 ** (attempt - 1), 8)
                    LOGGER.warning("3x-ui request %s failed (%d/%d): %s; retrying in %ss", path, attempt, self.retries, exc, delay)
                    time.sleep(delay)
        raise PanelError(f"3x-ui API request failed after {self.retries} attempts: {last_error}") from last_error

    def list_inbounds(self) -> list[dict[str, Any]]:
        result = self._request("GET", "/inbounds/list")
        if not isinstance(result, list):
            raise PanelError("3x-ui list response is not a list")
        return result

    def get_inbound(self, inbound_id: int) -> dict[str, Any]:
        result = self._request("GET", f"/inbounds/get/{inbound_id}")
        if not isinstance(result, dict):
            raise PanelError(f"3x-ui did not return inbound {inbound_id}")
        return result

    def update_share_address(self, inbound: dict[str, Any], address: str) -> bool:
        """Return True only if a configuration change was made."""
        inbound_id = int(inbound["id"])
        if inbound.get("shareAddrStrategy") == "custom" and inbound.get("shareAddr") == address:
            return False
        update = copy.deepcopy(inbound)
        update["shareAddrStrategy"] = "custom"
        update["shareAddr"] = address
        self._request("POST", f"/inbounds/update/{inbound_id}", json=update)
        verified = self.get_inbound(inbound_id)
        if verified.get("shareAddrStrategy") != "custom" or verified.get("shareAddr") != address:
            raise PanelError(f"3x-ui verification failed for inbound {inbound_id}")
        return True


def matching_inbounds(inbounds: Iterable[dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
    """Map remarks containing cucc/cmcc/ctcc/mix (case-insensitive) to a line."""
    matches: list[tuple[dict[str, Any], str]] = []
    for inbound in inbounds:
        remark = str(inbound.get("remark", "")).lower()
        line = next((line for marker, line in LINE_RULES.items() if marker in remark), None)
        if line:
            matches.append((inbound, line))
    return matches


def update_matching_inbounds(
    client: PanelClient,
    addresses: dict[str, str],
    fallback_address: str | None = None,
    node_policies: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Update matching inbounds.

    node_policies maps inbound id (str) -> {"mode": auto|pause|locked, "locked_address": ...}.
    pause/locked nodes are skipped by the automatic sync cycle.
    """
    from node_state import MODE_AUTO, MODE_LOCKED, MODE_PAUSE, get_policy

    state = {"inbounds": node_policies or {}}
    changes: list[dict[str, Any]] = []
    failures: list[str] = []
    matched = matching_inbounds(client.list_inbounds())
    if not matched:
        raise PanelError("no inbound remark contains cucc, cmcc, ctcc, or mix")

    for inbound, line in matched:
        policy = get_policy(state, inbound["id"])
        mode = policy.get("mode", MODE_AUTO)
        if mode == MODE_PAUSE:
            changes.append({
                "id": inbound["id"],
                "remark": inbound.get("remark", ""),
                "line": line,
                "address": inbound.get("shareAddr"),
                "changed": False,
                "skipped": True,
                "reason": "pause",
            })
            LOGGER.info("3x-ui inbound %s (%s) skipped (paused)", inbound["id"], inbound.get("remark", ""))
            continue
        if mode == MODE_LOCKED:
            changes.append({
                "id": inbound["id"],
                "remark": inbound.get("remark", ""),
                "line": line,
                "address": policy.get("locked_address") or inbound.get("shareAddr"),
                "changed": False,
                "skipped": True,
                "reason": "locked",
            })
            LOGGER.info("3x-ui inbound %s (%s) skipped (locked)", inbound["id"], inbound.get("remark", ""))
            continue

        address = addresses.get(line)
        if not address:
            raise PanelError(f"missing address for {line}")
        try:
            # /list returns the full inbound configuration, avoiding an extra
            # /get request before each update.
            changed = client.update_share_address(inbound, address)
            changes.append({"id": inbound["id"], "remark": inbound.get("remark", ""), "line": line, "address": address, "changed": changed})
            LOGGER.info("3x-ui inbound %s (%s) -> %s%s", inbound["id"], inbound.get("remark", ""), address, " [unchanged]" if not changed else "")
        except PanelError as exc:
            failure = f"{inbound['id']} ({inbound.get('remark', '')}): {exc}"
            LOGGER.error("3x-ui inbound update failed: %s", failure)
            # Do not change unrelated inbounds.  Recovery is deliberately
            # limited to the inbound whose preferred-IP update failed.
            if fallback_address:
                try:
                    changed = client.update_share_address(inbound, fallback_address)
                    changes.append({"id": inbound["id"], "remark": inbound.get("remark", ""), "line": line, "address": fallback_address, "changed": changed, "fallback": True})
                    LOGGER.warning("3x-ui inbound %s recovered with fallback %s", inbound["id"], fallback_address)
                    continue
                except PanelError as fallback_exc:
                    failure += f"; fallback failed: {fallback_exc}"
            failures.append(failure)
    if failures:
        raise PanelError("some inbounds failed: " + "; ".join(failures))
    return changes
