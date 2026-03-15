# Mediabox Plan

## Repo
- Local: `/Users/olafurkari/Repos/mediabox`
- Same server as Pulse: `ssh oli@100.104.189.115`, project dir `~/mediabox`


## Goal
Self-hosted media stack on the same server as Pulse (100.104.189.115).
Two things in one: automated movie/TV downloads + IPTV live TV with favorites.

## Stack
- **Plex** — watching (movies, shows, IPTV live TV with favorites)
- **Radarr** — movie automation (find, download, organize)
- **Sonarr** — TV show automation
- **Prowlarr** — torrent indexer aggregator (feeds Radarr/Sonarr)
- **qBittorrent** — downloader with web UI
- All in Docker Compose, new directory `~/mediabox` on server, separate from Pulse

## Server
- Same machine as Pulse: `100.104.189.115` (Tailscale), `ssh oli@100.104.189.115`
- Already running Docker + Docker Compose

## Plex
- User has Plex Pass ($5/month) — required for Live TV / IPTV
- IPTV source: M3U URL + Xtream Codes (both available)
- Live TV setup: point Plex at M3U → proper channel guide + favorites UI

## Still needed before building
- [ ] Plex claim token (get from plex.tv/claim — expires in 4 min, grab when ready to deploy)
- [ ] M3U URL from IPTV provider
- [ ] Confirm media storage path on server (e.g. /home/oli/media or mounted drive)
