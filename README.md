# Mediabox

Self-hosted media stack on `100.104.189.115`. Plex with IPTV live TV.

## Deploy

```bash
ssh oli@100.104.189.115
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

Plex will be available at **http://100.104.189.115:32400/web**

---

## IPTV Setup (after first boot)

1. Open Plex at `http://100.104.189.115:32400/web`
2. Go to **Settings → Live TV & DVR**
3. Click **Set Up Plex DVR**
4. Choose **IPTV** as the tuner type
5. Enter your M3U URL:
   ```
   http://livego.club:8080/get.php?username=qjdD0kuNEdBf&password=j17ceXEXeH5s&type=m3u_plus&output=ts
   ```
6. Plex will scan and import all channels
7. Mark favorites and they'll appear in the Live TV section

> Requires Plex Pass — already active on your account.
