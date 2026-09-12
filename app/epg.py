"""Fetches XMLTV EPG, matches it to our lineup, caches programmes for search."""
import asyncio
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
import xml.etree.ElementTree as ET

import httpx

from app.m3u import get_cached_channels, identity_for

# Comma-separated so a second provider can be added without a code change.
EPG_URLS = [u.strip() for u in os.environ.get("EPG_URL", "").split(",") if u.strip()]

# How far ahead to keep programmes. Providers publish only a few days; dnstream
# gives about 3. This bounds memory as well as the watchlist's horizon.
EPG_WINDOW_HOURS = int(os.environ.get("EPG_WINDOW_HOURS", "72"))

# {channel_id: [{title, desc, start, stop}]}
_epg_cache: Dict[str, List[Dict[str, Any]]] = {}


def get_epg_cache() -> Dict[str, List[Dict]]:
    return _epg_cache


def epg_window_hours() -> int:
    return EPG_WINDOW_HOURS


def _normalize(s: str) -> str:
    return unicodedata.normalize('NFD', s.lower()).encode('ascii', 'ignore').decode()


def _parse_time(s: str) -> Optional[datetime]:
    try:
        s = s.strip()
        dt = datetime.strptime(s[:14], '%Y%m%d%H%M%S')
        tz_part = s[14:].strip()
        if tz_part and len(tz_part) >= 5:
            sign = 1 if tz_part[0] == '+' else -1
            hours = int(tz_part[1:3])
            mins = int(tz_part[3:5])
            offset_secs = sign * (hours * 3600 + mins * 60)
            dt = dt - timedelta(seconds=offset_secs)
        return dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


async def fetch_epg() -> None:
    global _epg_cache

    channels = get_cached_channels()
    if not channels:
        print("[epg] No channels in the lineup yet — skipping EPG fetch.")
        return
    if not EPG_URLS:
        print("[epg] No EPG_URL configured — guide not built.")
        return

    known_ids = {ch['id'] for ch in channels}
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(hours=EPG_WINDOW_HOURS)
    new_cache: Dict[str, List[Dict]] = {}
    matched_total = 0

    for url in EPG_URLS:
        host = re.sub(r"^https?://([^/:]+).*", r"\1", url)
        print(f"[epg] Fetching guide from {host}…")
        try:
            async with httpx.AsyncClient(timeout=180, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                xml_data = resp.content
        except Exception as e:
            print(f"[epg] Failed to fetch guide from {host}: {e}")
            continue

        print(f"[epg] Parsing {len(xml_data) // 1024}KB from {host}…")
        try:
            root = ET.fromstring(xml_data)
        except Exception as e:
            print(f"[epg] Failed to parse XML from {host}: {e}")
            continue

        # Match EPG channels to ours using the same identity rule as the lineup.
        epg_to_channel: Dict[str, str] = {}
        for epg_ch in root.findall('channel'):
            epg_id = epg_ch.get('id', '')
            for display in epg_ch.findall('display-name'):
                ch_id = identity_for((display.text or '').strip())
                if ch_id and ch_id in known_ids:
                    epg_to_channel[epg_id] = ch_id
                    break

        matched_total += len(epg_to_channel)
        print(f"[epg] {host}: matched {len(epg_to_channel)} guide channels to the lineup")

        for p in root.findall('programme'):
            ch_id = epg_to_channel.get(p.get('channel', ''))
            if not ch_id:
                continue

            start = _parse_time(p.get('start', ''))
            stop = _parse_time(p.get('stop', ''))
            if not start or not stop or stop < now or start > window_end:
                continue

            title = (p.findtext('title', '') or '').strip()
            if not title or '<' in title:  # some providers embed XML in titles
                continue

            new_cache.setdefault(ch_id, []).append({
                'title': title,
                'desc': (p.findtext('desc', '') or '').strip()[:300],
                'start': start.isoformat(),
                'stop': stop.isoformat(),
            })

    if not new_cache:
        print("[epg] Nothing parsed — keeping the previous guide.")
        return

    for progs in new_cache.values():
        progs.sort(key=lambda x: x['start'])

    _epg_cache = new_cache
    total = sum(len(v) for v in new_cache.values())
    print(f"[epg] Cached {total} programmes for {len(new_cache)} channels "
          f"({EPG_WINDOW_HOURS}h window, {matched_total} matched).")


async def epg_refresh_loop() -> None:
    """Refresh every 6 hours. The guide only extends a few days and the files are
    large (60MB+), so there is nothing to gain from fetching more often."""
    while True:
        await asyncio.sleep(21600)  # 6 hours
        await fetch_epg()


def _matches(terms: List[str], *fields: str) -> bool:
    """Every term must appear somewhere in the supplied fields."""
    haystack = _normalize(" ".join(f for f in fields if f))
    return all(t in haystack for t in terms)


def search_epg(query: str, channels_by_id: Dict[str, Dict], limit: int = 200) -> List[Dict]:
    """Search programme titles/descriptions. Returns matches sorted live-first."""
    if len(query) < 2:
        return []

    terms = [t for t in _normalize(query).split() if t]
    if not terms:
        return []

    now = datetime.now(timezone.utc)
    results = []

    for ch_id, programmes in _epg_cache.items():
        ch = channels_by_id.get(ch_id)
        if not ch:
            continue

        for prog in programmes:
            if not _matches(terms, prog['title'], prog.get('desc', '')):
                continue

            start = datetime.fromisoformat(prog['start'])
            stop = datetime.fromisoformat(prog['stop'])
            is_live = start <= now <= stop
            minutes_until = max(0, int((start - now).total_seconds() / 60))

            results.append({
                'channel_id': ch_id,
                'channel_name': ch['name'],
                'group': ch.get('group', ''),
                'title': prog['title'],
                'desc': prog.get('desc', ''),
                'start': prog['start'],
                'stop': prog['stop'],
                'live': is_live,
                'minutes_until': minutes_until if not is_live else 0,
            })

    results.sort(key=lambda r: (not r['live'], r['start']))
    return results[:limit]
