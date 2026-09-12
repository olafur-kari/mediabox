# Mediabox

Self-hosted media stack on `100.113.186.78` (Tailscale) / `192.168.0.34` (LAN).
Plex, plus mediabox for IPTV live TV.

## Deploy

```bash
ssh oli@100.113.186.78
cd ~/mediabox
git pull
```

Create media directories (first time only):

```bash
mkdir -p ~/media/movies ~/media/tv
mkdir -p config/plex
```

Set up your env file:

```bash
cp .env.example .env
```

Edit `.env` — grab a fresh claim token from https://www.plex.tv/claim right before running:

```bash
nano .env
```

Start Plex:

```bash
docker compose up -d
```

Plex will be available at **http://100.113.186.78:32400/web**

---

## IPTV Setup

Channels come from **Threadfin**, which pulls the provider playlist and serves a
filtered lineup that mediabox reads.

- Threadfin admin: `http://100.113.186.78:34400/web`
- Sources live under **Settings → Files → M3U**
- Category filters under **Settings → Filter** decide which channels reach the lineup

Mediabox itself talks to the provider separately for two things the lineup doesn't
cover — the search index and the TV guide. Both are configured in `.env`:

```
M3U_URL=http://<host>/get.php?username=<user>&password=<pass>&type=m3u_plus&output=ts
EPG_URL=http://<host>/xmltv.php?username=<user>&password=<pass>
STREAM_LIMIT=1          # distinct channels open at once — must match the subscription
EPG_WINDOW_HOURS=72     # how far ahead the guide and watchlist look
```

`M3U_URL` and `EPG_URL` accept a comma-separated list, so a second provider can be
added without a code change.

> **Do not shorten the refresh intervals.** Providers rate-limit playlist downloads
> hard — repeated `get.php` calls return HTTP 884 and then ban the IP. The search
> index uses the provider's JSON API instead precisely to avoid this; Threadfin
> refreshes once a day at 00:00.
