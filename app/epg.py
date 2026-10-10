"""Fetches XMLTV EPG, matches it to our lineup, caches programmes for search."""
import asyncio
import gzip
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

# English UK guides, matched as "UK - <display name>". The provider's own guide has
# no listings at all for TNT Sports or Sky Sports Premier League, which is where the
# Premier League lives, so team searches never found the actual games. epgshare01
# publishes the UK listings (with team names in the descriptions) as a free feed.
UK_EPG_URLS = [u.strip() for u in os.environ.get(
    "UK_EPG_URL", "https://epgshare01.online/epgshare01/epg_ripper_UK1.xml.gz"
).split(",") if u.strip()]

# That feed abbreviates Sky's names ("SkySpMainEvHD", "SkySp Fball HD"); spell
# them out so identity_for sees the same name the lineup uses.
_UK_NAME_FIXES = [
    (re.compile(r"^SkySp(?=[A-Z .+])"), "Sky Sports "),
    (re.compile(r"MainEv"), "Main Event"),
    (re.compile(r"\bFball\b"), "Football"),
    (re.compile(r"(?<=[a-z0-9])HD$"), " HD"),
]

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


def _uk_display_name(name: str) -> str:
    for pattern, repl in _UK_NAME_FIXES:
        name = pattern.sub(repl, name)
    return "UK - " + re.sub(r"\s+", " ", name).strip()


