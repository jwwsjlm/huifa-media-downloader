from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class MediaItem:
    id: int | None = None
    source_url: str = ""
    source_platform: str = "youtube"
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    uploader: str = ""
    thumbnail_path: str = ""
    video_path: str = ""
    metadata_json_path: str = ""
    source_ip: str = ""
    proxy_profile: str = ""
    downloaded_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    sha256: str = ""


@dataclass
class PublishTask:
    id: int | None = None
    media_id: int = 0
    platform: str = "douyin"
    account: str = "default"
    status: str = "pending"
    title: str = ""
    description: str = ""
    topics: list[str] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""
    result: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

