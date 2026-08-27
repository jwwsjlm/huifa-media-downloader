from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import requests

from app.core.cover_service import CoverExportOptions, CoverFitMode, CoverService


MAX_THUMBNAIL_BYTES = 20 * 1024 * 1024
_THUMBNAIL_SUFFIXES = (
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".avif",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
)


class DownloadThumbnailManager:
    """Own early preview download and final cover-file publication for one task."""

    def __init__(
        self,
        output_dir: str | Path,
        *,
        proxy: str = "",
        convert_jpeg: bool = False,
        jpeg_quality: int = 90,
        cancel_event: threading.Event,
        log: Callable[..., None],
    ) -> None:
        self.output_dir = Path(output_dir)
        self.proxy = str(proxy or "").strip()
        self.convert_jpeg = bool(convert_jpeg)
        self.jpeg_quality = max(50, min(int(jpeg_quality or 90), 100))
        self.cancel_event = cancel_event
        self.log = log
        self.attempted = False
        self.saved = False
        self.path = ""

    def _safe_log(
        self,
        level: str,
        category: str,
        message: str,
        **details: Any,
    ) -> None:
        try:
            self.log(level, category, message, **details)
        except Exception:
            # Cover handling is optional. A logging backend failure must not
            # change file ownership or leave a temporary download behind.
            pass

    @staticmethod
    def _safe_id(video_id: object) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]", "_", str(video_id))[:80] or "video"

    def _proxies(self) -> dict[str, str] | None:
        if not self.proxy:
            return None
        return {"http": self.proxy, "https": self.proxy}

    @staticmethod
    def _response_size(response: object) -> int | None:
        headers = getattr(response, "headers", {}) or {}
        content_type = str(headers.get("Content-Type") or "")
        content_type = content_type.split(";", 1)[0].strip().casefold()
        if content_type and not content_type.startswith("image/"):
            raise RuntimeError(f"封面响应不是图片：{content_type}")
        try:
            announced_size = int(headers.get("Content-Length") or 0)
        except (TypeError, ValueError, OverflowError):
            announced_size = 0
        if announced_size > MAX_THUMBNAIL_BYTES:
            raise RuntimeError("封面文件超过 20 MB 安全限制")
        content_encoding = str(headers.get("Content-Encoding") or "").casefold()
        if announced_size > 0 and content_encoding in {"", "identity"}:
            return announced_size
        return None

    def _write_response(
        self,
        response: object,
        temp_path: Path,
        expected_size: int | None,
    ) -> bytes:
        written = 0
        prefix = bytearray()
        with temp_path.open("wb") as stream:
            for chunk in response.iter_content(chunk_size=256 * 1024):
                if self.cancel_event.is_set():
                    raise InterruptedError("用户取消封面下载")
                if not chunk:
                    continue
                next_size = written + len(chunk)
                if next_size > MAX_THUMBNAIL_BYTES:
                    raise RuntimeError("封面文件超过 20 MB 安全限制")
                persisted = stream.write(chunk)
                if persisted != len(chunk):
                    raise RuntimeError("封面文件写入不完整")
                written = next_size
                if len(prefix) < 64:
                    prefix.extend(chunk[:64 - len(prefix)])
        # Cancellation can arrive after the final chunk but before publication.
        if self.cancel_event.is_set():
            raise InterruptedError("用户取消封面下载")
        if written <= 0:
            raise RuntimeError("封面响应内容为空")
        if expected_size is not None and written != expected_size:
            raise RuntimeError(
                "封面响应内容不完整"
                f"（期望 {expected_size} 字节，实际 {written} 字节）"
            )
        return bytes(prefix)

    def _publish_preview(self, temp_path: Path, safe_id: str, prefix: bytes) -> Path:
        suffix = thumbnail_suffix(prefix)
        if not suffix:
            raise RuntimeError("无法识别封面图片格式")
        path = self.output_dir / f"{safe_id}.thumb{suffix}"
        temp_path.replace(path)
        published = self._convert_to_jpeg(path)
        self._remove_stale_preview_siblings(safe_id, published)
        return published

    def _remove_stale_preview_siblings(self, safe_id: str, keep: Path) -> None:
        keep_resolved = keep.resolve()
        for suffix in _THUMBNAIL_SUFFIXES:
            candidate = self.output_dir / f"{safe_id}.thumb{suffix}"
            try:
                if candidate.exists() and candidate.resolve() != keep_resolved:
                    candidate.unlink(missing_ok=True)
            except OSError as exc:
                self._safe_log(
                    "warning",
                    "封面",
                    "清理旧封面预览失败",
                    path=str(candidate),
                    error=str(exc),
                )

    @staticmethod
    def _discard_temp(temp_path: Path) -> None:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass

    def save_preview(self, url: str, video_id: object) -> str:
        """Download at most one early task-card preview for this worker."""
        if self.attempted:
            return self.path if self.saved else ""
        self.attempted = True
        safe_id = self._safe_id(video_id)
        temp_path = self.output_dir / f".{safe_id}.thumb.{uuid4().hex}.tmp"
        try:
            if self.cancel_event.is_set():
                raise InterruptedError("用户取消封面下载")
            with requests.get(
                str(url),
                headers={"User-Agent": "Mozilla/5.0"},
                proxies=self._proxies(),
                stream=True,
                timeout=(5, 20),
            ) as response:
                response.raise_for_status()
                expected_size = self._response_size(response)
                prefix = self._write_response(response, temp_path, expected_size)
            path = self._publish_preview(temp_path, safe_id, prefix)
            self.saved = True
            self.path = str(path)
            return self.path
        except InterruptedError:
            self._discard_temp(temp_path)
            return ""
        except Exception as exc:
            self._safe_log(
                "warning",
                "网络/代理",
                "缩略图下载失败，已使用占位图",
                error=str(exc),
            )
            self._discard_temp(temp_path)
            return ""

    @staticmethod
    def _output_stem(path: Path) -> str:
        stem = path.stem
        format_match = re.match(r"^(?P<base>.+)\.f[^.]+$", stem)
        return format_match.group("base") if format_match else stem

    def _downloaded_thumbnail(self, *media_paths: Path) -> Path | None:
        checked: set[Path] = set()
        for media_path in media_paths:
            stem = self._output_stem(media_path)
            for suffix in _THUMBNAIL_SUFFIXES:
                candidate = media_path.parent / f"{stem}{suffix}"
                if candidate in checked:
                    continue
                checked.add(candidate)
                if candidate.is_file():
                    return candidate
        return None

    def finalize(self, base: Path, video_path: Path) -> Path | None:
        """Keep one final cover named after the media and remove the preview."""
        preview = Path(self.path) if self.path else None
        thumbnail = self._downloaded_thumbnail(video_path, base)
        if thumbnail is not None:
            thumbnail = self._convert_to_jpeg(thumbnail)
        elif preview is not None and preview.is_file():
            if self.convert_jpeg:
                stem = self._output_stem(video_path)
                target = video_path.parent / f"{stem}.jpg"
                thumbnail = self._convert_to_jpeg(preview, target=target)
            else:
                thumbnail = preview

        if (
            preview is not None
            and preview.exists()
            and (thumbnail is None or preview.resolve() != thumbnail.resolve())
        ):
            try:
                preview.unlink(missing_ok=True)
            except OSError:
                pass
        self.path = str(thumbnail or "")
        return thumbnail

    def _convert_to_jpeg(self, source: Path, *, target: Path | None = None) -> Path:
        if not self.convert_jpeg:
            return source
        try:
            target = target or source.with_suffix(".jpg")
            if source.suffix.casefold() in {".jpg", ".jpeg"}:
                if source.resolve() == target.resolve():
                    return source
                source.replace(target)
                return target
            cover_service = CoverService()
            loaded = cover_service.load_local(source)
            result = cover_service.save_jpeg(
                loaded,
                target,
                CoverExportOptions(
                    width=loaded.image.width(),
                    height=loaded.image.height(),
                    quality=self.jpeg_quality,
                    fit_mode=CoverFitMode.PAD,
                    background_color="#ffffff",
                ),
                overwrite=True,
            )
            if source.resolve() != result.path.resolve():
                source.unlink(missing_ok=True)
            self._safe_log(
                "info",
                "封面",
                "下载封面已转换为 JPG",
                source_format=loaded.source_format,
                quality=self.jpeg_quality,
                output_path=str(result.path),
            )
            return result.path
        except Exception as exc:
            self._safe_log(
                "warning",
                "封面",
                "封面转换为 JPG 失败，已保留原始图片",
                source_path=str(source),
                error=str(exc),
            )
            return source


def thumbnail_suffix(prefix: bytes) -> str:
    """Return an extension matching the downloaded image bytes."""
    if prefix.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if len(prefix) >= 12 and prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        return ".webp"
    if prefix.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if prefix.startswith(b"BM"):
        return ".bmp"
    if prefix.startswith((b"II*\x00", b"MM\x00*")):
        return ".tiff"
    if len(prefix) >= 16 and prefix[4:8] == b"ftyp" and any(
        brand in prefix[8:40] for brand in (b"avif", b"avis")
    ):
        return ".avif"
    return ""


__all__ = [
    "DownloadThumbnailManager",
    "MAX_THUMBNAIL_BYTES",
    "thumbnail_suffix",
]
