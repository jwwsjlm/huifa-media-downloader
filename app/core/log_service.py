from __future__ import annotations

import json
import math
import threading
import time
import zipfile
from collections import defaultdict
from collections.abc import Mapping
from datetime import datetime
from itertools import islice
from pathlib import Path
from typing import Any

from app.core.paths import data_dir
from app.core.filename_rules import stable_ascii_component
from app.core.redaction import redact_secret_text, redact_url as redact_safe_url


class DownloadLogService:
    """Append-only per-task diagnostic logs for download troubleshooting."""

    _lock = threading.RLock()
    max_log_bytes = 5 * 1024 * 1024
    max_event_bytes = 256 * 1024
    max_collection_items = 200
    max_value_depth = 8
    max_text_chars = 2000
    flush_bytes = 64 * 1024
    flush_interval_seconds = 1.0

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root else data_dir() / "logs" / "downloads"
        self.root.mkdir(parents=True, exist_ok=True)
        self._buffers: dict[str, list[str]] = defaultdict(list)
        self._buffer_bytes: dict[str, int] = defaultdict(int)
        self._last_flush_at: dict[str, float] = {}

    def path_for(self, task_id: str) -> Path:
        safe_id = stable_ascii_component(
            task_id,
            fallback="task",
            digest_threshold=80,
            max_length=73,
        )
        return self.root / f"{safe_id}.jsonl"

    def clear(self, task_id: str) -> None:
        task_key = str(task_id)
        with self._lock:
            self._buffers.pop(task_key, None)
            self._buffer_bytes.pop(task_key, None)
            self._last_flush_at.pop(task_key, None)
            try:
                self.path_for(task_key).unlink(missing_ok=True)
            except OSError:
                pass

    def write(self, task_id: str, level: str, category: str, message: str, **details: Any) -> None:
        try:
            event = {
                "time": datetime.now().isoformat(timespec="seconds"),
                "level": self._safe_text(level, limit=64),
                "category": self._safe_text(category, limit=128),
                "message": self._safe_text(
                    message,
                    limit=self.max_text_chars,
                ),
            }
            if details:
                event["details"] = self._sanitize(details)
            line = json.dumps(
                event,
                ensure_ascii=False,
                allow_nan=False,
            ) + "\n"
            if len(line.encode("utf-8")) > self.max_event_bytes:
                event["details"] = {"truncated": True}
                line = json.dumps(
                    event,
                    ensure_ascii=False,
                    allow_nan=False,
                ) + "\n"
        except Exception as exc:
            line = json.dumps({
                "time": datetime.now().isoformat(timespec="seconds"),
                "level": "warning",
                "category": "日志",
                "message": "日志事件序列化失败",
                "details": {"error_type": type(exc).__name__},
            }, ensure_ascii=False) + "\n"
        task_key = str(task_id)
        with self._lock:
            self._buffers[task_key].append(line)
            self._buffer_bytes[task_key] += len(line.encode("utf-8"))
            now = time.monotonic()
            last_flush_at = self._last_flush_at.setdefault(task_key, now)
            should_flush = (
                str(level).casefold() in {"warning", "error"}
                or self._buffer_bytes[task_key] >= self.flush_bytes
                or now - last_flush_at >= self.flush_interval_seconds
            )
            if should_flush:
                self._flush_locked(task_key, now)

    def _flush_locked(self, task_id: str, now: float | None = None) -> None:
        lines = self._buffers.get(task_id)
        if not lines:
            return
        payload = "".join(lines)
        try:
            path = self.path_for(task_id)
            existing_size = path.stat().st_size if path.exists() else 0
            incoming_size = len(payload.encode("utf-8"))
            if existing_size + incoming_size >= self.max_log_bytes and path.exists():
                self._trim_existing_log(path)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(payload)
        except OSError:
            # Logging must never break a download. Drop this batch so a
            # permanently unavailable path cannot grow memory without bound.
            pass
        finally:
            self._buffers.pop(task_id, None)
            self._buffer_bytes.pop(task_id, None)
            self._last_flush_at[task_id] = now if now is not None else time.monotonic()

    def _trim_existing_log(self, path: Path) -> None:
        keep_bytes = max(1, self.max_log_bytes // 2)
        with path.open("rb") as source:
            source.seek(0, 2)
            start = max(0, source.tell() - keep_bytes)
            source.seek(start)
            tail = source.read(keep_bytes)
        if start:
            newline = tail.find(b"\n")
            tail = tail[newline + 1:] if newline >= 0 else b""
        with path.open("wb") as target:
            target.write(tail)

    def flush(self, task_id: str | None = None) -> None:
        """Write pending log batches without exposing buffering to readers."""
        with self._lock:
            if task_id is not None:
                self._flush_locked(str(task_id))
                return
            for task_key in tuple(self._buffers):
                self._flush_locked(task_key)

    def close(self) -> None:
        self.flush()

    def read(self, task_id: str) -> list[dict[str, Any]]:
        self.flush(task_id)
        path = self.path_for(task_id)
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        with self._lock:
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        value = {"time": "", "level": "warning", "category": "日志", "message": line}
                    if isinstance(value, dict):
                        events.append(value)
            except OSError:
                return []
        return events

    def render(self, task_id: str) -> str:
        events = self.read(task_id)
        if not events:
            return "暂无日志。"
        lines: list[str] = []
        for event in events:
            details = event.get("details") or {}
            suffix = "  " + json.dumps(details, ensure_ascii=False) if details else ""
            lines.append(f"[{event.get('time', '')}] [{event.get('level', '')}] [{event.get('category', '')}] {event.get('message', '')}{suffix}")
        return "\n".join(lines)

    def export_bundle(self, destination: str | Path | None = None, summary: dict[str, Any] | None = None) -> Path:
        """Export logs and a redacted environment summary for support tickets."""
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = Path(destination) if destination else self.root.parent / f"diagnostics-{timestamp}.zip"
        target.parent.mkdir(parents=True, exist_ok=True)
        manifest = self._sanitize(summary or {})
        manifest["exported_at"] = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            for task_key in tuple(self._buffers):
                self._flush_locked(task_key)
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                for path in sorted(self.root.glob("*.jsonl")):
                    archive.write(path, arcname=f"downloads/{path.name}")
        return target

    @staticmethod
    def redact_url(url: str) -> str:
        return redact_safe_url(url)

    @staticmethod
    def classify_error(error: str) -> str:
        text = (error or "").lower()
        risk_words = (
            "captcha", "challenge", "bot", "robot", "sign in", "login", "age-restricted",
            "verification", "authentication required", "unauthorized", "401", "429", "rate limit", "too many requests", "403", "forbidden",
            "ip block", "blocked", "geo restriction", "unavailable in your country",
            "fresh cookies", "cookies are needed", "cookie is needed", "cookies required",
        )
        network_words = ("timeout", "timed out", "connection", "dns", "proxy", "network", "http error 5", "temporary failure", "name or service not known", "connection reset")
        format_words = ("requested format", "format is not available", "ffmpeg", "merge")
        validation_words = (
            "ffprobe", "媒体成品", "文件可能不完整", "文件可能已损坏", "空文件",
            "没有可播放的视频流", "没有可播放的音频流", "播放时长无效",
        )
        disk_words = (
            "no space left on", "not enough space on the disk",
            "there is not enough space", "disk quota exceeded", "enospc",
            "winerror 112", "errno 28", "errno 122", "insufficient storage",
            "下载磁盘空间不足", "磁盘空间不足", "剩余空间已低于安全阈值",
            "剩余空间已低于", "无法读取下载磁盘的剩余空间", "等待其他下载任务释放磁盘空间",
        )
        if any(word in text for word in risk_words):
            return "风控/登录"
        # A user cancelling while blocked on a disk reservation remains a
        # normal user action, not a storage failure.
        if "cancel" in text or "取消" in text:
            return "用户操作"
        if any(word in text for word in disk_words):
            return "磁盘/存储"
        if any(word in text for word in network_words):
            return "网络/代理"
        if any(word in text for word in validation_words):
            return "文件/校验"
        if any(word in text for word in format_words):
            return "格式/工具"
        return "未知"

    @staticmethod
    def _safe_text(value: Any, *, limit: int) -> str:
        try:
            return redact_secret_text(
                value,
                redact_urls=True,
                limit=limit,
            )
        except Exception:
            return f"<{type(value).__name__}>"

    @classmethod
    def _sanitize(cls, value: Any, *, _depth: int = 0) -> Any:
        if _depth >= cls.max_value_depth:
            return "<达到日志嵌套上限>"
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            try:
                items = islice(value.items(), cls.max_collection_items + 1)
                pairs = list(items)
            except Exception:
                return f"<{type(value).__name__}>"
            for index, (key, item) in enumerate(pairs):
                if index >= cls.max_collection_items:
                    result["<truncated>"] = True
                    break
                key_value = cls._safe_text(key, limit=128)
                key_text = key_value.casefold()
                if any(secret_key in key_text for secret_key in (
                    "cookie", "password", "passwd", "token",
                    "authorization", "credential", "secret",
                )):
                    continue
                if key_text in {"url", "source_url", "webpage_url"} and isinstance(item, str):
                    result[key_value] = redact_safe_url(item)
                else:
                    result[key_value] = cls._sanitize(item, _depth=_depth + 1)
            return result
        if isinstance(value, (list, tuple, set, frozenset)):
            items = list(islice(iter(value), cls.max_collection_items + 1))
            sanitized = [
                cls._sanitize(item, _depth=_depth + 1)
                for item in items[:cls.max_collection_items]
            ]
            if len(items) > cls.max_collection_items:
                sanitized.append("<truncated>")
            return sanitized
        if isinstance(value, bytes):
            return f"<{len(value)} bytes>"
        if isinstance(value, Path):
            return cls._safe_text(value, limit=cls.max_text_chars)
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else str(value)
        if isinstance(value, str):
            return cls._safe_text(value, limit=cls.max_text_chars)
        return cls._safe_text(value, limit=cls.max_text_chars)
