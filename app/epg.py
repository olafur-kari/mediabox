"""Fetches XMLTV EPG, matches to our lineup channels, caches programmes for search."""
import asyncio
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
import xml.etree.ElementTree as ET

import httpx

from app.m3u import get_cached_channels

EPG_URL = os.environ.get(
    "EPG_URL",
    "http://livego.club:8080/xmltv.php?username=qjdD0kuNEdBf&password=j17ceXEXeH5s",
)

# {channel_id: [{title, desc, start, stop}]}
_epg_cache: Dict[str, List[Dict[str, Any]]] = {}


def get_epg_cache() -> Dict[str, List[Dict]]:
    return _epg_cache


def _normalize(s: str) -> str:
    return unicodedata.normalize('NFD', s.lower()).encode('ascii', 'ignore').decode()


def _strip_quality(name: str) -> str:
    return re.sub(r'\s+(FHD|HD|SD)$', '', name, flags=re.IGNORECASE).strip()


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
        return

    # Build lookup: normalized+quality-stripped name → channel_id
    ch_lookup: Dict[str, str] = {}
    for ch in channels:
        key = _strip_quality(_normalize(ch['name']))
        ch_lookup[key] = ch['id']

    print(f"[epg] Fetching EPG for {len(ch_lookup)} channels…")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(EPG_URL)
            resp.raise_for_status()
            xml_data = resp.content
    except Exception as e:
        print(f"[epg] Failed to fetch EPG: {e}")
        return

    print(f"[epg] Parsing {len(xml_data) // 1024}KB…")
    try:
        root = ET.fromstring(xml_data)
    except Exception as e:
        print(f"[epg] Failed to parse XML: {e}")
        return

    # Match EPG channel IDs → our channel IDs via display-name
    epg_to_channel: Dict[str, str] = {}
    for epg_ch in root.findall('channel'):
        epg_id = epg_ch.get('id', '')
        display = epg_ch.findtext('display-name', '')
        key = _strip_quality(_normalize(display))
        ch_id = ch_lookup.get(key)
        if ch_id:
            epg_to_channel[epg_id] = ch_id

    print(f"[epg] Matched {len(epg_to_channel)} EPG channels to lineup")

    now = datetime.now(timezone.utc)
    window_end = now + timedelta(hours=12)
    new_cache: Dict[str, List[Dict]] = {}

    for p in root.findall('programme'):
        ch_id = epg_to_channel.get(p.get('channel', ''))
        if not ch_id:
            continue

        start = _parse_time(p.get('start', ''))
        stop = _parse_time(p.get('stop', ''))
        if not start or not stop or stop < now or start > window_end:
            continue

        title = (p.findtext('title', '') or '').strip()
        if not title or '<' in title:  # Skip malformed entries (some providers embed XML in titles)
            continue

        desc = (p.findtext('desc', '') or '').strip()[:200]

        new_cache.setdefault(ch_id, []).append({
            'title': title,
            'desc': desc,
            'start': start.isoformat(),
            'stop': stop.isoformat(),
        })

    for progs in new_cache.values():
        progs.sort(key=lambda x: x['start'])

    _epg_cache = new_cache
    total = sum(len(v) for v in new_cache.values())
    print(f"[epg] Cached {total} programmes for {len(new_cache)} channels.")


async def epg_refresh_loop() -> None:
    while True:
        await asyncio.sleep(3600)  # Refresh every hour
        await fetch_epg()


def search_epg(query: str, channels_by_id: Dict[str, Dict]) -> List[Dict]:
    """Search programme titles/descriptions. Returns matches with channel info, sorted live-first."""
    if len(query) < 2:
        return []

    q = _normalize(query)
    now = datetime.now(timezone.utc)
    results = []

    for ch_id, programmes in _epg_cache.items():
        ch = channels_by_id.get(ch_id)
        if not ch:
            continue

        for prog in programmes:
            if q not in _normalize(prog['title']) and q not in _normalize(prog.get('desc', '')):
                continue

            start = datetime.fromisoformat(prog['start'])
            stop = datetime.fromisoformat(prog['stop'])
            is_live = start <= now <= stop
            minutes_until = max(0, int((start - now).total_seconds() / 60))

            results.append({
                'channel_id': ch_id,
                'channel_name': ch['name'],
                'title': prog['title'],
                'start': prog['start'],
                'stop': prog['stop'],
                'live': is_live,
                'minutes_until': minutes_until if not is_live else 0,
            })

    results.sort(key=lambda r: (not r['live'], r['minutes_until']))
    return results
