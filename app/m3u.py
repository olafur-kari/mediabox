import hashlib
import json
import os
import re
from typing import Any, Dict, List, Optional

import httpx

THREADFIN_URL = os.environ.get("THREADFIN_URL", "http://100.104.189.115:34400")
LINEUP_URL = f"{THREADFIN_URL}/lineup.json"
GROUPS_FILE = "/data/groups.json"

# In-memory channel cache
_channels_cache: List[Dict] = []
_groups_cache: List[Dict] = []


def _channel_id(name: str) -> str:
    return hashlib.md5(name.encode()).hexdigest()


COUNTRY_NAMES = {
    "IS": "Ísland",
    "NO": "Noregur",
    "SE": "Svíþjóð",
    "DK": "Danmörk",
    "UK": "Bretland",
    "GR": "Grikkland",
    "DE": "Þýskaland",
    "FR": "Frakkland",
    "ES": "Spánn",
    "IT": "Ítalía",
    "NL": "Holland",
    "PL": "Pólland",
    "PT": "Portúgal",
    "US": "Bandaríkin",
    "AU": "Ástralía",
}


def _parse_group(guide_name: str) -> Optional[str]:
    """Extract country group from channel name prefix (e.g. 'IS: ...' → 'Ísland').
    Returns None if not a recognised country channel."""
    bracket_match = re.search(r'\[([^\]]+)\]', guide_name)
    if bracket_match:
        code = bracket_match.group(1).strip().upper()
        return COUNTRY_NAMES.get(code)
    colon_match = re.match(r'^([A-Z]{2,})\s*:', guide_name)
    if colon_match:
        code = colon_match.group(1).strip().upper()
        return COUNTRY_NAMES.get(code)
    return None


def _normalize_channel_name(name: str) -> str:
    """Normalize minor spelling variants so channels from different M3U sources group together."""
    # "TNT Sports N" and "TNT Sport N" are the same channel
    name = re.sub(r'\bSports\b', 'Sport', name, flags=re.IGNORECASE)
    return name


def _strip_backup_suffix(name: str) -> str:
    """Strip backup-related suffixes to find the base channel name."""
    name = re.sub(r'\s+\(?B\d?\)?$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s+Backup\s*\d*$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s+(FHD|UHD|4K|HD)(\s+P\d+)?$', '', name, flags=re.IGNORECASE)
    return name.strip()


def _logo_abbr(name: str) -> str:
    """Generate a short abbreviation for the channel logo placeholder."""
    clean = re.sub(r'^[A-Z]{2}\s*:\s*', '', name)
    clean = re.sub(r'\s*\[.*?\]', '', clean).strip()
    words = clean.split()
    if not words:
        return "TV"
    if len(words) == 1:
        return words[0][:4].upper()
    # Use initials for multi-word names
    abbr = ''.join(w[0] for w in words if w[0].isalpha())[:4].upper()
    return abbr if abbr else words[0][:4].upper()


def _load_groups_config() -> Dict:
    if os.path.exists(GROUPS_FILE):
        try:
            with open(GROUPS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


_FLAG_MAP = {
    "Ísland": "🇮🇸",
    "Noregur": "🇳🇴",
    "Svíþjóð": "🇸🇪",
    "Danmörk": "🇩🇰",
    "Bretland": "🇬🇧",
    "Grikkland": "🇬🇷",
    "Þýskaland": "🇩🇪",
    "Frakkland": "🇫🇷",
    "Spánn": "🇪🇸",
    "Ítalía": "🇮🇹",
    "Holland": "🇳🇱",
    "Pólland": "🇵🇱",
    "Portúgal": "🇵🇹",
    "Bandaríkin": "🇺🇸",
    "Ástralía": "🇦🇺",
}


def _group_flag(group_name: str) -> str:
    return _FLAG_MAP.get(group_name, "📺")


async def fetch_channels() -> List[Dict]:
    """Fetch and parse the Threadfin lineup, returning grouped channels."""
    global _channels_cache, _groups_cache

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(LINEUP_URL)
            resp.raise_for_status()
            lineup = resp.json()
    except Exception as e:
        print(f"[m3u] Failed to fetch lineup from {LINEUP_URL}: {e}")
        return _channels_cache  # Return cached if available

    # lineup is a list of {GuideName, GuideNumber, URL, ...}
    raw_channels = lineup if isinstance(lineup, list) else []

    # Group by base name (stripping backup suffixes)
    base_to_streams: Dict[str, List[Dict]] = {}
    base_to_meta: Dict[str, Dict] = {}

    for item in raw_channels:
        guide_name = item.get("GuideName", "").strip()
        url = item.get("URL", "").strip()
        if not guide_name or not url:
            continue

        base_name = _normalize_channel_name(_strip_backup_suffix(guide_name))
        group = _parse_group(guide_name)
        if group is None:
            continue  # Skip non-country channels

        if base_name not in base_to_streams:
            base_to_streams[base_name] = []
            base_to_meta[base_name] = {
                "group": group,
                "logo": _logo_abbr(base_name),
            }

        is_primary = len(base_to_streams[base_name]) == 0
        suffix_match = re.search(r'\s+(\(B\d?\)|B\d?|Backup\s*\d*|FHD|UHD|4K)$', guide_name, flags=re.IGNORECASE)
        if is_primary:
            label = "Primary"
        elif suffix_match:
            suffix = suffix_match.group(1).strip()
            backup_num = len(base_to_streams[base_name])
            label = f"Backup {backup_num}"
        else:
            label = f"Backup {len(base_to_streams[base_name])}"

        base_to_streams[base_name].append({
            "label": label,
            "url": url,
            "health": "unknown",
        })

    # Build channel objects
    channels: List[Dict] = []
    for base_name, streams in base_to_streams.items():
        meta = base_to_meta[base_name]
        ch_id = _channel_id(base_name)
        channels.append({
            "id": ch_id,
            "name": base_name,
            "logo": meta["logo"],
            "show": "",
            "group": meta["group"],
            "streams": streams,
        })

    # Sort channels by name within each group
    channels.sort(key=lambda c: (c["group"], c["name"]))

    _channels_cache = channels

    # Build groups structure
    groups_map: Dict[str, List[Dict]] = {}
    for ch in channels:
        g = ch["group"]
        if g not in groups_map:
            groups_map[g] = []
        groups_map[g].append(ch)

    groups_config = _load_groups_config()
    _groups_cache = []
    for g_name, g_channels in groups_map.items():
        config = groups_config.get(g_name, {})
        _groups_cache.append({
            "id": _channel_id(g_name),
            "name": config.get("name", g_name),
            "flag": config.get("flag", _group_flag(g_name)),
            "channels": g_channels,
        })

    _groups_cache.sort(key=lambda g: g["name"])

    return _channels_cache


def get_cached_channels() -> List[Dict]:
    return _channels_cache


def get_cached_groups() -> List[Dict]:
    return _groups_cache


def get_channel_by_id(channel_id: str) -> Optional[Dict]:
    for ch in _channels_cache:
        if ch["id"] == channel_id:
            return ch
    return None
