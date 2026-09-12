from datetime import datetime
from typing import Optional
from sqlmodel import SQLModel, Field


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True)
    hashed_password: str
    is_admin: bool = Field(default=False)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_login: Optional[datetime] = Field(default=None)
    login_count: int = Field(default=0)


class Favorite(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    channel_id: str


class RecentlyWatched(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    channel_id: str
    watched_at: datetime = Field(default_factory=datetime.utcnow)


class CustomChannel(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    url: str
    added_by: int = Field(index=True)  # user_id
    created_at: datetime = Field(default_factory=datetime.utcnow)


class ProviderChannel(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    name_normalized: str = Field(index=True)  # accent-stripped lowercase for search
    group: str
    url: str


class WatchKeyword(SQLModel, table=True):
    """A saved interest — "Liverpool", "US Open", a player's name — that the
    watchlist turns into a schedule of what's on and where."""
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    keyword: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class StreamQuality(SQLModel, table=True):
    """Measured video resolution for one provider stream.

    Providers label streams UHD/FHD with no relation to reality — on dnstream the
    same "UHD" tag sits on both 1280x720 and 1920x1080 feeds. Keyed by the lineup's
    GuideName, filled in by scripts/probe-quality.py.
    """
    guide_name: str = Field(primary_key=True)
    width: int = 0
    height: int = 0
    codec: str = ""
    measured_at: datetime = Field(default_factory=datetime.utcnow)
