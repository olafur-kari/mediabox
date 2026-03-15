"""Fetches the full provider M3U, strips VOD/Series noise, stores TV channels in SQLite."""
import asyncio
import os
import re

import httpx
from sqlmodel import Session, delete, select

from app.models import ProviderChannel

M3U_URL = os.environ.get(
    "M3U_URL",
    "http://livego.club:8080/get.php?username=qjdD0kuNEdBf&password=j17ceXEXeH5s&type=m3u_plus&output=ts",
)

# Group title prefixes/keywords that indicate VOD/series — skip these
_VOD_PATTERNS = re.compile(
    r"(series|movies?|movie|film|vod|24-7|24/7|kids|boxing|ufc|wwe|wrestling|ppv)",
    re.IGNORECASE,
)


def _is_tv_group(group: str) -> bool:
    return not _VOD_PATTERNS.search(group)


def _parse_m3u(text: str):
    """Yield (name, group, url) for each TV channel entry."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF"):
            name_match = re.search(r'tvg-name="([^"]*)"', line)
            group_match = re.search(r'group-title="([^"]*)"', line)
            name = name_match.group(1).strip() if name_match else ""
            group = group_match.group(1).strip() if group_match else ""
            # Next non-empty line should be the URL
            i += 1
            while i < len(lines) and not lines[i].strip():
                i += 1
            if i < len(lines):
                url = lines[i].strip()
                if name and url and not url.startswith("#") and _is_tv_group(group):
                    yield name, group, url
        i += 1


async def fetch_provider_channels(engine) -> int:
    """Download full M3U, parse TV channels, store in DB. Returns count inserted."""
    print("[provider] Fetching full M3U from provider…")
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.get(M3U_URL)
            resp.raise_for_status()
            text = resp.text
    except Exception as e:
        print(f"[provider] Failed to fetch M3U: {e}")
        return 0

    print("[provider] Parsing channels…")
    rows = list(_parse_m3u(text))
    print(f"[provider] Found {len(rows)} TV channels, storing in DB…")

    with Session(engine) as session:
        session.exec(delete(ProviderChannel))
        session.commit()
        for name, group, url in rows:
            session.add(ProviderChannel(name=name, group=group, url=url))
        session.commit()

    print(f"[provider] Stored {len(rows)} provider channels.")
    return len(rows)


async def provider_refresh_loop(engine):
    """Refresh provider channel list once a day."""
    while True:
        await asyncio.sleep(86400)  # 24 hours
        await fetch_provider_channels(engine)


def search_provider_channels(engine, query: str, limit: int = 15):
    """Search provider channels by name (case-insensitive substring)."""
    q = f"%{query}%"
    with Session(engine) as session:
        results = session.exec(
            select(ProviderChannel)
            .where(ProviderChannel.name.ilike(q))
            .limit(limit)
        ).all()
    return [{"id": r.id, "name": r.name, "group": r.group, "url": r.url} for r in results]
