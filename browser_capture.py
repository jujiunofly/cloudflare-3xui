"""Discover the page's JSON/XHR endpoints with Playwright."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from playwright.async_api import Response, async_playwright

from parser import CARRIERS, _carrier, has_required_data, parse_payload

LOGGER = logging.getLogger(__name__)
PAGE_URL = "https://api.uouin.com/cloudflare.html"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def _safe_headers(headers: dict[str, str]) -> dict[str, str]:
    allowed = {"accept", "accept-language", "content-type", "referer", "origin", "x-requested-with"}
    return {key: value for key, value in headers.items() if key.lower() in allowed}


def _request_info(response: Response, carrier: str | None) -> dict[str, Any]:
    request = response.request
    split = urlsplit(response.url)
    # Preserve the query in URL as some sites sign or route requests with it.
    return {
        "url": urlunsplit((split.scheme, split.netloc, split.path, split.query, "")),
        "method": request.method,
        "headers": _safe_headers(dict(request.headers)),
        "body": request.post_data or None,
        "params": None,
        "carrier": carrier,
    }


async def discover(config_path: Path, wait_ms: int = 12_000, timeout_ms: int = 30_000, persist: bool = False) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    urls_seen: set[str] = set()
    inspection_tasks: set[asyncio.Task[None]] = set()

    async def inspect(response: Response) -> None:
        if response.request.resource_type not in {"fetch", "xhr"}:
            return
        try:
            body = await response.text()
        except Exception as exc:  # response bodies may be unavailable for failed CORS requests
            LOGGER.debug("无法读取响应 %s: %s", response.url, exc)
            return
        request_hint = f"{response.url} {response.request.post_data or ''}"
        hinted_carrier = next((name for name in CARRIERS if name in request_hint), None)
        # parser also understands English ISP aliases used in many endpoint URLs.
        if not hinted_carrier:
            hinted_carrier = _carrier(request_hint)
        parsed = parse_payload(body, hinted_carrier)
        if not has_required_data(parsed):
            return
        carriers = [name for name in CARRIERS if parsed[name]]
        key = response.url
        if key in urls_seen:
            return
        urls_seen.add(key)
        carrier = carriers[0] if len(carriers) == 1 else hinted_carrier
        info = _request_info(response, carrier)
        candidates.append(info)
        LOGGER.info("发现可疑 API: %s (线路: %s)", response.url, "、".join(carriers))

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=USER_AGENT,
            locale="zh-CN",
            extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"},
        )
        page = await context.new_page()

        def on_response(response: Response) -> None:
            task = asyncio.create_task(inspect(response))
            inspection_tasks.add(task)
            task.add_done_callback(inspection_tasks.discard)

        page.on("response", on_response)
        try:
            await page.goto(PAGE_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            await page.wait_for_timeout(wait_ms)
            if inspection_tasks:
                await asyncio.gather(*inspection_tasks, return_exceptions=True)
        finally:
            await context.close()
            await browser.close()

    if not candidates:
        raise RuntimeError("未发现含 Cloudflare IP 的 Fetch/XHR 接口；可用 --wait-ms 增加等待时间后重试")
    if persist:
        # Kept for one-off diagnostic use.  The synchroniser deliberately
        # passes persist=False because this endpoint changes frequently.
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            config = {"schema_version": 2}
        config["apis"] = candidates
        config["captured_at"] = datetime.now(timezone.utc).isoformat()
        config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return candidates


def discover_sync(config_path: Path, wait_ms: int, timeout_ms: int, persist: bool = False) -> list[dict[str, Any]]:
    return asyncio.run(discover(config_path, wait_ms, timeout_ms, persist))
