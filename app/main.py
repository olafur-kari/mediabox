import asyncio
import os
import re
import secrets
import string
import time
from contextlib import asynccontextmanager
from datetime import datetime
from typing import List, Optional

import httpx

from fastapi import Cookie, Depends, FastAPI, Form, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, SQLModel, create_engine, select

from app.auth import (
    COOKIE_NAME,
    TOKEN_EXPIRE_DAYS,
    add_user,
    create_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.epg import fetch_epg, epg_refresh_loop, search_epg, epg_window_hours
from app.m3u import fetch_channels, get_cached_channels, get_cached_groups, get_channel_by_id
from app.models import CustomChannel, Favorite, ProviderChannel, RecentlyWatched, User, WatchKeyword
from app.provider import fetch_provider_channels, provider_refresh_loop, search_provider_channels

# Container default; override for a local run (see run-local.sh)
DATA_DIR = os.environ.get("MEDIABOX_DATA_DIR", "/data")
DATABASE_URL = f"sqlite:///{os.path.join(DATA_DIR, 'mediabox.db')}"
engine = create_engine(DATABASE_URL, echo=False)

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

# ── Stream concurrency limit ───────────────────────────────────────────────────
# Distinct channels that may be open at once. Threadfin buffering means any
# number of people can share one channel, so this counts channels, not viewers.
STREAM_LIMIT = int(os.environ.get("STREAM_LIMIT", "1"))
HEARTBEAT_TIMEOUT = 20  # seconds — session expires if no heartbeat received

# user_id → {"ts": last heartbeat, "channel_id": str|None, "channel_name": str}
_active_sessions: dict[int, dict] = {}


def _channels_in_use(exclude_user: int | None = None) -> dict:
    """Distinct channels currently open, as {channel_id: channel_name}."""
    return {
        s["channel_id"]: s["channel_name"]
        for uid, s in _active_sessions.items()
        if s.get("channel_id") and uid != exclude_user
    }


async def _session_expiry_loop():
    """Background task: evict sessions that haven't heartbeated recently."""
    while True:
        await asyncio.sleep(5)
        cutoff = time.monotonic() - HEARTBEAT_TIMEOUT
        expired = [uid for uid, s in _active_sessions.items() if s["ts"] < cutoff]
        for uid in expired:
            _active_sessions.pop(uid, None)
        if expired:
            print(f"[streams] Expired {len(expired)} inactive session(s)")


def get_session():
    with Session(engine) as session:
        yield session


async def _lineup_loop():
    """Load the Threadfin lineup, retrying until it answers, then refresh hourly.

    The EPG is built here too, because matching guide entries to channels requires
    a lineup to match against.
    """
    delay = 5
    while True:
        await fetch_channels()
        if get_cached_channels():
            break
        print(f"[m3u] Lineup empty — retrying in {delay}s (Threadfin may still be starting)")
        await asyncio.sleep(delay)
        delay = min(delay * 2, 60)

    await fetch_epg()

    while True:
        await asyncio.sleep(3600)
        await fetch_channels()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables
    SQLModel.metadata.create_all(engine)
    # Migrate: add columns that may not exist in older DBs
    with engine.connect() as conn:
        for col, ddl in [("last_login", "DATETIME"), ("login_count", "INTEGER DEFAULT 0")]:
            try:
                conn.exec_driver_sql(f"ALTER TABLE user ADD COLUMN {col} {ddl}")
                conn.commit()
            except Exception:
                pass  # Column already exists
    # Threadfin needs ~25s to load its playlist after a restart and both containers
    # start together, so the first attempt usually loses the race. Retry instead of
    # coming up with a permanently empty channel list.
    asyncio.create_task(_lineup_loop())
    # Fetch full provider channel list in background (non-blocking)
    asyncio.create_task(fetch_provider_channels(engine))
    asyncio.create_task(provider_refresh_loop(engine))
    asyncio.create_task(epg_refresh_loop())
    # Expire stale stream sessions
    asyncio.create_task(_session_expiry_loop())
    yield


app = FastAPI(lifespan=lifespan)

# Serve static assets
app.mount("/static", StaticFiles(directory=os.path.join(FRONTEND_DIR, "static")), name="static")


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _check_auth(mb_token: Optional[str]) -> Optional[dict]:
    """Return user dict if authenticated, else None."""
    if not mb_token:
        return None
    from app.auth import decode_token
    payload = decode_token(mb_token)
    if not payload:
        return None
    return {"user_id": int(payload["sub"]), "username": payload["username"]}


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    with open(os.path.join(FRONTEND_DIR, "index.html")) as f:
        return HTMLResponse(f.read())


@app.get("/", response_class=HTMLResponse)
async def root(mb_token: Optional[str] = Cookie(default=None)):
    user = _check_auth(mb_token)
    if not user:
        return RedirectResponse("/login", status_code=302)
    with open(os.path.join(FRONTEND_DIR, "tv.html")) as f:
        return HTMLResponse(f.read())


# ── Auth API ──────────────────────────────────────────────────────────────────

@app.post("/api/auth/login")
async def api_login(
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    session: Session = Depends(get_session),
):
    user = session.exec(select(User).where(User.username == username)).first()
    if not user or not verify_password(password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    user.last_login = datetime.utcnow()
    user.login_count = (user.login_count or 0) + 1
    session.add(user)
    session.commit()

    token = create_token(user.id, user.username)
    redirect = RedirectResponse("/", status_code=302)
    redirect.set_cookie(
        COOKIE_NAME,
        token,
        max_age=TOKEN_EXPIRE_DAYS * 86400,
        httponly=True,
        samesite="lax",
    )
    return redirect


@app.post("/api/auth/logout")
async def api_logout():
    redirect = RedirectResponse("/login", status_code=302)
    redirect.delete_cookie(COOKIE_NAME)
    return redirect


# ── User API ──────────────────────────────────────────────────────────────────

@app.get("/api/me")
async def api_me(current_user: dict = Depends(get_current_user)):
    return current_user


# ── Channels API ──────────────────────────────────────────────────────────────

@app.get("/api/channels")
async def api_channels(current_user: dict = Depends(get_current_user)):
    return {"groups": get_cached_groups()}


def _pick_best_url(streams: list) -> str:
    return streams[0]["url"]


def _resolve_threadfin_url(url: str) -> str:
    """Replace localhost with the Threadfin host so the server can reach it."""
    threadfin_url = os.environ.get("THREADFIN_URL", "http://100.113.186.78:34400")
    return url.replace("http://localhost:34400", threadfin_url)


@app.get("/api/stream/{channel_id}")
async def api_stream(channel_id: str, stream_idx: int = 0, current_user: dict = Depends(get_current_user)):
    if channel_id.startswith("custom-"):
        return {"url": f"/proxy/stream/{channel_id}", "channel_id": channel_id}
    ch = get_channel_by_id(channel_id)
    if not ch:
        raise HTTPException(status_code=404, detail="Channel not found")
    streams = ch.get("streams", [])
    if not streams:
        raise HTTPException(status_code=404, detail="No streams available")
    if stream_idx >= len(streams):
        stream_idx = 0
    return {"url": f"/proxy/stream/{channel_id}?stream_idx={stream_idx}", "channel_id": channel_id}


@app.get("/api/active-users")
async def api_active_users(current_user: dict = Depends(get_current_user)):
    in_use = _channels_in_use()
    return {
        "count": len(in_use),
        "viewers": len(_active_sessions),
        "limit": STREAM_LIMIT,
        "channels": sorted(set(in_use.values())),
    }


@app.post("/api/stream/start")
async def api_stream_start(
    channel_id: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
):
    """Claim a viewing slot.

    The subscription limits *provider connections*, not viewers. Threadfin buffers a
    channel once and fans it out, so everyone watching the same channel costs one
    connection — the limit therefore counts distinct channels. Admins bypass it.
    """
    user_id = current_user["user_id"]
    is_admin = current_user.get("is_admin", False)

    ch = get_channel_by_id(channel_id) if channel_id else None
    channel_name = ch["name"] if ch else "óþekkt rás"

    # What everyone *else* has open. Switching channels frees the one we were on.
    others = _channels_in_use(exclude_user=user_id)

    if channel_id not in others and len(others) >= STREAM_LIMIT and not is_admin:
        in_use = ", ".join(sorted(set(others.values())))
        plural = "" if STREAM_LIMIT == 1 else "s"
        raise HTTPException(
            status_code=409,
            detail=(
                f"Already watching: {in_use}. This subscription allows {STREAM_LIMIT} "
                f"channel{plural} at a time — switch to that channel to watch along, "
                f"or wait until it is free."
            ),
        )

    _active_sessions[user_id] = {
        "ts": time.monotonic(),
        "channel_id": channel_id,
        "channel_name": channel_name,
    }
    in_use_now = _channels_in_use()
    print(f"[streams] user {user_id} → {channel_name} ({len(in_use_now)}/{STREAM_LIMIT} channels open)")
    return {"ok": True, "active": len(in_use_now), "limit": STREAM_LIMIT}


@app.post("/api/stream/heartbeat")
async def api_stream_heartbeat(current_user: dict = Depends(get_current_user)):
    user_id = current_user["user_id"]
    if user_id in _active_sessions:
        _active_sessions[user_id]["ts"] = time.monotonic()
    return {"ok": True}


@app.post("/api/stream/stop")
async def api_stream_stop(current_user: dict = Depends(get_current_user)):
    user_id = current_user["user_id"]
    _active_sessions.pop(user_id, None)
    print(f"[streams] user {user_id} stopped ({len(_channels_in_use())}/{STREAM_LIMIT} channels open)")
    return {"ok": True}


@app.get("/proxy/stream/{channel_id}")
async def proxy_stream(request: Request, channel_id: str, stream_idx: int = 0, mb_token: Optional[str] = Cookie(default=None)):
    user = _check_auth(mb_token)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")

    # Custom channel IDs are prefixed with "custom-"
    if channel_id.startswith("custom-"):
        custom_id = int(channel_id[7:])
        with Session(engine) as s:
            custom_ch = s.get(CustomChannel, custom_id)
        if not custom_ch:
            raise HTTPException(status_code=404, detail="Channel not found")
        url = custom_ch.url
    else:
        ch = get_channel_by_id(channel_id)
        if not ch:
            raise HTTPException(status_code=404, detail="Channel not found")
        streams = ch.get("streams", [])
        if not streams or stream_idx >= len(streams):
            raise HTTPException(status_code=404, detail="Stream not found")
        url = _resolve_threadfin_url(streams[stream_idx]["url"])

    async def stream_generator():
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                async for chunk in resp.aiter_bytes(chunk_size=65536):
                    if await request.is_disconnected():
                        break
                    yield chunk

    # Detect content type from URL
    content_type = "video/MP2T"
    if ".m3u8" in url:
        content_type = "application/vnd.apple.mpegurl"

    return StreamingResponse(
        stream_generator(),
        media_type=content_type,
        headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
    )


# ── Favorites API ─────────────────────────────────────────────────────────────

@app.get("/api/favorites")
async def api_get_favorites(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    user_id = current_user["user_id"]
    favs = session.exec(select(Favorite).where(Favorite.user_id == user_id)).all()
    return [f.channel_id for f in favs]


@app.post("/api/favorites/{channel_id}")
async def api_toggle_favorite(
    channel_id: str,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    user_id = current_user["user_id"]
    existing = session.exec(
        select(Favorite).where(
            Favorite.user_id == user_id,
            Favorite.channel_id == channel_id,
        )
    ).first()

    if existing:
        session.delete(existing)
        session.commit()
        return {"action": "removed", "channel_id": channel_id}
    else:
        fav = Favorite(user_id=user_id, channel_id=channel_id)
        session.add(fav)
        session.commit()
        return {"action": "added", "channel_id": channel_id}


# ── Recently Watched API ──────────────────────────────────────────────────────

@app.get("/api/recent")
async def api_get_recent(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    user_id = current_user["user_id"]
    recents = session.exec(
        select(RecentlyWatched)
        .where(RecentlyWatched.user_id == user_id)
        .order_by(RecentlyWatched.watched_at.desc())
        .limit(10)
    ).all()
    return [r.channel_id for r in recents]


@app.post("/api/recent/{channel_id}")
async def api_add_recent(
    channel_id: str,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    user_id = current_user["user_id"]

    # Remove existing entry for this channel if any
    existing = session.exec(
        select(RecentlyWatched).where(
            RecentlyWatched.user_id == user_id,
            RecentlyWatched.channel_id == channel_id,
        )
    ).first()
    if existing:
        session.delete(existing)
        session.commit()

    # Add new entry
    entry = RecentlyWatched(user_id=user_id, channel_id=channel_id, watched_at=datetime.utcnow())
    session.add(entry)
    session.commit()

    # Keep only last 10 per user
    all_recent = session.exec(
        select(RecentlyWatched)
        .where(RecentlyWatched.user_id == user_id)
        .order_by(RecentlyWatched.watched_at.desc())
    ).all()
    if len(all_recent) > 10:
        for old in all_recent[10:]:
            session.delete(old)
        session.commit()

    return {"action": "added", "channel_id": channel_id}


# ── Custom Channels API ───────────────────────────────────────────────────────

@app.get("/api/custom-channels")
async def api_get_custom_channels(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    channels = session.exec(select(CustomChannel)).all()
    return [{"id": c.id, "name": c.name, "url": c.url} for c in channels]


@app.post("/api/custom-channels")
async def api_add_custom_channel(
    name: str = Form(...),
    url: str = Form(...),
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    ch = CustomChannel(name=name, url=url, added_by=current_user["user_id"])
    session.add(ch)
    session.commit()
    session.refresh(ch)
    return {"id": ch.id, "name": ch.name, "url": ch.url}


@app.delete("/api/custom-channels/{channel_id}")
async def api_delete_custom_channel(
    channel_id: int,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    ch = session.get(CustomChannel, channel_id)
    if not ch:
        raise HTTPException(status_code=404, detail="Not found")
    session.delete(ch)
    session.commit()
    return {"action": "deleted"}


# ── EPG Search API ────────────────────────────────────────────────────────────

@app.get("/api/epg/search")
async def api_epg_search(q: str, current_user: dict = Depends(get_current_user)):
    if len(q) < 2:
        return []
    channels_by_id = {ch["id"]: ch for ch in get_cached_channels()}
    return search_epg(q, channels_by_id)


# ── Provider Search API ───────────────────────────────────────────────────────

@app.get("/api/provider-search")
async def api_provider_search(
    q: str,
    current_user: dict = Depends(get_current_user),
):
    if len(q) < 2:
        return []
    return search_provider_channels(engine, q)


# ── Watchlist / Schedule API ──────────────────────────────────────────────────

# Event channels carry their kick-off in the name: "... (2026-09-12 23:50:34)"
_EVENT_TIME_RE = re.compile(r"\((\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?::\d{2})?)\)")


@app.get("/api/watchlist")
async def api_get_watchlist(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    rows = session.exec(
        select(WatchKeyword).where(WatchKeyword.user_id == current_user["user_id"])
    ).all()
    return [{"id": r.id, "keyword": r.keyword} for r in rows]


@app.post("/api/watchlist")
async def api_add_watchlist(
    keyword: str,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    keyword = keyword.strip()
    if len(keyword) < 2:
        raise HTTPException(status_code=400, detail="Keyword must be at least 2 characters.")
    user_id = current_user["user_id"]
    existing = session.exec(
        select(WatchKeyword).where(
            WatchKeyword.user_id == user_id,
            WatchKeyword.keyword == keyword,
        )
    ).first()
    if existing:
        return {"id": existing.id, "keyword": existing.keyword}
    row = WatchKeyword(user_id=user_id, keyword=keyword)
    session.add(row)
    session.commit()
    session.refresh(row)
    return {"id": row.id, "keyword": row.keyword}


@app.delete("/api/watchlist/{keyword_id}")
async def api_delete_watchlist(
    keyword_id: int,
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    row = session.get(WatchKeyword, keyword_id)
    if not row or row.user_id != current_user["user_id"]:
        raise HTTPException(status_code=404, detail="Not found")
    session.delete(row)
    session.commit()
    return {"ok": True}


@app.get("/api/schedule")
async def api_schedule(
    current_user: dict = Depends(get_current_user),
    session: Session = Depends(get_session),
    q: Optional[str] = None,
):
    """What's coming up for the saved keywords (or a one-off `q`).

    Two sources, because fixtures live in two places: the TV guide, which has proper
    start times, and event/PPV channel names, which spell the fixture out but only
    sometimes carry a time.
    """
    if q:
        keywords = [q.strip()]
    else:
        keywords = [
            r.keyword for r in session.exec(
                select(WatchKeyword).where(WatchKeyword.user_id == current_user["user_id"])
            ).all()
        ]

    channels_by_id = {ch["id"]: ch for ch in get_cached_channels()}
    programmes: list[dict] = []
    events: list[dict] = []
    seen_prog: set = set()
    seen_event: set = set()

    for kw in keywords:
        for hit in search_epg(kw, channels_by_id, limit=100):
            key = (hit["channel_id"], hit["start"], hit["title"])
            if key in seen_prog:
                continue
            seen_prog.add(key)
            programmes.append({**hit, "keyword": kw})

        for ch in search_provider_channels(engine, kw, limit=40):
            if ch["name"] in seen_event:
                continue
            seen_event.add(ch["name"])
            m = _EVENT_TIME_RE.search(ch["name"])
            events.append({
                "keyword": kw,
                "name": ch["name"],
                "group": ch["group"],
                "url": ch["url"],
                "listed_time": m.group(1) if m else None,
            })

    programmes.sort(key=lambda x: x["start"])
    events.sort(key=lambda x: (x["listed_time"] is None, x["listed_time"] or "", x["name"]))

    return {
        "window_hours": epg_window_hours(),
        "keywords": keywords,
        "programmes": programmes,
        "events": events,
    }


# ── Admin API ─────────────────────────────────────────────────────────────────

def _require_admin(current_user: dict = Depends(get_current_user)):
    if not current_user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin only")
    return current_user


def _generate_password(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


@app.get("/api/admin/users")
async def api_admin_list_users(
    current_user: dict = Depends(_require_admin),
    session: Session = Depends(get_session),
):
    users = session.exec(select(User)).all()
    return [
        {
            "id": u.id,
            "username": u.username,
            "is_admin": u.is_admin,
            "created_at": u.created_at.strftime("%Y-%m-%d") if u.created_at else None,
            "last_login": u.last_login.strftime("%Y-%m-%d %H:%M") if u.last_login else "Never",
            "login_count": u.login_count or 0,
        }
        for u in users
    ]


@app.post("/api/admin/users")
async def api_admin_create_user(
    username: str = Form(...),
    current_user: dict = Depends(_require_admin),
    session: Session = Depends(get_session),
):
    existing = session.exec(select(User).where(User.username == username)).first()
    if existing:
        raise HTTPException(status_code=400, detail="Username already taken")
    password = _generate_password()
    user = add_user(session, username, password)
    return {"id": user.id, "username": user.username, "password": password}


@app.delete("/api/admin/users/{user_id}")
async def api_admin_delete_user(
    user_id: int,
    current_user: dict = Depends(_require_admin),
    session: Session = Depends(get_session),
):
    user = session.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == current_user["user_id"]:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    session.delete(user)
    session.commit()
    return {"action": "deleted"}
