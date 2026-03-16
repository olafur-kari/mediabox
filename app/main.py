import asyncio
import os
import secrets
import string
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
from app.epg import fetch_epg, epg_refresh_loop, search_epg
from app.m3u import fetch_channels, get_cached_channels, get_cached_groups, get_channel_by_id
from app.models import CustomChannel, Favorite, ProviderChannel, RecentlyWatched, User
from app.provider import fetch_provider_channels, provider_refresh_loop, search_provider_channels

DATABASE_URL = "sqlite:////data/mediabox.db"
engine = create_engine(DATABASE_URL, echo=False)

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")


def get_session():
    with Session(engine) as session:
        yield session


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables
    SQLModel.metadata.create_all(engine)
    # Fetch channels from Threadfin
    await fetch_channels()
    # Fetch full provider channel list in background (non-blocking)
    asyncio.create_task(fetch_provider_channels(engine))
    asyncio.create_task(provider_refresh_loop(engine))
    # Fetch EPG programme data in background
    asyncio.create_task(fetch_epg())
    asyncio.create_task(epg_refresh_loop())
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
    threadfin_url = os.environ.get("THREADFIN_URL", "http://100.104.189.115:34400")
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


@app.get("/proxy/stream/{channel_id}")
async def proxy_stream(channel_id: str, stream_idx: int = 0, mb_token: Optional[str] = Cookie(default=None)):
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
        {"id": u.id, "username": u.username, "is_admin": u.is_admin,
         "created_at": u.created_at.strftime("%Y-%m-%d") if u.created_at else None}
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