async def _load_guide(url, name_for, known_ids, now, window_end) -> Dict[str, List[Dict]]:
    """Fetch one XMLTV file and return {channel_id: [programme]} for our lineup.

    name_for turns a guide display-name into the raw name identity_for expects.
    """
    host = re.sub(r"^https?://([^/:]+).*", r"\1", url)
    print(f"[epg] Fetching guide from {host}…")
    try:
        async with httpx.AsyncClient(timeout=180, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            xml_data = resp.content
    except Exception as e:
        print(f"[epg] Failed to fetch guide from {host}: {e}")
        return {}

    try:
        if xml_data[:2] == b"\x1f\x8b":
            xml_data = gzip.decompress(xml_data)
        print(f"[epg] Parsing {len(xml_data) // 1024}KB from {host}…")
        root = ET.fromstring(xml_data)
    except Exception as e:
        print(f"[epg] Failed to parse XML from {host}: {e}")
        return {}

    # Match EPG channels to ours using the same identity rule as the lineup.
    epg_to_channel: Dict[str, str] = {}
    for epg_ch in root.findall('channel'):
        epg_id = epg_ch.get('id', '')
        for display in epg_ch.findall('display-name'):
            ch_id = identity_for(name_for((display.text or '').strip()))
            if ch_id and ch_id in known_ids:
                epg_to_channel[epg_id] = ch_id
                break
    print(f"[epg] {host}: matched {len(epg_to_channel)} guide channels to the lineup")

    guide: Dict[str, List[Dict]] = {}
    seen = set()
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

        # Several guide channels (HD/SD, regional) can map to one of ours.
        key = (ch_id, start, title)
        if key in seen:
            continue
        seen.add(key)

        sub = (p.findtext('sub-title', '') or '').strip()
        desc = (p.findtext('desc', '') or '').strip()
        guide.setdefault(ch_id, []).append({
            'title': title,
            'desc': (f"{sub}. {desc}" if sub and sub not in desc else desc)[:300],
            'start': start.isoformat(),
            'stop': stop.isoformat(),
        })
    return guide


async def fetch_epg() -> None:
    global _epg_cache

    channels = get_cached_channels()
    if not channels:
        print("[epg] No channels in the lineup yet — skipping EPG fetch.")
        return
    if not EPG_URLS and not UK_EPG_URLS:
        print("[epg] No EPG_URL configured — guide not built.")
        return

    known_ids = {ch['id'] for ch in channels}
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(hours=EPG_WINDOW_HOURS)
    new_cache: Dict[str, List[Dict]] = {}
    matched_total = 0

    for url in EPG_URLS:
        guide = await _load_guide(url, lambda name: name, known_ids, now, window_end)
        for ch_id, progs in guide.items():
            new_cache.setdefault(ch_id, []).extend(progs)
        matched_total += len(guide)

    # The English UK guide replaces the provider's listings channel by channel: it is
    # complete where the provider's is thin, and mixing both would list every
    # programme twice.
    for url in UK_EPG_URLS:
        guide = await _load_guide(url, _uk_display_name, known_ids, now, window_end)
        new_cache.update(guide)
        matched_total += len(guide)

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


# ── Match search ──────────────────────────────────────────────────────────────

# Channels whose commentary is English. Everything else is hidden from match search,
# except the Nordic Viaplay channels, which carry every Premier League game —
# including the Saturday 3pm ones no UK channel is allowed to show.
ENGLISH_COUNTRIES = {"UK", "US", "AU", "CA", "IE"}
FALLBACK_COUNTRIES = {"NO", "SE", "DK"}

# Viaplay's guide names the ground, not the teams ("Stamford Bridge, London.
# Omgång 6"), so a team search also looks for its home stadium. Away games stay
# invisible there — the guide simply doesn't say who is visiting.
TEAM_STADIUMS = {
    "arsenal": "emirates",
    "aston villa": "villa park", "villa": "villa park",
    "bournemouth": "vitality stadium",
    "brentford": "gtech",
    "brighton": "amex",
    "burnley": "turf moor",
    "chelsea": "stamford bridge",
    "coventry": "coventry building society arena",
    "crystal palace": "selhurst park", "palace": "selhurst park",
    "everton": "hill dickinson",
    "fulham": "craven cottage",
    "hull": "mkm stadium",
    "ipswich": "portman road",
    "leeds": "elland road",
    "liverpool": "anfield",
    "manchester city": "etihad", "man city": "etihad",
    "manchester united": "old trafford", "man united": "old trafford", "man utd": "old trafford",
    "newcastle": "st james",
    "nottingham forest": "city ground", "forest": "city ground",
    "sunderland": "stadium of light",
    "tottenham": "tottenham hotspur stadium", "spurs": "tottenham hotspur stadium",
    "west ham": "london stadium",
    "wolves": "molineux", "wolverhampton": "molineux",
}

# How close two listings must start to count as the same game, for deciding whether
# a Nordic listing is the only way to watch it.
_SAME_GAME_WINDOW = timedelta(minutes=30)


def _query_variants(query: str) -> List[List[str]]:
    """The query's terms, plus the same with a team name swapped for its stadium."""
    q = _normalize(query).strip()
    variants = [q.split()]
    for team, stadium in TEAM_STADIUMS.items():
        if re.search(rf"\b{re.escape(team)}\b", q):
            variants.append(re.sub(rf"\b{re.escape(team)}\b", stadium, q).split())
    return [v for v in variants if v]


def search_matches(query: str, channels_by_id: Dict[str, Dict], limit: int = 30) -> List[Dict]:
    """Live and upcoming programmes for a search, English channels only.

    One row per programme (BBC One alone has a dozen regional copies), and a Nordic
    listing only when no English channel shows something at the same time.
    """
    if len(query.strip()) < 2:
        return []
    variants = _query_variants(query)
    now = datetime.now(timezone.utc)

    english: Dict[tuple, Dict] = {}
    nordic: List[Dict] = []

    for ch_id, programmes in _epg_cache.items():
        ch = channels_by_id.get(ch_id)
        if not ch:
            continue
        country = ch.get("country")
        if country not in ENGLISH_COUNTRIES and country not in FALLBACK_COUNTRIES:
            continue
        # The fallback exists for games; a Swedish film channel mentioning
        # "Liverpool" is not one.
        if country in FALLBACK_COUNTRIES and "sport" not in _normalize(ch['name']):
            continue

        for prog in programmes:
            if not any(_matches(v, prog['title'], prog.get('desc', '')) for v in variants):
                continue
            start = datetime.fromisoformat(prog['start'])
            stop = datetime.fromisoformat(prog['stop'])
            if stop <= now:
                continue
            live = start <= now
            row = {
                'channel_id': ch_id,
                'channel_name': ch['name'],
                'title': prog['title'],
                'desc': prog.get('desc', ''),
                'start': prog['start'],
                'stop': prog['stop'],
                'live': live,
                'minutes_until': 0 if live else int((start - now).total_seconds() // 60),
                'lang': None if country in ENGLISH_COUNTRIES else country,
                'also_on': 0,
            }
            if country in ENGLISH_COUNTRIES:
                key = (_normalize(prog['title']), prog['start'])
                kept = english.get(key)
                if kept is None:
                    english[key] = row
                else:
                    # Keep the shortest channel name: "BBC One" over "BBC One Yorks".
                    row['also_on'] = kept['also_on'] + 1
                    if len(row['channel_name']) < len(kept['channel_name']):
                        english[key] = row
                    else:
                        kept['also_on'] += 1
            else:
                nordic.append(row)

    results = list(english.values())
    english_starts = [datetime.fromisoformat(r['start']) for r in results]
    seen_nordic = set()
    for row in nordic:
        start = datetime.fromisoformat(row['start'])
        if any(abs(start - s) <= _SAME_GAME_WINDOW for s in english_starts):
            continue
        key = (_normalize(row['desc'][:40]), row['start'])
        if key in seen_nordic:
            continue
        seen_nordic.add(key)
        results.append(row)

    results.sort(key=lambda r: (not r['live'], r['start']))
    return results[:limit]
