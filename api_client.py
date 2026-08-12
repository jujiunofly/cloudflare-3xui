"""Replay previously captured public API requests with requests."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import requests

from parser import CARRIERS, has_required_data, merge_results, parse_payload

LOGGER = logging.getLogger(__name__)
DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}


class ApiError(RuntimeError):
    pass


def load_config(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ApiError(f"无法读取配置 {path}: {exc}") from exc


def _response_payload(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def fetch_apis(config: dict[str, Any], timeout: tuple[float, float], retries: int) -> dict[str, list[dict[str, str]]]:
    apis = config.get("apis", [])
    if not apis:
        raise ApiError("尚未发现 API。请先运行: python main.py --discover")

    session = requests.Session()
    collected = []
    for api in apis:
        url = api.get("url")
        if not url:
            continue
        headers = DEFAULT_HEADERS | api.get("headers", {})
        method = api.get("method", "GET").upper()
        for attempt in range(1, retries + 1):
            try:
                response = session.request(method, url, headers=headers, params=api.get("params"), data=api.get("body"), timeout=timeout)
                response.raise_for_status()
                parsed = parse_payload(_response_payload(response), api.get("carrier"))
                if has_required_data(parsed):
                    LOGGER.info("API 成功: %s", url)
                    collected.append(parsed)
                    break
                raise ApiError(f"响应未包含可识别 IP 数据: {url}")
            except (requests.RequestException, ApiError) as exc:
                LOGGER.warning("API 请求失败 (%d/%d): %s; %s", attempt, retries, url, exc)
                if attempt == retries:
                    break
                time.sleep(min(2 ** (attempt - 1), 8))
    output = merge_results(collected)
    if not any(output[carrier] for carrier in CARRIERS):
        raise ApiError("所有已保存 API 均未返回可用 Cloudflare IP 数据")
    return output
