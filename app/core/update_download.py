from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import requests
from PySide6.QtCore import QObject, QThread, Signal, Slot

from app.core.github_mirrors import is_public_http_url


_GITHUB_DOWNLOAD_HOSTS = frozenset({
    "codeload.github.com",
    "github.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
    "release-assets.githubusercontent.com",
})


def is_supported_github_download_url(url: str) -> bool:
    """Return whether *url* is an official GitHub release attachment URL."""
    try:
        parsed = urlsplit(str(url or "").strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold()
    if parsed.scheme.casefold() != "https" or host not in _GITHUB_DOWNLOAD_HOSTS:
        return False
    if parsed.username or parsed.password:
        return False
    if host == "github.com" and "/releases/download/" not in parsed.path:
        return False
    return True


def is_supported_github_source_archive_url(url: str) -> bool:
    """Return whether *url* is a GitHub source snapshot pinned to a commit."""
    try:
        parsed = urlsplit(str(url or "").strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").casefold()
    if parsed.scheme.casefold() != "https" or parsed.username or parsed.password:
        return False
    if host == "github.com":
        return re.fullmatch(
            r"/[^/]+/[^/]+/archive/[0-9a-fA-F]{40}\.zip",
            parsed.path,
        ) is not None
    if host == "codeload.github.com":
        return re.fullmatch(
            r"/[^/]+/[^/]+/zip/[0-9a-fA-F]{40}",
            parsed.path.rstrip("/"),
        ) is not None
    return False


def normalize_expected_download_size(value: object) -> int | None:
    """Normalize an optional release-asset size without accepting bad metadata."""
    if value is None or str(value).strip() == "":
        return None
    try:
        size = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("更新资源大小无效") from exc
    if size <= 0:
        raise ValueError("更新资源大小无效")
    return size


@dataclass(frozen=True, slots=True)
class DownloadedAssetReceipt:
    size: int
    sha256: str

    def validate(
        self,
        path: Path,
        *,
        expected_size: int | None,
        expected_sha256: str,
    ) -> None:
        try:
            persisted_size = path.stat().st_size
        except OSError as exc:
            raise RuntimeError("更新资源临时文件不可用") from exc
        if self.size <= 0 or persisted_size <= 0:
            raise RuntimeError("更新资源为空，未替换本地文件")
        if persisted_size != self.size:
            raise RuntimeError("更新资源写入不完整")
        if expected_size is not None and self.size != expected_size:
            raise RuntimeError(
                "更新资源大小校验失败"
                f"（期望 {expected_size} 字节，实际 {self.size} 字节）"
            )
        if expected_sha256 and self.sha256.casefold() != expected_sha256.casefold():
            raise RuntimeError("更新资源 SHA-256 校验失败")


@dataclass(frozen=True, slots=True)
class _AssetDownloadCandidate:
    url: str
    name: str
    third_party: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "_AssetDownloadCandidate":
        return cls(
            url=str(value.get("url") or "").strip(),
            name=str(value.get("name") or "下载线路").strip() or "下载线路",
            third_party=bool(value.get("third_party")),
        )


class AssetDownloadWorker(QObject):
    """Download one release asset and publish it only after integrity checks."""

    finished = Signal(str)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        url: str | list[dict[str, Any]],
        target: Path,
        expected_digest: str = "",
        *,
        expected_size: object = None,
        allow_unverified_third_party: bool = False,
        allow_source_archive: bool = False,
    ):
        super().__init__()
        if isinstance(url, list):
            raw_candidates = [dict(candidate) for candidate in url]
        else:
            raw_candidates = [{
                "url": str(url),
                "name": "GitHub 直连",
                "third_party": False,
            }]
        self.candidates = tuple(
            _AssetDownloadCandidate.from_mapping(candidate)
            for candidate in raw_candidates
        )
        self.url = self.candidates[0].url if self.candidates else ""
        self.target = target
        digest = str(expected_digest or "").strip()
        self.expected_digest = digest.split(":", 1)[-1].casefold() if digest else ""
        self.expected_size = normalize_expected_download_size(expected_size)
        self.allow_unverified_third_party = bool(allow_unverified_third_party)
        self.allow_source_archive = bool(allow_source_archive)
        self._cancelled = threading.Event()
        self._publish_lock = threading.Lock()

    def cancel(self) -> None:
        # Linearize cancellation against the final atomic replace. Once this
        # method returns, an uncommitted .part file cannot become active.
        with self._publish_lock:
            self._cancelled.set()

    def _cancel_requested(self) -> bool:
        return (
            self._cancelled.is_set()
            or QThread.currentThread().isInterruptionRequested()
        )

    def _raise_if_cancelled(self) -> None:
        if self._cancel_requested():
            raise InterruptedError("更新下载已取消")

    def _validate_candidate(self, candidate: _AssetDownloadCandidate) -> None:
        if candidate.third_party:
            if not self.expected_digest and not self.allow_unverified_third_party:
                raise RuntimeError("第三方线路必须有发布方 SHA-256 才能使用")
            if not is_public_http_url(candidate.url):
                raise RuntimeError("第三方线路不是有效的公网 HTTP/HTTPS 地址")
            return
        if not is_supported_github_download_url(candidate.url) and not (
            self.allow_source_archive
            and is_supported_github_source_archive_url(candidate.url)
        ):
            raise RuntimeError("更新资源地址不是受支持的 GitHub HTTPS 下载地址")

    def _validate_response_url(
        self,
        candidate: _AssetDownloadCandidate,
        response_url: str,
    ) -> None:
        if candidate.third_party:
            if not is_public_http_url(response_url):
                raise RuntimeError("第三方线路重定向到了不安全的地址")
            return
        if not is_supported_github_download_url(response_url) and not (
            self.allow_source_archive
            and is_supported_github_source_archive_url(response_url)
        ):
            raise RuntimeError("更新资源重定向到了不受支持的下载地址")

    def _download_candidate(
        self,
        candidate: _AssetDownloadCandidate,
        temporary: Path,
    ) -> None:
        self._validate_candidate(candidate)
        hasher = hashlib.sha256()
        downloaded_size = 0
        with requests.get(
            candidate.url,
            headers={"User-Agent": "HuifaVideoDownloader"},
            stream=True,
            timeout=30,
        ) as response:
            final_url = str(getattr(response, "url", "") or candidate.url)
            self._validate_response_url(candidate, final_url)
            response.raise_for_status()
            with temporary.open("wb") as handle:
                for chunk in response.iter_content(chunk_size=1024 * 256):
                    self._raise_if_cancelled()
                    if not chunk:
                        continue
                    written = handle.write(chunk)
                    if written != len(chunk):
                        raise RuntimeError("更新资源写入不完整")
                    downloaded_size += written
                    hasher.update(chunk)
        self._raise_if_cancelled()
        DownloadedAssetReceipt(
            size=downloaded_size,
            sha256=hasher.hexdigest(),
        ).validate(
            temporary,
            expected_size=self.expected_size,
            expected_sha256=self.expected_digest,
        )

    def _publish_download(self, temporary: Path) -> None:
        # Cancellation and publication share one lock so a cancellation that
        # has already returned cannot lose a race to Path.replace().
        with self._publish_lock:
            self._raise_if_cancelled()
            temporary.replace(self.target)

    @staticmethod
    def _failure_message(failures: list[tuple[str, str]]) -> str:
        if len(failures) == 1:
            return failures[0][1]
        return (
            "；".join(f"{name}：{error}" for name, error in failures)
            or "没有可用的更新下载线路"
        )

    @staticmethod
    def _remove_temporary(temporary: Path) -> None:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass

    @Slot()
    def run(self) -> None:
        temporary = self.target.with_name(self.target.name + ".part")
        try:
            self._raise_if_cancelled()
            self.target.parent.mkdir(parents=True, exist_ok=True)
            failures: list[tuple[str, str]] = []
            for candidate in self.candidates:
                self._raise_if_cancelled()
                try:
                    temporary.unlink(missing_ok=True)
                    self._download_candidate(candidate, temporary)
                    self._publish_download(temporary)
                    self.finished.emit(str(self.target))
                    return
                except InterruptedError:
                    raise
                except Exception as exc:
                    failures.append((candidate.name, str(exc)))
            self._raise_if_cancelled()
            raise RuntimeError(self._failure_message(failures))
        except InterruptedError:
            self._remove_temporary(temporary)
            self.cancelled.emit()
        except Exception as exc:
            self._remove_temporary(temporary)
            self.failed.emit(str(exc))
