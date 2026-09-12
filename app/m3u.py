import hashlib
import json
import os
import re
from typing import Any, Dict, List, Optional

import httpx

THREADFIN_URL = os.environ.get("THREADFIN_URL", "http://100.113.186.78:34400")
LINEUP_URL = f"{THREADFIN_URL}/lineup.json"
GROUPS_FILE = os.path.join(os.environ.get("MEDIABOX_DATA_DIR", "/data"), "groups.json")

# In-memory channel cache
_channels_cache: List[Dict] = []
_groups_cache: List[Dict] = []


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
    "CA": "Kanada",
}

# Providers spell the same country differently — fold the variants onto our codes.
_COUNTRY_ALIASES = {
    "NOR": "NO", "SWE": "SE", "DEN": "DK", "GER": "DE", "USA": "US",
    "SPA": "ES", "ITA": "IT", "POR": "PT", "POL": "PL", "NED": "NL",
    "GRE": "GR", "AUS": "AU", "CAN": "CA", "UKI": "UK", "FRA": "FR",
}

# Codes to drop even though they look like one of ours.
# dnstream uses "IS" for Israel, which collides with our IS = Ísland.
# Empty this set when an Icelandic source is added, so IS means Ísland again.
EXCLUDED_COUNTRY_CODES = {"IS"}

# Providers pad their category lists with separator rows: "##### UK - SPORTS #####"
_SEPARATOR_RE = re.compile(r"^\s*#")

# Short tokens that should stay upper-case when we tidy an ALL-CAPS channel name.
_ACRONYMS = {
    "BBC", "ITV", "TNT", "HBO", "ESPN", "NBC", "CBS", "ABC", "CNN", "MTV",
    "SVT", "NRK", "DR", "RTL", "ZDF", "ARD", "CNBC", "BT", "TV", "TV2",
    "PL", "F1", "NFL", "NBA", "NHL", "MLB", "UFC", "WWE", "EPL", "EFL",
    "WSL", "VIP", "PPV", "US", "UK", "LA", "FA", "MSG", "AMC", "AXN",
}


def _group_id(name: str) -> str:
    return hashlib.md5(name.encode()).hexdigest()


