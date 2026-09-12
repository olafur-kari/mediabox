"""Fetches provider M3U playlists, strips VOD/series noise, stores searchable channels in SQLite.

This is the *search index*, separate from the browse lineup that comes via Threadfin.
It deliberately keeps sport, PPV and event feeds that the lineup filters out, because
those carry fixtures in their names ("Liverpool v Fulham • Matchweek 4") and are the
main thing worth searching for.
"""
import asyncio
import os
import re
import unicodedata
import urllib.parse

import httpx
from sqlmodel import Session, delete, select

from app.models import ProviderChannel

# Comma-separated so a second provider can be added without a code change.
M3U_URLS = [u.strip() for u in os.environ.get("M3U_URL", "").split(",") if u.strip()]

# Categories with no live-TV value. Sport/PPV/events are deliberately NOT here.
_SKIP_CATEGORY = re.compile(
    r"(series|movies?|film|cinema|pelicula|vod|24[-/ ]?7|kids|enfants|ni[nñ]os|"
    r"bambini|novela|netflix|disney\+|amazon prime|hulu|hbo max)",
    re.IGNORECASE,
)

# VOD entries are also identifiable from the URL path.
_VOD_URL = re.compile(r"/(movie|series)/", re.IGNORECASE)

# Provider category separator rows: "##### UK - SPORTS #####"
_SEPARATOR = re.compile(r"^\s*#")


def _normalize(s: str) -> str:
    """Lowercase + strip diacritics for accent-insensitive search."""
    return unicodedata.normalize('NFD', s.lower()).encode('ascii', 'ignore').decode()


def _is_wanted(group: str, url: str) -> bool:
    if _VOD_URL.search(url):
        return False
    return not _SKIP_CATEGORY.search(group)


def _parse_m3u(text: str):
    """Yield (name, normalized_name, group, url) for each wanted entry."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF"):
            group_match = re.search(r'group-title="([^"]*)"', line)
            group = group_match.group(1).strip() if group_match else ""

            # Prefer tvg-name, fall back to the display text after the final comma.
            name_match = re.search(r'tvg-name="([^"]*)"', line)
            name = name_match.group(1).strip() if name_match else ""
            if not name and "," in line:
                name = line.rsplit(",", 1)[1].strip()

            i += 1
            while i < len(lines) and not lines[i].strip():
                i += 1
            if i < len(lines):
                url = lines[i].strip()
                if (
                    name
                    and url
                    and not url.startswith("#")
                    and not _SEPARATOR.match(name)
                    and _is_wanted(group, url)
                ):
                    yield name, _normalize(name), group, url
        i += 1


# Xtream-style playlists (get.php?username=…&password=…) also expose a JSON API.
# Prefer it: it returns ~7MB instead of ~94MB and, unlike get.php, is not rate
# limited — dnstream answers repeated get.php calls with HTTP 884 and then bans
# the IP outright, which matters because Threadfin pulls the same playlist daily.
_XTREAM_RE = re.compile(r"^(https?://[^/]+)/get\.php\?(.*)$", re.IGNORECASE)


def _xtream_parts(url: str):
    """Return (base_url, username, password) for an Xtream playlist URL, else None."""
    m = _XTREAM_RE.match(url)
    if not m:
        return None
    qs = urllib.parse.parse_qs(m.group(2))
    user = (qs.get("username") or [None])[0]
    pw = (qs.get("password") or [None])[0]
    if not user or not pw:
        return None
    return m.group(1), user, pw


async def _fetch_via_api(client, base: str, user: str, pw: str):
    """Yield (name, normalized, group, url) using the provider's JSON API."""
    auth = f"username={urllib.parse.quote(user)}&password={urllib.parse.quote(pw)}"

    resp = await client.get(f"{base}/player_api.php?{auth}&action=get_live_categories")
    resp.raise_for_status()
    categories = {str(c.get("category_id")): c.get("category_name", "") for c in resp.json()}

    resp = await client.get(f"{base}/player_api.php?{auth}&action=get_live_streams")
    resp.raise_for_status()

    for item in resp.json():
        name = (item.get("name") or "").strip()
        stream_id = item.get("stream_id")
        if not name or stream_id is None or _SEPARATOR.match(name):
            continue
        group = categories.get(str(item.get("category_id")), "")
        url = f"{base}/{user}/{pw}/{stream_id}"
        if _is_wanted(group, url):
            yield name, _normalize(name), group, url


async def fetch_provider_channels(engine) -> int:
    """Download every configured playlist, parse, store in DB. Returns count inserted."""
    if not M3U_URLS:
        print("[provider] No M3U_URL configured — search index not built.")
        return 0

    rows = []
    for url in M3U_URLS:
        host = re.sub(r"^https?://([^/:]+).*", r"\1", url)
        xtream = _xtream_parts(url)
        found = []
        try:
            async with httpx.AsyncClient(timeout=180, follow_redirects=True) as client:
                if xtream:
                    print(f"[provider] Fetching catalogue from {host} via API…")
                    found = [row async for row in _fetch_via_api(client, *xtream)]
                else:
                    print(f"[provider] Fetching playlist from {host}…")
                    resp = await client.get(url)
                    resp.raise_for_status()
                    found = list(_parse_m3u(resp.text))
        except Exception as e:
            print(f"[provider] Failed to fetch catalogue from {host}: {e}")
            continue
        print(f"[provider] {host}: {len(found)} searchable channels")
        rows.extend(found)

    if not rows:
        print("[provider] Nothing fetched — keeping the previous index.")
        return 0

    print(f"[provider] Storing {len(rows)} channels…")
    with Session(engine) as session:
        session.exec(delete(ProviderChannel))
        session.commit()
        for name, name_normalized, group, url in rows:
            session.add(ProviderChannel(
                name=name, name_normalized=name_normalized, group=group, url=url
            ))
        session.commit()

    print(f"[provider] Stored {len(rows)} provider channels.")
    return len(rows)


async def provider_refresh_loop(engine):
    """Refresh once a day. Providers rate-limit playlist downloads aggressively —
    dnstream returns HTTP 884 and then bans the IP outright — so do not shorten this."""
    while True:
        await asyncio.sleep(86400)  # 24 hours
        await fetch_provider_channels(engine)


def search_provider_channels(engine, query: str, limit: int = 50):
    """Search provider channels by normalized name (accent-insensitive).

    Every whitespace-separated term must appear, so "liverpool fulham" narrows
    rather than widens.
    """
    terms = [t for t in _normalize(query).split() if t]
    if not terms:
        return []
    with Session(engine) as session:
        stmt = select(ProviderChannel)
        for t in terms:
            stmt = stmt.where(ProviderChannel.name_normalized.ilike(f"%{t}%"))
        results = session.exec(stmt.limit(limit)).all()
    return [
        {"id": r.id, "name": r.name, "group": r.group, "url": r.url}
        for r in results
    ]
