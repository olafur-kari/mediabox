#!/usr/bin/env python3
"""Build an M3U file for Threadfin from the provider's Xtream API.

Threadfin re-downloads its M3U source every time it starts. Providers rate-limit
that endpoint hard — dnstream answers repeated get.php calls with HTTP 884 and
then blocks the IP, which leaves Threadfin unable to finish starting at all.

The JSON API has no such limit, so we pull the catalogue from there and hand
Threadfin a local file instead. Point its M3U source at the output path and it
can restart as often as it likes without touching the provider.

    python3 scripts/build-playlist.py --out /path/to/dnstream.m3u
"""
import argparse
import json
import re
import sys
import urllib.parse
import urllib.request

DEFAULT_OUT = "/home/oli/mediabox/config/threadfin/dnstream.m3u"


def parse_source(url: str):
    m = re.match(r"^(https?://[^/]+)/get\.php\?(.*)$", url, re.IGNORECASE)
    if not m:
        sys.exit(f"Not an Xtream playlist URL: {url}")
    qs = urllib.parse.parse_qs(m.group(2))
    return m.group(1), qs["username"][0], qs["password"][0]


def api(base, user, pw, action):
    url = f"{base}/player_api.php?username={urllib.parse.quote(user)}&password={urllib.parse.quote(pw)}&action={action}"
    with urllib.request.urlopen(url, timeout=180) as r:
        return json.load(r)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", help="provider get.php URL (defaults to M3U_URL in the env file)")
    ap.add_argument("--env", default="/home/oli/mediabox/.env")
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args()

    source = args.source
    if not source:
        for line in open(args.env):
            if line.startswith("M3U_URL="):
                source = line.split("=", 1)[1].strip()
                break
    if not source:
        sys.exit("No source URL: pass --source or set M3U_URL in the env file.")

    base, user, pw = parse_source(source)
    categories = {str(c["category_id"]): c["category_name"] for c in api(base, user, pw, "get_live_categories")}
    streams = api(base, user, pw, "get_live_streams")

    written = 0
    with open(args.out, "w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for s in streams:
            name = (s.get("name") or "").strip()
            sid = s.get("stream_id")
            if not name or sid is None or name.startswith("#"):
                continue
            group = categories.get(str(s.get("category_id")), "")
            logo = s.get("stream_icon") or ""
            f.write(
                f'#EXTINF:-1 tvg-id="{s.get("epg_channel_id") or ""}" tvg-name="{name}" '
                f'tvg-logo="{logo}" group-title="{group}",{name}\n'
                f"{base}/{user}/{pw}/{sid}\n"
            )
            written += 1

    print(f"wrote {written} channels to {args.out}")


if __name__ == "__main__":
    main()