def _strip_backup_suffix(name: str) -> str:
    """Strip backup/quality suffixes to find the base channel name."""
    name = re.sub(r'\s+\(?B\d?\)?$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s+Backup\s*\d*$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'\s+(FHD|UHD|4K|HD|SD)(\s+P\d+)?$', '', name, flags=re.IGNORECASE)
    return name.strip()


def _canonical_name(name: str) -> str:
    """Provider-independent form of a channel name.

    Different providers write the same channel differently — "Sky Sport Main Event",
    "SKY SPORTS MAIN EVENTS UHD" — so identity is built from a flattened form rather
    than the raw name. This is what keeps favourites alive across a provider change.
    """
    n = _strip_backup_suffix(name)
    n = re.sub(r'\bSPORTS\b', 'SPORT', n, flags=re.IGNORECASE)
    n = re.sub(r'\bEVENTS\b', 'EVENT', n, flags=re.IGNORECASE)
    return re.sub(r'[^a-z0-9]+', ' ', n.lower()).strip()


def _channel_id(country_code: str, name: str) -> str:
    """Stable ID: country + canonical name. Country is part of identity because
    providers carry the same channel in several languages (UK/IT/FR Sky Sport F1)."""
    return hashlib.md5(f"{country_code}|{_canonical_name(name)}".encode()).hexdigest()


def _split_country(guide_name: str):
    """Split "UK - BBC 1 FHD" / "IS: RÚV" / "[NO] NRK1" into (code, rest).

    Returns (None, name) when there is no recognisable country tag.
    """
    m = re.match(r'^\[([A-Za-z]{2,4})\]\s*(.+)$', guide_name)
    if not m:
        m = re.match(r'^([A-Za-z]{2,4})\s*[:\-]\s*(.+)$', guide_name)
    if not m:
        return None, guide_name.strip()
    code = m.group(1).upper()
    return _COUNTRY_ALIASES.get(code, code), m.group(2).strip()


def _display_name(name: str) -> str:
    """Tidy an ALL-CAPS provider name into something readable."""
    if not name.isupper():
        return name
    words = []
    for w in name.split():
        if w in _ACRONYMS or not w.isalpha():
            words.append(w)
        else:
            words.append(w.capitalize())
    return " ".join(words)


def identity_for(guide_name: str) -> Optional[str]:
    """Channel ID for any raw provider or EPG name, or None if it isn't a channel we keep.

    Shared by the lineup, the EPG matcher and the favourites migration so all three
    agree on what counts as "the same channel".
    """
    if not guide_name or _SEPARATOR_RE.match(guide_name):
        return None
    code, rest = _split_country(guide_name)
    if not code or code in EXCLUDED_COUNTRY_CODES or code not in COUNTRY_NAMES:
        return None
    return _channel_id(code, rest)


def _logo_abbr(name: str) -> str:
    """Generate a short abbreviation for the channel logo placeholder."""
    clean = re.sub(r'\s*\[.*?\]', '', name).strip()
    words = clean.split()
    if not words:
        return "TV"
    if len(words) == 1:
        return words[0][:4].upper()
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
    "Kanada": "🇨🇦",
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

    raw_channels = lineup if isinstance(lineup, list) else []

    base_to_streams: Dict[str, List[Dict]] = {}
    base_to_meta: Dict[str, Dict] = {}
    skipped_no_country = 0
    skipped_excluded = 0

    for item in raw_channels:
        guide_name = item.get("GuideName", "").strip()
        url = item.get("URL", "").strip()
        if not guide_name or not url:
            continue
        if _SEPARATOR_RE.match(guide_name):
            continue  # provider category separator row, not a channel

        code, rest = _split_country(guide_name)
        if code in EXCLUDED_COUNTRY_CODES:
            skipped_excluded += 1
            continue
        group = COUNTRY_NAMES.get(code) if code else None
        if group is None:
            skipped_no_country += 1
            continue

        base_name = _display_name(_strip_backup_suffix(rest))
        key = f"{code}|{_canonical_name(rest)}"

        if key not in base_to_streams:
            base_to_streams[key] = []
            base_to_meta[key] = {
                "group": group,
                "name": base_name,
                "country": code,
                "logo": _logo_abbr(base_name),
            }

        is_primary = len(base_to_streams[key]) == 0
        if is_primary:
            label = "Primary"
        else:
            label = f"Backup {len(base_to_streams[key])}"

        base_to_streams[key].append({
            "label": label,
            "url": url,
            "health": "unknown",
        })

    channels: List[Dict] = []
    for key, streams in base_to_streams.items():
        meta = base_to_meta[key]
        channels.append({
            "id": _channel_id(meta["country"], meta["name"]),
            "name": meta["name"],
            "logo": meta["logo"],
            "show": "",
            "group": meta["group"],
            "country": meta["country"],
            "streams": streams,
        })

    channels.sort(key=lambda c: (c["group"], c["name"]))
    _channels_cache = channels

    groups_map: Dict[str, List[Dict]] = {}
    for ch in channels:
        groups_map.setdefault(ch["group"], []).append(ch)

    groups_config = _load_groups_config()
    _groups_cache = []
    for g_name, g_channels in groups_map.items():
        config = groups_config.get(g_name, {})
        _groups_cache.append({
            "id": _group_id(g_name),
            "name": config.get("name", g_name),
            "flag": config.get("flag", _group_flag(g_name)),
            "channels": g_channels,
        })

    _groups_cache.sort(key=lambda g: g["name"])

    print(f"[m3u] Lineup: {len(channels)} channels in {len(_groups_cache)} groups "
          f"(skipped {skipped_no_country} without a country tag, {skipped_excluded} excluded)")

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
