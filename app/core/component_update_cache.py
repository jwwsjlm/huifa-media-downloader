from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from app.core.atomic_json import write_json_atomic


_CACHE_LOCK = threading.RLock()
_MAX_CACHE_ENTRIES = 64
_MAX_RELEASE_BODY_LENGTH = 100_000
_MAX_RELEASE_ASSETS = 100


def _release_payload_for_cache(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the bounded, credential-free release metadata used by the UI."""

    root_keys = (
        "tag_name",
        "name",
        "html_url",
        "published_at",
        "body",
        "_metadata_route",
        "_metadata_route_name",
        "_metadata_third_party",
        "_metadata_cached",
        "_metadata_warning",
    )
    stored: dict[str, Any] = {
        key: payload.get(key, "")
        for key in root_keys
        if payload.get(key) is not None
    }
    body = stored.get("body")
    if isinstance(body, str) and len(body) > _MAX_RELEASE_BODY_LENGTH:
        stored["body"] = body[:_MAX_RELEASE_BODY_LENGTH]

    assets: list[dict[str, Any]] = []
    raw_assets = payload.get("assets")
    if isinstance(raw_assets, list):
        for raw_asset in raw_assets[:_MAX_RELEASE_ASSETS]:
            if not isinstance(raw_asset, Mapping):
                continue
            asset: dict[str, Any] = {}
            for key in (
                "name",
                "size",
                "state",
                "browser_download_url",
                "digest",
                "content_type",
                "updated_at",
                "source_install",
            ):
                value = raw_asset.get(key)
                if value is not None:
                    asset[key] = value
            assets.append(asset)
    stored["assets"] = assets
    return stored


def read_component_cache(
    path: str | Path,
    repo: str,
    *,
    schema_version: int,
) -> dict[str, Any] | None:
    """Read one repository entry, ignoring corrupt or mismatched cache data."""

    normalized_repo = _normalized_repo(repo)
    if not normalized_repo:
        return None
    try:
        with _CACHE_LOCK:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(raw, Mapping) or raw.get("schema_version") != schema_version:
        return None
    entries = raw.get("entries")
    if not isinstance(entries, Mapping):
        return None
    entry = entries.get(normalized_repo)
    if (
        not isinstance(entry, Mapping)
        or _normalized_repo(entry.get("repo")) != normalized_repo
        or not isinstance(entry.get("payload"), Mapping)
    ):
        return None
    return dict(entry)


def write_component_cache(
    path: str | Path,
    repo: str,
    payload: Mapping[str, Any],
    *,
    endpoint: str,
    schema_version: int,
    ttl_seconds: float,
    response_headers: Mapping[str, Any] | None = None,
) -> bool:
    """Merge one cache entry atomically without persisting request secrets."""

    normalized_repo = _normalized_repo(repo)
    if not normalized_repo:
        return False
    headers = response_headers or {}
    now = time.time()
    entry = {
        "repo": normalized_repo,
        "endpoint": str(endpoint or "latest"),
        "payload": _release_payload_for_cache(payload),
        "etag": str(headers.get("ETag") or headers.get("etag") or "").strip(),
        "last_modified": str(
            headers.get("Last-Modified") or headers.get("last-modified") or ""
        ).strip(),
        "checked_at": now,
        "expires_at": now + max(0.0, float(ttl_seconds)),
    }
    target = Path(path)
    try:
        with _CACHE_LOCK:
            existing = _read_document(target)
            entries = existing.get("entries") if isinstance(existing, Mapping) else {}
            merged = {
                str(key): dict(value)
                for key, value in entries.items()
                if isinstance(value, Mapping)
                and isinstance(value.get("payload"), Mapping)
            } if isinstance(entries, Mapping) else {}
            merged[normalized_repo] = entry
            if len(merged) > _MAX_CACHE_ENTRIES:
                merged = dict(sorted(
                    merged.items(),
                    key=lambda item: _entry_timestamp(item[1]),
                    reverse=True,
                )[:_MAX_CACHE_ENTRIES])
            write_json_atomic(target, {
                "schema_version": schema_version,
                "entries": merged,
            })
    except (OSError, TypeError, ValueError):
        return False
    return True


def _read_document(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError, TypeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _normalized_repo(value: object) -> str:
    return str(value or "").strip().lower()


def _entry_timestamp(entry: Mapping[str, Any]) -> float:
    try:
        value = float(entry.get("checked_at") or 0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return value if math.isfinite(value) else 0.0
