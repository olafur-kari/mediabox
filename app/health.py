import asyncio
import time
from typing import Dict, Any

import httpx

from app.m3u import get_cached_channels

# In-memory health cache: {url: {health, ms, checked_at}}
_health_cache: Dict[str, Dict[str, Any]] = {}

_check_running = False


def get_health_cache() -> Dict[str, Dict[str, Any]]:
    return _health_cache


async def check_url(client: httpx.AsyncClient, url: str) -> None:
    start = time.monotonic()
    try:
        resp = await client.head(url, timeout=5, follow_redirects=True)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        if elapsed_ms < 2000:
            health = "green"
        elif elapsed_ms < 5000:
            health = "yellow"
        else:
            health = "red"
        _health_cache[url] = {
            "health": health,
            "ms": elapsed_ms,
            "checked_at": time.time(),
            "status_code": resp.status_code,
        }
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        _health_cache[url] = {
            "health": "red",
            "ms": None,
            "checked_at": time.time(),
            "error": str(e),
        }


async def run_health_check() -> None:
    global _check_running
    if _check_running:
        return
    _check_running = True
    try:
        channels = get_cached_channels()
        urls = set()
        for ch in channels:
            for stream in ch.get("streams", []):
                url = stream.get("url")
                if url:
                    urls.add(url)

        if not urls:
            return

        async with httpx.AsyncClient(timeout=5) as client:
            tasks = [check_url(client, url) for url in urls]
            await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        _check_running = False


async def health_check_loop() -> None:
    """Run health checks every 5 minutes indefinitely."""
    while True:
        await run_health_check()
        await asyncio.sleep(300)  # 5 minutes


def get_stream_health(url: str) -> str:
    entry = _health_cache.get(url)
    if not entry:
        return "unknown"
    return entry.get("health", "unknown")


def enrich_channels_with_health(channels: list) -> list:
    """Return a copy of channels with health data filled in from cache."""
    result = []
    for ch in channels:
        ch_copy = dict(ch)
        streams_copy = []
        for stream in ch.get("streams", []):
            s = dict(stream)
            url = s.get("url", "")
            entry = _health_cache.get(url, {})
            s["health"] = entry.get("health", "unknown")
            s["ms"] = entry.get("ms")
            streams_copy.append(s)
        ch_copy["streams"] = streams_copy
        result.append(ch_copy)
    return result
