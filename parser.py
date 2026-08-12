"""Parse API payloads returned by the Cloudflare IP page.

This module deliberately works on API response bodies only.  It never reads or
parses the page's HTML table.
"""
from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Iterable
from typing import Any

CARRIERS = ("电信", "联通", "移动", "多线")
_ALIASES = {
    "电信": ("电信", "telecom", "chinanet", "ct"),
    "联通": ("联通", "unicom", "cu"),
    "移动": ("移动", "mobile", "cmcc"),
    "多线": ("多线", "multi", "bgp"),
}
_IP_KEYS = ("ip", "host", "address", "node", "server", "ipv4")
_PING_KEYS = ("ping", "delay", "latency", "rtt", "ms")
_SPEED_KEYS = ("speed", "download", "throughput", "bandwidth", "mbps")


def empty_result() -> dict[str, list[dict[str, str]]]:
    return {carrier: [] for carrier in CARRIERS}


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _find_value(record: dict[str, Any], keys: Iterable[str]) -> str:
    lowered = {str(k).lower(): v for k, v in record.items()}
    for key in keys:
        if key in lowered and lowered[key] is not None:
            return _as_text(lowered[key])
    return ""


def _carrier(value: Any) -> str | None:
    text = _as_text(value).lower()
    for carrier, aliases in _ALIASES.items():
        if any(alias in text for alias in aliases):
            return carrier
    return None


def _valid_ip(value: str) -> str | None:
    # API values occasionally include a port; remove it only for IPv4.
    value = value.strip()
    if re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}:\d+", value):
        value = value.rsplit(":", 1)[0]
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return None
    return value


def _normalise_metric(value: str, unit: str) -> str:
    if not value:
        return ""
    value = value.strip()
    if re.search(r"(?:ms|mb/s|mbps|mib/s)", value, re.I):
        return value
    return f"{value} {unit}"


def _walk(value: Any, inherited_carrier: str | None = None) -> Iterable[tuple[dict[str, Any], str | None]]:
    if isinstance(value, dict):
        current = inherited_carrier
        for key in ("carrier", "line", "isp", "operator", "type", "name", "线路", "运营商"):
            if key in value:
                current = _carrier(value[key]) or current
        yield value, current
        for key, item in value.items():
            # Many APIs use the line name as a container key, e.g.
            # {"telecom": [{"ip": "..."}]}.  Pass that context down.
            yield from _walk(item, _carrier(key) or current)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item, inherited_carrier)


def parse_payload(payload: Any, default_carrier: str | None = None) -> dict[str, list[dict[str, str]]]:
    """Return the required carrier-to-IP format from JSON or plain-text APIs."""
    result = empty_result()
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return _parse_text(payload, default_carrier)

    seen: set[tuple[str, str]] = set()
    for record, inherited in _walk(payload, default_carrier):
        carrier = inherited
        if not carrier:
            carrier = _carrier(" ".join(_as_text(v) for v in record.values()))
        ip = _valid_ip(_find_value(record, _IP_KEYS))
        if not carrier or not ip or (carrier, ip) in seen:
            continue
        seen.add((carrier, ip))
        result[carrier].append({
            "ip": ip,
            "ping": _normalise_metric(_find_value(record, _PING_KEYS), "ms"),
            "speed": _normalise_metric(_find_value(record, _SPEED_KEYS), "mb/s"),
        })
    # A few public APIs return a bare list of IP strings rather than objects.
    # The captured URL supplies the carrier context in that case.
    if default_carrier and not has_required_data(result):
        return _parse_text(json.dumps(payload, ensure_ascii=False), default_carrier)
    return result


def _parse_text(text: str, default_carrier: str | None) -> dict[str, list[dict[str, str]]]:
    result = empty_result()
    carrier = default_carrier or _carrier(text)
    if not carrier:
        return result
    for ip in re.findall(r"(?<![\w:.])(?:\d{1,3}\.){3}\d{1,3}(?![\w:.])", text):
        valid = _valid_ip(ip)
        if valid:
            result[carrier].append({"ip": valid, "ping": "", "speed": ""})
    return result


def has_required_data(parsed: dict[str, list[dict[str, str]]]) -> bool:
    return any(parsed[carrier] for carrier in CARRIERS)


def merge_results(results: Iterable[dict[str, list[dict[str, str]]]]) -> dict[str, list[dict[str, str]]]:
    merged = empty_result()
    seen: set[tuple[str, str]] = set()
    for result in results:
        for carrier in CARRIERS:
            for item in result.get(carrier, []):
                key = (carrier, item["ip"])
                if key not in seen:
                    seen.add(key)
                    merged[carrier].append(item)
    return merged
