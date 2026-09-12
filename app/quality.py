"""Measures the real resolution of streams, because provider labels lie.

dnstream ships 1280x720 feeds tagged "UHD" and 1920x1080 feeds tagged the same, so
ordering by label is close to guesswork. This probes the streams with ffprobe and
stores what they actually are.

Probing costs a provider connection for a few seconds per stream, and this line
allows exactly one, so we only probe channels somebody actually watches — favourites
and recently-watched — and only in the small hours.
"""
import asyncio
import json
import os
from datetime import datetime, timezone

from sqlmodel import Session, select

from app.m3u import get_cached_channels
from app.models import Favorite, RecentlyWatched, StreamQuality

PROBE_HOUR = int(os.environ.get("QUALITY_PROBE_HOUR", "4"))     # local hour to run
PROBE_TIMEOUT = int(os.environ.get("QUALITY_PROBE_TIMEOUT", "25"))
PROBE_GAP = int(os.environ.get("QUALITY_PROBE_GAP", "5"))       # seconds between probes


async def probe_stream(url: str):
    """Return {"width", "height", "codec"} for a stream, or None if it won't answer."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,codec_name",
        "-of", "json", url,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=PROBE_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None

    try:
        streams = json.loads(out or b"{}").get("streams") or []
        if not streams:
            return None
        s = streams[0]
        if not s.get("height"):
            return None
        return {"width": int(s.get("width") or 0),
                "height": int(s["height"]),
                "codec": s.get("codec_name") or ""}
    except Exception:
        return None


def _watched_channel_ids(engine) -> set:
    with Session(engine) as session:
        favs = {r.channel_id for r in session.exec(select(Favorite)).all()}
        recents = {r.channel_id for r in session.exec(select(RecentlyWatched)).all()}
    return favs | recents


def probe_targets(engine) -> list:
    """(guide_name, url) for every variant of a channel somebody watches."""
    wanted = _watched_channel_ids(engine)
    targets = []
    for ch in get_cached_channels():
        if ch["id"] not in wanted:
            continue
        for s in ch.get("streams", []):
            if s.get("guide_name") and s.get("url"):
                targets.append((s["guide_name"], s["url"]))
    return targets


async def run_quality_probe(engine) -> int:
    """Probe every variant of every watched channel, one at a time. Returns count measured."""
    targets = probe_targets(engine)
    if not targets:
        print("[quality] Nothing to probe — no favourites or recently watched yet.")
        return 0

    print(f"[quality] Probing {len(targets)} streams (one at a time, ~{PROBE_GAP + 5}s each)…")
    measured = 0
    for guide_name, url in targets:
        result = await probe_stream(url)
        if result:
            with Session(engine) as session:
                row = session.get(StreamQuality, guide_name) or StreamQuality(guide_name=guide_name)
                row.width = result["width"]
                row.height = result["height"]
                row.codec = result["codec"]
                row.measured_at = datetime.now(timezone.utc).replace(tzinfo=None)
                session.add(row)
                session.commit()
            measured += 1
            print(f"[quality]   {guide_name} -> {result['width']}x{result['height']} {result['codec']}")
        else:
            print(f"[quality]   {guide_name} -> no answer")
        await asyncio.sleep(PROBE_GAP)

    print(f"[quality] Measured {measured}/{len(targets)} streams.")
    return measured


async def quality_probe_loop(engine):
    """Run once a day at PROBE_HOUR, when nobody is likely to be watching."""
    while True:
        now = datetime.now()
        hours = (PROBE_HOUR - now.hour) % 24 or 24
        await asyncio.sleep(hours * 3600 - now.minute * 60)
        try:
            await run_quality_probe(engine)
        except Exception as e:
            print(f"[quality] Probe run failed: {e}")
