from __future__ import annotations

import base64
import hashlib
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit, urlunsplit

import requests
from packaging.version import InvalidVersion, Version

from app.core.atomic_json import write_json_atomic
from app.core.version import APP_VERSION
from app.core.update_receipt import (
    INSTALL_INTENT_FILENAME,
    INSTALL_RECEIPT_FILENAME,
    record_update_install_result,
)


VELOPACK_VERSION = "1.2.0"
DEFAULT_AUTO_CHECK_INTERVAL = timedelta(hours=24)
PORTABLE_EXE_NAME = "HuifaVideoDownloader.exe"
_GITHUB_API_VERSION = "2026-03-10"
_GITHUB_DOWNLOAD_HOSTS = frozenset({
    "github.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
    "release-assets.githubusercontent.com",
})


class ApplicationUpdaterError(RuntimeError):
    """Base error for the application self-update integration."""


class UpdaterUnavailableError(ApplicationUpdaterError):
    """Velopack is not installed or its expected API is unavailable."""


class UpdaterNotManagedError(ApplicationUpdaterError):
    """The process is not running from a Velopack-managed release."""


class UpdateCheckError(ApplicationUpdaterError):
    """The update feed could not be checked."""


class UpdateDownloadError(ApplicationUpdaterError):
    """The selected update could not be downloaded and verified."""


class UpdateDownloadCancelled(UpdateDownloadError):
    """The current transfer stopped cooperatively and can be resumed later."""


class UpdateInstallError(ApplicationUpdaterError):
    """The downloaded update could not be scheduled or applied."""


class UpdateConfirmationRequired(ApplicationUpdaterError):
    """Applying an update requires explicit user confirmation."""


class _RestartPortableDownload(RuntimeError):
    """The current partial response must be discarded and retried from zero."""


@dataclass(frozen=True, slots=True)
class VelopackUpdaterConfig:
    repository: str
    prerelease: bool = False
    access_token: str | None = field(default=None, repr=False)
    channel: str | None = None
    allow_downgrade: bool = False
    maximum_deltas: int = 10

    def repository_url(self) -> str:
        return normalize_github_repository(self.repository)


@dataclass(frozen=True, slots=True)
class ApplicationUpdate:
    """UI-safe update information; the native Velopack object stays private."""

    token: str
    current_version: str
    version: str
    package_id: str
    file_name: str
    size_bytes: int
    sha256: str
    release_notes_markdown: str
    is_downgrade: bool
    is_portable: bool
    downloaded: bool = False
    delivery_kind: str = "velopack"


@dataclass(frozen=True, slots=True)
class UpdaterRuntime:
    app_id: str
    current_version: str
    is_portable: bool


@dataclass(frozen=True, slots=True)
class _PortableDownloadPlan:
    update: ApplicationUpdate
    asset: dict[str, Any]
    target: Path
    temporary: Path
    resume_manifest: Path
    expected_size: int
    expected_digest: str


@dataclass(frozen=True, slots=True)
class _PortableReleaseCandidate:
    version: Version
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _PortableReleaseAsset:
    download_url: str
    size: int
    digest: str


@dataclass(frozen=True, slots=True)
class _PortableInstallLaunch:
    staged: Path
    helper: Path
    command: tuple[str, ...]
    creation_flags: int


@dataclass(frozen=True, slots=True)
class _PortableTransferMode:
    file_mode: str
    written: int
    reset_hash: bool


@dataclass(slots=True)
class _DownloadProgress:
    callback: Callable[[int], None] | None
    cancel_callback: Callable[[], bool] | None = None
    cancel_message: str = "程序更新下载已暂停，可稍后继续"
    last_value: int = -1

    def raise_if_cancelled(self) -> None:
        if self.cancel_callback is not None and self.cancel_callback():
            raise UpdateDownloadCancelled(self.cancel_message)

    def report(self, value: Any) -> None:
        self.raise_if_cancelled()
        self._publish(value)

    def complete(self) -> None:
        """Publish terminal progress after the update is durably committed."""
        self._publish(100)

    def _publish(self, value: Any) -> None:
        try:
            progress = max(0, min(100, int(value)))
        except (TypeError, ValueError):
            return
        if progress != self.last_value and self.callback is not None:
            try:
                self.callback(progress)
            except Exception:
                # UI progress is observational. A stale or already-destroyed
                # receiver must never roll back a verified update transaction.
                pass
        self.last_value = progress


def _close_http_response(response: Any | None) -> None:
    """Best-effort connection cleanup that never replaces the real result."""

    close = getattr(response, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        # Cleanup happens after the payload was consumed, or while a more
        # useful exception is already propagating. Never hide that outcome.
        pass


def _safe_update_error(error: Exception, access_token: str | None) -> str:
    message = str(error).strip() or error.__class__.__name__
    return message.replace(access_token, "***") if access_token else message


def normalize_github_repository(value: str) -> str:
    """Return a canonical public GitHub repository URL.

    Settings may contain either ``owner/repository`` or the full HTTPS URL.
    Credentials, query strings, fragments, non-GitHub hosts and extra path
    components are rejected before they reach ``GithubSource``.
    """
    raw = str(value or "").strip().rstrip("/")
    if not raw:
        raise ValueError("GitHub 更新仓库不能为空")
    if "://" not in raw:
        if raw.count("/") != 1:
            raise ValueError("GitHub 更新仓库应为 owner/repository")
        raw = f"https://github.com/{raw}"

    try:
        parsed = urlsplit(raw)
    except ValueError as exc:
        raise ValueError("GitHub 更新仓库地址无效") from exc
    if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() != "github.com":
        raise ValueError("GitHub 更新仓库必须使用 https://github.com")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("GitHub 更新仓库地址不能包含凭据、查询参数或片段")

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise ValueError("GitHub 更新仓库应为 owner/repository")
    owner, repository = parts
    repository = re.sub(r"(?i)\.git$", "", repository)
    allowed = re.compile(r"^[A-Za-z0-9_.-]+$")
    if not owner or not repository or not allowed.fullmatch(owner) or not allowed.fullmatch(repository):
        raise ValueError("GitHub 更新仓库名称包含不支持的字符")
    return urlunsplit(("https", "github.com", f"/{owner}/{repository}", "", ""))


def is_supported_github_release_url(value: str) -> bool:
    """Accept only HTTPS URLs used by GitHub Release asset downloads."""
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https" or host not in _GITHUB_DOWNLOAD_HOSTS:
        return False
    if parsed.username or parsed.password:
        return False
    if host == "github.com" and "/releases/download/" not in parsed.path:
        return False
    return True


def portable_single_exe_supported(executable_path: str | Path | None = None) -> bool:
    """Whether this process can safely use the single-EXE replacement path."""
    executable = Path(executable_path or sys.executable)
    return bool(
        os.name == "nt"
        and getattr(sys, "frozen", False)
        and executable.suffix.casefold() == ".exe"
    )


def load_velopack() -> Any:
    try:
        module = importlib.import_module("velopack")
    except (ImportError, OSError) as exc:
        raise UpdaterUnavailableError(
            f"缺少 Velopack {VELOPACK_VERSION} 运行库；请使用 Velopack 发行构建"
        ) from exc
    required = ("App", "GithubSource", "UpdateManager", "UpdateOptions")
    missing = [name for name in required if not hasattr(module, name)]
    if missing:
        raise UpdaterUnavailableError(
            "Velopack Python API 不完整：" + "、".join(missing)
        )
    return module


def run_velopack_startup(
    velopack_module: Any | None = None,
    receipt_state_dir: str | Path | None = None,
) -> None:
    """Handle Velopack install/update hooks before normal application startup.

    The Velopack-specific PyInstaller runtime hook calls this before the main
    entry script. Auto-apply is disabled: downloading an update must never
    bypass the application's explicit install confirmation UI.
    """
    module = velopack_module or load_velopack()
    app = module.App().set_auto_apply_on_startup(False)

    def record_restart(*_args: Any) -> None:
        try:
            state_dir = (
                Path(receipt_state_dir)
                if receipt_state_dir is not None
                else _velopack_application_update_state_dir()
            )
            if state_dir is not None:
                record_update_install_result(
                    state_dir,
                    status="succeeded",
                    current_version=APP_VERSION,
                    delivery_kind="velopack",
                    message="Velopack 已完成更新并重新启动程序",
                )
        except Exception:
            # A receipt is informational. Never let a storage permission issue
            # interfere with Velopack's mandatory early-startup lifecycle.
            pass

    on_restarted = getattr(app, "on_restarted", None)
    if callable(on_restarted):
        configured = on_restarted(record_restart)
        if configured is not None:
            app = configured
    app.run()


def velopack_persistent_data_dir(executable_dir: str | Path | None = None) -> Path | None:
    """Locate update-safe data storage for a managed Windows release.

    Velopack replaces the complete ``current`` directory during every update.
    Both its installed and portable Windows layouts keep ``Update.exe`` in the
    parent directory, so persistent data must live beside ``current``, not
    beside the real application binary.
    """
    directory = Path(executable_dir or Path(sys.executable).resolve().parent).resolve()
    if directory.name.lower() != "current":
        return None
    root = directory.parent
    if not (root / "Update.exe").is_file() or not (directory / "sq.version").is_file():
        return None
    return root / "data"


def _velopack_application_update_state_dir() -> Path | None:
    persistent = velopack_persistent_data_dir()
    return persistent / "updates" / "application" if persistent is not None else None


class AutoUpdateCheckThrottle:
    """Small atomic ledger used to limit background checks to once per day."""

    def __init__(self, path: str | Path, interval: timedelta = DEFAULT_AUTO_CHECK_INTERVAL):
        self.path = Path(path)
        self.interval = interval

    def is_due(self, repository: str, now: datetime | None = None) -> bool:
        now_utc = _utc(now)
        state = self._read()
        if state.get("repository") != normalize_github_repository(repository):
            return True
        raw_checked = str(state.get("checked_at") or "")
        try:
            checked_at = _utc(datetime.fromisoformat(raw_checked))
        except (TypeError, ValueError):
            return True
        elapsed = now_utc - checked_at
        # A future timestamp usually means the system clock was corrected
        # after the previous run. Treat it as stale instead of suppressing
        # automatic checks until that future date is reached.
        return elapsed < timedelta(0) or elapsed >= self.interval

    def mark_checked(self, repository: str, now: datetime | None = None) -> None:
        payload = {
            "repository": normalize_github_repository(repository),
            "checked_at": _utc(now).isoformat(),
        }
        write_json_atomic(self.path, payload)

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}


class VelopackApplicationUpdater:
    """Synchronous, UI-independent adapter around Velopack's Python SDK.

    Check/download/apply calls perform blocking IO and should be invoked by the
    application's existing worker-thread abstraction. The adapter deliberately
    contains no Qt objects so it can be tested independently and reused by both
    the installer and self-updating portable distributions.
    """

    def __init__(self, config: VelopackUpdaterConfig, velopack_module: Any | None = None):
        self.config = config
        self._velopack = velopack_module or load_velopack()
        self._source = self._velopack.GithubSource(
            config.repository_url(),
            config.access_token,
            config.prerelease,
        )
        self._options = self._velopack.UpdateOptions(
            config.allow_downgrade,
            config.maximum_deltas,
            config.channel,
        )
        # UpdateManager resolves the Velopack manifest in its constructor and
        # therefore raises when this code is run from an IDE/source checkout.
        # Create it lazily so merely constructing Settings/UI services never
        # crashes a development or legacy onefile build.
        self._manager: Any | None = None
        self._native_updates: dict[str, Any] = {}
        self._downloaded: set[str] = set()

    def runtime(self) -> UpdaterRuntime:
        manager = self._get_manager()
        try:
            return UpdaterRuntime(
                app_id=str(manager.get_app_id() or ""),
                current_version=str(manager.get_current_version() or ""),
                is_portable=bool(manager.get_is_portable()),
            )
        except Exception as exc:
            raise UpdaterNotManagedError(
                "当前程序不是 Velopack 安装版或便携版，无法执行本程序更新"
            ) from exc

    def check_for_updates(self) -> ApplicationUpdate | None:
        runtime = self.runtime()
        manager = self._get_manager()
        try:
            native = manager.check_for_updates()
        except Exception as exc:
            message = _safe_update_error(exc, self.config.access_token)
            if "not installed" in message.lower() or "not managed" in message.lower():
                raise UpdaterNotManagedError(
                    "当前程序不是 Velopack 安装版或便携版，无法检查本程序更新"
                ) from exc
            raise UpdateCheckError(f"检查程序更新失败：{message}") from exc
        if native is None:
            return None
        return self._register(native, runtime, downloaded=False)

    def pending_restart(self) -> ApplicationUpdate | None:
        runtime = self.runtime()
        manager = self._get_manager()
        try:
            asset = manager.get_update_pending_restart()
        except Exception as exc:
            raise UpdateCheckError(
                f"读取待安装更新失败：{_safe_update_error(exc, self.config.access_token)}"
            ) from exc
        if asset is None:
            return None
        return self._register(asset, runtime, downloaded=True)

    def download_update(
        self,
        update: ApplicationUpdate,
        progress_callback: Callable[[int], None] | None = None,
        cancel_callback: Callable[[], bool] | None = None,
    ) -> ApplicationUpdate:
        native = self._native(update)
        progress = _DownloadProgress(
            progress_callback,
            cancel_callback,
            "程序更新下载已暂停，可稍后继续",
        )
        progress.report(0)
        manager = self._get_manager()
        try:
            manager.download_updates(native, progress.report)
        except UpdateDownloadCancelled:
            raise
        except Exception as exc:
            raise UpdateDownloadError(
                "下载或校验程序更新失败："
                f"{_safe_update_error(exc, self.config.access_token)}"
            ) from exc
        progress.complete()
        self._downloaded.add(update.token)
        return ApplicationUpdate(
            token=update.token,
            current_version=update.current_version,
            version=update.version,
            package_id=update.package_id,
            file_name=update.file_name,
            size_bytes=update.size_bytes,
            sha256=update.sha256,
            release_notes_markdown=update.release_notes_markdown,
            is_downgrade=update.is_downgrade,
            is_portable=update.is_portable,
            downloaded=True,
        )

    def install_and_restart(
        self,
        update: ApplicationUpdate,
        *,
        confirmed: bool,
        restart_args: Sequence[str] | None = None,
    ) -> None:
        native = self._require_downloaded(update, confirmed)
        manager = self._get_manager()
        try:
            if restart_args is None:
                manager.apply_updates_and_restart(native)
            else:
                manager.apply_updates_and_restart_with_args(native, list(restart_args))
        except Exception as exc:
            raise UpdateInstallError(
                f"安装程序更新失败：{_safe_update_error(exc, self.config.access_token)}"
            ) from exc

    def schedule_install_on_exit(
        self,
        update: ApplicationUpdate,
        *,
        confirmed: bool,
        restart: bool = True,
        restart_args: Sequence[str] | None = None,
        silent: bool = False,
    ) -> None:
        """Start the updater, then let the UI close services and exit cleanly."""
        native = self._require_downloaded(update, confirmed)
        manager = self._get_manager()
        try:
            manager.wait_exit_then_apply_updates(
                native,
                silent,
                restart,
                list(restart_args) if restart_args is not None else None,
            )
        except Exception as exc:
            raise UpdateInstallError(
                f"安排程序更新失败：{_safe_update_error(exc, self.config.access_token)}"
            ) from exc

    def _register(self, native: Any, runtime: UpdaterRuntime, downloaded: bool) -> ApplicationUpdate:
        asset = getattr(native, "TargetFullRelease", native)
        token = uuid.uuid4().hex
        self._native_updates[token] = native
        if downloaded:
            self._downloaded.add(token)
        return ApplicationUpdate(
            token=token,
            current_version=runtime.current_version,
            version=str(getattr(asset, "Version", "") or ""),
            package_id=str(getattr(asset, "PackageId", "") or ""),
            file_name=Path(str(getattr(asset, "FileName", "") or "")).name,
            size_bytes=max(0, int(getattr(asset, "Size", 0) or 0)),
            sha256=str(getattr(asset, "SHA256", "") or "").lower(),
            release_notes_markdown=str(getattr(asset, "NotesMarkdown", "") or ""),
            is_downgrade=bool(getattr(native, "IsDowngrade", False)),
            is_portable=runtime.is_portable,
            downloaded=downloaded,
        )

    def _native(self, update: ApplicationUpdate) -> Any:
        native = self._native_updates.get(update.token)
        if native is None:
            raise ApplicationUpdaterError("更新对象已失效，请重新检查更新")
        return native

    def _get_manager(self) -> Any:
        if self._manager is not None:
            return self._manager
        try:
            self._manager = self._velopack.UpdateManager(self._source, self._options)
        except Exception as exc:
            raise UpdaterNotManagedError(
                "当前程序不是 Velopack 安装版或便携版，无法执行本程序更新"
            ) from exc
        return self._manager

    def _require_downloaded(self, update: ApplicationUpdate, confirmed: bool) -> Any:
        if not confirmed:
            raise UpdateConfirmationRequired("安装程序更新前必须由用户明确确认")
        native = self._native(update)
        if update.token not in self._downloaded and not update.downloaded:
            raise UpdateInstallError("程序更新尚未下载完成")
        return native

class PortableExeApplicationUpdater:
    """GitHub Releases updater for the primary PyInstaller one-file build.

    Velopack remains the update engine for managed installer/onedir builds.
    A one-file executable is instead downloaded as an exact release asset,
    verified using GitHub's asset SHA-256 digest, staged beside the running
    executable, and replaced by a short-lived PowerShell helper only after the
    application has completed its normal cooperative shutdown.
    """

    def __init__(
        self,
        config: VelopackUpdaterConfig,
        state_dir: str | Path,
        *,
        executable_path: str | Path | None = None,
        current_version: str = APP_VERSION,
        session: Any | None = None,
        process_id: int | None = None,
    ) -> None:
        self.config = config
        self.state_dir = Path(state_dir)
        self.executable_path = Path(executable_path or sys.executable).resolve()
        self.current_version = str(current_version or APP_VERSION).strip()
        self._session = session or requests.Session()
        self._process_id = int(process_id or os.getpid())
        self._assets: dict[str, dict[str, Any]] = {}
        self._downloaded_paths: dict[str, Path] = {}
        self._pending_path = self.state_dir / "portable-pending.json"
        self._install_intent_path = self.state_dir / INSTALL_INTENT_FILENAME
        self._install_receipt_path = self.state_dir / INSTALL_RECEIPT_FILENAME

    def runtime(self) -> UpdaterRuntime:
        if os.name != "nt" or self.executable_path.suffix.casefold() != ".exe":
            raise UpdaterNotManagedError("当前程序不是可自更新的 Windows 单 EXE 便携版")
        return UpdaterRuntime(
            app_id="Huifa.VideoDownloader",
            current_version=self.current_version,
            is_portable=True,
        )

    def check_for_updates(self) -> ApplicationUpdate | None:
        self.runtime()
        repository_url = self.config.repository_url()
        owner, repository = [part for part in urlsplit(repository_url).path.split("/") if part]
        releases = self._fetch_github_releases(owner, repository)
        current = self._parse_version(self.current_version, "当前版本")
        candidate = self._select_portable_release(releases, current)
        if candidate is None:
            return None
        asset = self._portable_release_asset(candidate)

        token = uuid.uuid4().hex
        self._assets[token] = {
            "url": asset.download_url,
            "size": asset.size,
            "sha256": asset.digest,
            "version": str(candidate.version),
        }
        return ApplicationUpdate(
            token=token,
            current_version=self.current_version,
            version=str(candidate.version),
            package_id="Huifa.VideoDownloader",
            file_name=PORTABLE_EXE_NAME,
            size_bytes=asset.size,
            sha256=asset.digest,
            release_notes_markdown=str(candidate.payload.get("body") or ""),
            is_downgrade=candidate.version < current,
            is_portable=True,
            downloaded=False,
            delivery_kind="single-exe",
        )

    def _fetch_github_releases(self, owner: str, repository: str) -> list[Any]:
        if self.config.prerelease:
            api_url = f"https://api.github.com/repos/{owner}/{repository}/releases?per_page=30"
        else:
            api_url = f"https://api.github.com/repos/{owner}/{repository}/releases/latest"
        response = None
        try:
            response = self._session.get(
                api_url,
                headers=self._github_headers(),
                timeout=(10, 30),
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            raise UpdateCheckError(
                f"检查程序更新失败：{_safe_update_error(exc, self.config.access_token)}"
            ) from exc
        finally:
            _close_http_response(response)
        if self.config.prerelease:
            if not isinstance(payload, list):
                raise UpdateCheckError("GitHub Releases 返回了无效的数据格式")
            return payload
        if not isinstance(payload, dict):
            raise UpdateCheckError("GitHub 最新 Release 返回了无效的数据格式")
        return [payload]

    def _select_portable_release(
        self,
        releases: Sequence[Any],
        current: Version,
    ) -> _PortableReleaseCandidate | None:
        candidates: list[_PortableReleaseCandidate] = []
        for release in releases:
            if not isinstance(release, dict) or release.get("draft"):
                continue
            if release.get("prerelease") and not self.config.prerelease:
                continue
            raw_tag = str(release.get("tag_name") or "").strip()
            try:
                version = self._parse_version(raw_tag, "发行版本")
            except UpdateCheckError:
                continue
            if version == current:
                continue
            if not self.config.allow_downgrade and version < current:
                continue
            candidates.append(_PortableReleaseCandidate(version, release))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.version)

    def _portable_release_asset(
        self,
        candidate: _PortableReleaseCandidate,
    ) -> _PortableReleaseAsset:
        assets = candidate.payload.get("assets")
        if not isinstance(assets, list):
            assets = []
        asset = next(
            (
                item
                for item in assets
                if isinstance(item, dict)
                and str(item.get("name") or "").casefold() == PORTABLE_EXE_NAME.casefold()
                and str(item.get("state") or "uploaded").casefold() == "uploaded"
            ),
            None,
        )
        if asset is None:
            raise UpdateCheckError(
                f"GitHub 版本 {candidate.version} 缺少单 EXE 资产 {PORTABLE_EXE_NAME}"
            )
        download_url = str(asset.get("browser_download_url") or "").strip()
        if not is_supported_github_release_url(download_url):
            raise UpdateCheckError("GitHub Release 的单 EXE 下载地址不受信任")
        digest = self._digest_hex(asset.get("digest"))
        if not digest:
            raise UpdateCheckError(
                "GitHub Release 未提供单 EXE 的 SHA-256 digest，已拒绝自动更新"
            )
        try:
            size = int(asset.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size <= 0:
            raise UpdateCheckError("GitHub Release 的单 EXE 文件大小无效")
        return _PortableReleaseAsset(
            download_url=download_url,
            size=size,
            digest=digest,
        )

    def pending_restart(self) -> ApplicationUpdate | None:
        self.runtime()
        try:
            payload = json.loads(self._pending_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError):
            self._remove_pending_manifest()
            return None
        if not isinstance(payload, dict):
            self._remove_pending_manifest()
            return None
        try:
            schema_version = int(payload.get("schema_version") or 0)
        except (TypeError, ValueError):
            schema_version = 0
        file_name = str(payload.get("file_name") or "").strip()
        if (
            schema_version != 1
            or not file_name
            or file_name in {".", ".."}
            or "/" in file_name
            or "\\" in file_name
            or Path(file_name).is_absolute()
        ):
            self._remove_pending_manifest()
            return None
        source = self.state_dir / file_name
        digest = self._digest_hex(payload.get("sha256"))
        try:
            size = int(payload.get("size_bytes") or 0)
        except (TypeError, ValueError):
            size = 0
        try:
            source_in_state_dir = source.resolve().parent == self.state_dir.resolve()
        except OSError:
            source_in_state_dir = False
        if not source_in_state_dir or not source.is_file() or not digest or size <= 0:
            self._remove_pending_manifest()
            return None
        version_text = str(payload.get("version") or "").strip()
        try:
            pending_version = self._parse_version(version_text, "待安装版本")
            running_version = self._parse_version(self.current_version, "当前版本")
        except UpdateCheckError:
            self._remove_pending_manifest()
            return None
        if pending_version == running_version or (
            pending_version < running_version and not self.config.allow_downgrade
        ):
            self._remove_pending_manifest()
            try:
                source.unlink(missing_ok=True)
            except OSError:
                pass
            return None
        try:
            self._validate_download(source, digest, size)
        except UpdateDownloadError:
            self._remove_pending_manifest()
            try:
                source.unlink(missing_ok=True)
            except OSError:
                pass
            return None

        token = uuid.uuid4().hex
        self._downloaded_paths[token] = source
        return ApplicationUpdate(
            token=token,
            current_version=str(payload.get("current_version") or self.current_version),
            version=str(pending_version),
            package_id="Huifa.VideoDownloader",
            file_name=PORTABLE_EXE_NAME,
            size_bytes=size,
            sha256=digest,
            release_notes_markdown=str(payload.get("release_notes_markdown") or ""),
            is_downgrade=bool(payload.get("is_downgrade", False)),
            is_portable=True,
            downloaded=True,
            delivery_kind="single-exe",
        )

    def _portable_download_plan(self, update: ApplicationUpdate) -> _PortableDownloadPlan:
        asset = self._assets.get(update.token)
        if asset is None:
            raise ApplicationUpdaterError("更新对象已失效，请重新检查更新")
        try:
            asset_size = int(asset["size"])
            update_size = int(update.size_bytes)
        except (KeyError, TypeError, ValueError) as exc:
            raise ApplicationUpdaterError("已检查的更新资源元数据无效，请重新检查更新") from exc
        asset_digest = self._digest_hex(asset.get("sha256"))
        asset_version = str(asset.get("version") or "").strip()
        if (
            not asset_digest
            or asset_size <= 0
            or update.version != asset_version
            or update_size != asset_size
            or self._digest_hex(update.sha256) != asset_digest
            or update.file_name.casefold() != PORTABLE_EXE_NAME.casefold()
            or not update.is_portable
            or update.delivery_kind != "single-exe"
        ):
            raise ApplicationUpdaterError("更新对象与已检查的发布元数据不一致，请重新检查更新")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        safe_version = re.sub(
            r"[^A-Za-z0-9_.-]+",
            "-",
            asset_version,
        ).strip(".-") or "update"
        target = self.state_dir / f"HuifaVideoDownloader-{safe_version}.exe"
        temporary = target.with_name(target.name + ".part")
        return _PortableDownloadPlan(
            update=update,
            asset=asset,
            target=target,
            temporary=temporary,
            resume_manifest=temporary.with_name(temporary.name + ".json"),
            expected_size=asset_size,
            expected_digest=asset_digest,
        )

    def _finish_portable_download(
        self,
        plan: _PortableDownloadPlan,
        source: Path,
        progress: _DownloadProgress,
        *,
        already_validated: bool = False,
    ) -> ApplicationUpdate:
        progress.raise_if_cancelled()
        if not already_validated:
            self._validate_download(
                source,
                plan.expected_digest,
                plan.expected_size,
                cancel_check=progress.raise_if_cancelled,
            )
        downloaded = self._copy_update(plan.update, downloaded=True)
        try:
            # Publish the recovery record before renaming the complete part.
            # If the process stops between these operations, startup discards
            # the not-yet-valid manifest while the resumable full part remains.
            progress.raise_if_cancelled()
            self._write_pending_manifest(downloaded, plan.target)
            progress.raise_if_cancelled()
            if source != plan.target:
                source.replace(plan.target)
        except BaseException:
            self._remove_pending_manifest()
            raise
        self._remove_resume_download(plan.temporary, plan.resume_manifest)
        self._downloaded_paths[plan.update.token] = plan.target
        progress.complete()
        return downloaded

    def _hash_resume_prefix(
        self,
        plan: _PortableDownloadPlan,
        offset: int,
        progress: _DownloadProgress,
    ) -> Any:
        hasher = hashlib.sha256()
        if not offset:
            return hasher
        hashed = 0
        with plan.temporary.open("rb") as existing:
            while True:
                progress.raise_if_cancelled()
                chunk = existing.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
                hashed += len(chunk)
                progress.report(int(hashed * 100 / plan.expected_size))
        return hasher

    def _portable_transfer_mode(
        self,
        plan: _PortableDownloadPlan,
        response: Any,
        resume_state: dict[str, Any] | None,
        offset: int,
    ) -> _PortableTransferMode:
        status = int(getattr(response, "status_code", 200) or 200)
        if status == 416 and offset:
            raise _RestartPortableDownload("服务器拒绝了旧的断点位置")
        response.raise_for_status()
        if status not in {200, 206}:
            raise UpdateDownloadError(f"更新服务器返回了不支持的状态码：{status}")
        if status == 200:
            return _PortableTransferMode("wb", 0, bool(offset))

        content_range = self._response_header(response, "Content-Range")
        if not self._content_range_matches(
            content_range,
            offset,
            plan.expected_size,
        ):
            raise _RestartPortableDownload("服务器返回了无效的 Content-Range")
        if offset and not self._resume_validators_match(
            resume_state or {},
            response,
        ):
            raise _RestartPortableDownload("服务器上的更新文件标识已变化")
        return _PortableTransferMode("ab" if offset else "wb", offset, False)

    def _stream_portable_response(
        self,
        plan: _PortableDownloadPlan,
        response: Any,
        transfer: _PortableTransferMode,
        hasher: Any,
        progress: _DownloadProgress,
    ) -> tuple[int, str]:
        written = transfer.written
        with plan.temporary.open(transfer.file_mode) as handle:
            self._write_resume_download(
                plan.resume_manifest,
                plan.asset,
                plan.update.version,
                response,
            )
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                progress.raise_if_cancelled()
                if not chunk:
                    continue
                written += len(chunk)
                if written > plan.expected_size:
                    self._remove_resume_download(
                        plan.temporary,
                        plan.resume_manifest,
                    )
                    raise UpdateDownloadError(
                        "下载文件大小超过 GitHub Release 元数据"
                    )
                persisted = handle.write(chunk)
                if persisted != len(chunk):
                    raise OSError("更新文件写入不完整")
                hasher.update(chunk)
                progress.report(int(written * 100 / plan.expected_size))
            handle.flush()
            os.fsync(handle.fileno())
        return written, hasher.hexdigest()

    def _validate_portable_transfer(
        self,
        plan: _PortableDownloadPlan,
        written: int,
        digest: str,
    ) -> None:
        if written <= 0:
            self._remove_resume_download(plan.temporary, plan.resume_manifest)
            raise UpdateDownloadError("更新服务器未返回可下载内容")
        if written != plan.expected_size:
            raise UpdateDownloadError(
                f"下载暂时中断：应为 {plan.expected_size} 字节，"
                f"已保存 {written} 字节；再次下载将从断点继续"
            )
        if digest.casefold() != plan.expected_digest.casefold():
            self._remove_resume_download(plan.temporary, plan.resume_manifest)
            raise UpdateDownloadError("下载的单 EXE 未通过 GitHub SHA-256 校验")
        try:
            self._validate_windows_executable(plan.temporary)
        except UpdateDownloadError:
            self._remove_resume_download(plan.temporary, plan.resume_manifest)
            raise

    def _download_portable_asset(
        self,
        plan: _PortableDownloadPlan,
        resume_state: dict[str, Any] | None,
        progress: _DownloadProgress,
    ) -> None:
        last_restart_reason = ""
        for _request_attempt in range(2):
            progress.raise_if_cancelled()
            response = None
            offset = (
                plan.temporary.stat().st_size
                if resume_state and plan.temporary.is_file() else 0
            )
            if offset < 0 or offset > plan.expected_size:
                self._remove_resume_download(plan.temporary, plan.resume_manifest)
                resume_state = None
                offset = 0

            hasher = self._hash_resume_prefix(
                plan,
                offset,
                progress,
            )
            headers = self._github_headers(binary=True)
            if offset:
                headers["Range"] = f"bytes={offset}-"
                validator = self._resume_validator(resume_state or {})
                if validator:
                    headers["If-Range"] = validator
            try:
                response = self._session.get(
                    str(plan.asset["url"]),
                    headers=headers,
                    stream=True,
                    timeout=(10, 120),
                    allow_redirects=True,
                )
                final_url = str(getattr(response, "url", "") or plan.asset["url"])
                if not is_supported_github_release_url(final_url):
                    self._remove_resume_download(plan.temporary, plan.resume_manifest)
                    raise UpdateDownloadError("更新下载被重定向到了不受信任的地址")
                try:
                    transfer = self._portable_transfer_mode(
                        plan,
                        response,
                        resume_state,
                        offset,
                    )
                except _RestartPortableDownload as exc:
                    last_restart_reason = str(exc)
                    self._remove_resume_download(plan.temporary, plan.resume_manifest)
                    resume_state = None
                    continue
                if transfer.reset_hash:
                    hasher = hashlib.sha256()
                    progress.report(0)
                written, digest = self._stream_portable_response(
                    plan,
                    response,
                    transfer,
                    hasher,
                    progress,
                )
                self._validate_portable_transfer(plan, written, digest)
                return
            finally:
                _close_http_response(response)

        self._remove_resume_download(plan.temporary, plan.resume_manifest)
        raise UpdateDownloadError(
            (last_restart_reason or "服务器未能提供有效的断点响应")
            + "，已清除旧断点，请重新下载"
        )

    def download_update(
        self,
        update: ApplicationUpdate,
        progress_callback: Callable[[int], None] | None = None,
        cancel_callback: Callable[[], bool] | None = None,
    ) -> ApplicationUpdate:
        plan = self._portable_download_plan(update)
        progress = _DownloadProgress(
            progress_callback,
            cancel_callback,
            "程序更新下载已暂停，已保留进度，可稍后继续",
        )
        progress.report(0)
        try:
            # A previous process may have completed the local rename just
            # before writing the recovery manifest. Reuse that exact artifact.
            if plan.target.is_file():
                try:
                    return self._finish_portable_download(
                        plan,
                        plan.target,
                        progress,
                    )
                except UpdateDownloadCancelled:
                    raise
                except UpdateDownloadError:
                    plan.target.unlink(missing_ok=True)

            resume_state = self._load_resume_download(
                plan.temporary,
                plan.resume_manifest,
                plan.asset,
                update.version,
            )
            if resume_state and plan.temporary.stat().st_size == plan.expected_size:
                try:
                    return self._finish_portable_download(
                        plan,
                        plan.temporary,
                        progress,
                    )
                except UpdateDownloadCancelled:
                    raise
                except UpdateDownloadError:
                    self._remove_resume_download(
                        plan.temporary,
                        plan.resume_manifest,
                    )
                    resume_state = None

            self._download_portable_asset(
                plan,
                resume_state,
                progress,
            )
            return self._finish_portable_download(
                plan,
                plan.temporary,
                progress,
                already_validated=True,
            )
        except UpdateDownloadCancelled:
            raise
        except UpdateDownloadError:
            raise
        except Exception as exc:
            saved = 0
            try:
                if plan.resume_manifest.is_file() and plan.temporary.is_file():
                    saved = plan.temporary.stat().st_size
            except OSError:
                saved = 0
            if 0 < saved < plan.expected_size:
                suffix = f"；已保留 {saved} 字节，下次将从断点继续"
            elif saved == plan.expected_size:
                suffix = "；完整下载已保留，重试将直接完成登记"
            else:
                suffix = ""
            raise UpdateDownloadError(
                "下载程序更新失败："
                f"{_safe_update_error(exc, self.config.access_token)}{suffix}"
            ) from exc

    def install_and_restart(
        self,
        update: ApplicationUpdate,
        *,
        confirmed: bool,
        restart_args: Sequence[str] | None = None,
    ) -> None:
        self.schedule_install_on_exit(
            update,
            confirmed=confirmed,
            restart=True,
            restart_args=restart_args,
        )

    def schedule_install_on_exit(
        self,
        update: ApplicationUpdate,
        *,
        confirmed: bool,
        restart: bool = True,
        restart_args: Sequence[str] | None = None,
        silent: bool = False,
    ) -> None:
        del silent
        source = self._require_portable_install_source(update, confirmed=confirmed)
        staged = self._stage_portable_update(source, update)
        launch = self._prepare_portable_install_launch(
            update,
            source=source,
            staged=staged,
            restart=restart,
            restart_args=restart_args,
        )
        self._launch_portable_install_helper(launch)

    def _require_portable_install_source(
        self,
        update: ApplicationUpdate,
        *,
        confirmed: bool,
    ) -> Path:
        if not confirmed:
            raise UpdateConfirmationRequired("安装程序更新前必须由用户明确确认")
        source = self._downloaded_paths.get(update.token)
        if source is None or not update.downloaded:
            raise UpdateInstallError("程序更新尚未下载完成")
        self._validate_download(source, update.sha256, update.size_bytes)
        return source

    @staticmethod
    def _cleanup_portable_install_artifacts(*paths: Path) -> None:
        for path in paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _stage_portable_update(
        self,
        source: Path,
        update: ApplicationUpdate,
    ) -> Path:
        target = self.executable_path
        staged = target.with_name(target.name + ".update")
        probe = target.parent / f".huifa-update-write-{uuid.uuid4().hex}.tmp"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            probe.write_bytes(b"ok")
            probe.unlink()
            staged.unlink(missing_ok=True)
            try:
                # Portable data lives on the same volume as the EXE. A hard
                # link avoids copying a 300+ MiB release a second time before
                # shutdown; filesystems without link support fall back safely.
                os.link(source, staged)
            except OSError:
                shutil.copy2(source, staged)
            self._validate_download(staged, update.sha256, update.size_bytes)
        except Exception as exc:
            self._cleanup_portable_install_artifacts(probe, staged)
            raise UpdateInstallError(
                "无法在程序目录暂存新版 EXE；如果程序位于 Program Files，请使用安装版或以管理员身份运行更新。"
                f"\n\n{_safe_update_error(exc, self.config.access_token)}"
            ) from exc
        return staged

    def _prepare_portable_install_launch(
        self,
        update: ApplicationUpdate,
        *,
        source: Path,
        staged: Path,
        restart: bool,
        restart_args: Sequence[str] | None,
    ) -> _PortableInstallLaunch:
        helper = self.state_dir / f"portable-update-{uuid.uuid4().hex}.ps1"
        log_path = self.state_dir / "portable-update.log"
        try:
            powershell = self._powershell_executable()
            self.state_dir.mkdir(parents=True, exist_ok=True)
            restart_payload = base64.b64encode(
                json.dumps(list(restart_args or []), ensure_ascii=False).encode("utf-8")
            ).decode("ascii")
            helper.write_text(self._helper_script(), encoding="utf-8-sig")
            creation_flags = 0
            if os.name == "nt":
                creation_flags = (
                    subprocess.CREATE_NO_WINDOW
                    | subprocess.CREATE_NEW_PROCESS_GROUP
                )
            command = (
                powershell,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-WindowStyle",
                "Hidden",
                "-File",
                str(helper),
                "-ParentPid",
                str(self._process_id),
                "-StagedPath",
                str(staged),
                "-TargetPath",
                str(self.executable_path),
                "-ExpectedSha256",
                update.sha256,
                "-DownloadedPath",
                str(source),
                "-PendingManifest",
                str(self._pending_path),
                "-IntentManifest",
                str(self._install_intent_path),
                "-ReceiptPath",
                str(self._install_receipt_path),
                "-FromVersion",
                update.current_version,
                "-ToVersion",
                update.version,
                "-LogPath",
                str(log_path),
                "-Restart",
                "1" if restart else "0",
                "-RestartArgsBase64",
                restart_payload,
            )
        except Exception as exc:
            self._cleanup_portable_install_artifacts(helper, staged)
            raise UpdateInstallError(
                "无法准备单 EXE 更新替换器："
                f"{_safe_update_error(exc, self.config.access_token)}"
            ) from exc
        return _PortableInstallLaunch(
            staged=staged,
            helper=helper,
            command=command,
            creation_flags=creation_flags,
        )

    def _launch_portable_install_helper(self, launch: _PortableInstallLaunch) -> None:
        try:
            subprocess.Popen(
                list(launch.command),
                close_fds=True,
                creationflags=launch.creation_flags,
            )
        except Exception as exc:
            self._cleanup_portable_install_artifacts(launch.helper, launch.staged)
            raise UpdateInstallError(
                "无法启动单 EXE 更新替换器："
                f"{_safe_update_error(exc, self.config.access_token)}"
            ) from exc

    def _github_headers(self, *, binary: bool = False) -> dict[str, str]:
        headers = {
            "Accept": "application/octet-stream" if binary else "application/vnd.github+json",
            "X-GitHub-Api-Version": _GITHUB_API_VERSION,
            "User-Agent": "HuifaVideoDownloader-Updater",
        }
        if binary:
            # Byte ranges describe the selected representation. Disabling
            # transfer compression keeps on-disk offsets aligned with Range
            # and Content-Range even when a proxy is involved.
            headers["Accept-Encoding"] = "identity"
        if self.config.access_token:
            headers["Authorization"] = f"Bearer {self.config.access_token}"
        return headers

    def _load_resume_download(
        self,
        temporary: Path,
        manifest: Path,
        asset: dict[str, Any],
        version: str,
    ) -> dict[str, Any] | None:
        """Return trusted resume metadata or discard an unrelated partial."""
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if temporary.exists():
                self._remove_resume_download(temporary, manifest)
            return None
        except (OSError, ValueError, TypeError):
            self._remove_resume_download(temporary, manifest)
            return None
        if not isinstance(payload, dict) or not temporary.is_file():
            self._remove_resume_download(temporary, manifest)
            return None
        expected = {
            "schema_version": 1,
            "url": str(asset["url"]),
            "version": str(version),
            "size_bytes": int(asset["size"]),
            "sha256": str(asset["sha256"]).casefold(),
        }
        actual = {
            "schema_version": payload.get("schema_version"),
            "url": str(payload.get("url") or ""),
            "version": str(payload.get("version") or ""),
            "size_bytes": payload.get("size_bytes"),
            "sha256": str(payload.get("sha256") or "").casefold(),
        }
        try:
            actual["schema_version"] = int(actual["schema_version"] or 0)
            actual["size_bytes"] = int(actual["size_bytes"] or 0)
            partial_size = temporary.stat().st_size
        except (OSError, TypeError, ValueError):
            self._remove_resume_download(temporary, manifest)
            return None
        if actual != expected or partial_size <= 0 or partial_size > expected["size_bytes"]:
            self._remove_resume_download(temporary, manifest)
            return None
        return payload

    def _write_resume_download(
        self,
        manifest: Path,
        asset: dict[str, Any],
        version: str,
        response: Any,
    ) -> dict[str, Any]:
        payload = {
            "schema_version": 1,
            "url": str(asset["url"]),
            "version": str(version),
            "size_bytes": int(asset["size"]),
            "sha256": str(asset["sha256"]).casefold(),
            "etag": self._response_header(response, "ETag"),
            "last_modified": self._response_header(response, "Last-Modified"),
        }
        write_json_atomic(manifest, payload)
        return payload

    @staticmethod
    def _response_header(response: Any, name: str) -> str:
        headers = getattr(response, "headers", None)
        if headers is None:
            return ""
        try:
            return str(headers.get(name, "") or "").strip()
        except (AttributeError, TypeError):
            return ""

    @staticmethod
    def _resume_validator(payload: dict[str, Any]) -> str:
        etag = str(payload.get("etag") or "").strip()
        if etag and not etag.casefold().startswith("w/"):
            return etag
        return str(payload.get("last_modified") or "").strip()

    def _resume_validators_match(self, payload: dict[str, Any], response: Any) -> bool:
        previous_etag = str(payload.get("etag") or "").strip()
        response_etag = self._response_header(response, "ETag")
        has_strong_etag = bool(
            previous_etag and not previous_etag.casefold().startswith("w/")
        )
        if (
            has_strong_etag
            and response_etag
            and previous_etag != response_etag
        ):
            return False
        previous_modified = str(payload.get("last_modified") or "").strip()
        response_modified = self._response_header(response, "Last-Modified")
        if not has_strong_etag and previous_modified and response_modified:
            return previous_modified == response_modified
        return True

    @staticmethod
    def _content_range_matches(value: str, offset: int, expected_size: int) -> bool:
        match = re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+)", str(value or "").strip(), re.I)
        if match is None:
            return False
        start, end, total = (int(item) for item in match.groups())
        return bool(
            start == int(offset)
            and total == int(expected_size)
            and start <= end < total
        )

    @staticmethod
    def _remove_resume_download(temporary: Path, manifest: Path) -> None:
        for path in (temporary, manifest, manifest.with_name(manifest.name + ".tmp")):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _write_pending_manifest(self, update: ApplicationUpdate, path: Path) -> None:
        try:
            resolved_path = path.resolve()
            resolved_state_dir = self.state_dir.resolve()
        except OSError as exc:
            raise UpdateDownloadError("无法解析程序更新的本地保存路径") from exc
        if resolved_path.parent != resolved_state_dir:
            raise UpdateDownloadError("程序更新文件不在应用本地更新目录中")
        payload = {
            "schema_version": 1,
            "file_name": resolved_path.name,
            "current_version": update.current_version,
            "version": update.version,
            "size_bytes": update.size_bytes,
            "sha256": update.sha256,
            "release_notes_markdown": update.release_notes_markdown,
            "is_downgrade": update.is_downgrade,
        }
        write_json_atomic(self._pending_path, payload)

    def _remove_pending_manifest(self) -> None:
        for path in (
            self._pending_path,
            self._pending_path.with_name(self._pending_path.name + ".tmp"),
        ):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass

    def _validate_download(
        self,
        path: Path,
        expected_digest: str,
        expected_size: int,
        *,
        cancel_check: Callable[[], None] | None = None,
    ) -> None:
        try:
            if cancel_check is not None:
                cancel_check()
            if path.stat().st_size != int(expected_size):
                raise UpdateDownloadError("已下载更新的文件大小与 GitHub 元数据不一致")
            hasher = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    if cancel_check is not None:
                        cancel_check()
                    hasher.update(chunk)
            if hasher.hexdigest().casefold() != self._digest_hex(expected_digest):
                raise UpdateDownloadError("已下载更新的 SHA-256 校验失败")
            self._validate_windows_executable(path)
        except UpdateDownloadError:
            raise
        except OSError as exc:
            raise UpdateDownloadError(f"无法读取已下载更新：{exc}") from exc

    @staticmethod
    def _validate_windows_executable(path: Path) -> None:
        try:
            size = path.stat().st_size
            if size < 64:
                raise UpdateDownloadError("下载内容不是有效的 Windows EXE")
            with path.open("rb") as stream:
                if stream.read(2) != b"MZ":
                    raise UpdateDownloadError("下载内容不是有效的 Windows EXE")
                stream.seek(0x3C)
                pe_offset = int.from_bytes(stream.read(4), "little")
                if pe_offset < 64 or pe_offset > size - 4:
                    raise UpdateDownloadError("下载内容的 PE 头无效")
                stream.seek(pe_offset)
                if stream.read(4) != b"PE\x00\x00":
                    raise UpdateDownloadError("下载内容的 PE 签名无效")
        except UpdateDownloadError:
            raise
        except OSError as exc:
            raise UpdateDownloadError(f"无法验证 Windows EXE：{exc}") from exc

    @staticmethod
    def _digest_hex(value: Any) -> str:
        raw = str(value or "").strip().casefold()
        if raw.startswith("sha256:"):
            raw = raw.split(":", 1)[1]
        return raw if re.fullmatch(r"[0-9a-f]{64}", raw) else ""

    @staticmethod
    def _parse_version(value: str, label: str) -> Version:
        raw = str(value or "").strip()
        raw = re.sub(r"^[vV](?=\d)", "", raw)
        try:
            return Version(raw)
        except InvalidVersion as exc:
            raise UpdateCheckError(f"{label}不是有效版本号：{value}") from exc

    @staticmethod
    def _copy_update(update: ApplicationUpdate, *, downloaded: bool) -> ApplicationUpdate:
        return ApplicationUpdate(
            token=update.token,
            current_version=update.current_version,
            version=update.version,
            package_id=update.package_id,
            file_name=update.file_name,
            size_bytes=update.size_bytes,
            sha256=update.sha256,
            release_notes_markdown=update.release_notes_markdown,
            is_downgrade=update.is_downgrade,
            is_portable=update.is_portable,
            downloaded=downloaded,
            delivery_kind=update.delivery_kind,
        )

    def _powershell_executable(self) -> str:
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        candidate = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        if candidate.is_file():
            return str(candidate)
        resolved = shutil.which("powershell.exe")
        if resolved:
            return resolved
        raise UpdateInstallError("系统未找到 Windows PowerShell，无法安全替换单 EXE")

    @staticmethod
    def _helper_script() -> str:
        return r'''param(
    [Parameter(Mandatory=$true)][int]$ParentPid,
    [Parameter(Mandatory=$true)][string]$StagedPath,
    [Parameter(Mandatory=$true)][string]$TargetPath,
    [Parameter(Mandatory=$true)][string]$ExpectedSha256,
    [Parameter(Mandatory=$true)][string]$DownloadedPath,
    [Parameter(Mandatory=$true)][string]$PendingManifest,
    [Parameter(Mandatory=$true)][string]$IntentManifest,
    [Parameter(Mandatory=$true)][string]$ReceiptPath,
    [Parameter(Mandatory=$true)][string]$FromVersion,
    [Parameter(Mandatory=$true)][string]$ToVersion,
    [Parameter(Mandatory=$true)][string]$LogPath,
    [Parameter(Mandatory=$true)][int]$Restart,
    [Parameter(Mandatory=$true)][string]$RestartArgsBase64
)
$ErrorActionPreference = 'Stop'
$backupPath = $TargetPath + '.previous'
function Write-UpdateLog([string]$Message) {
    $directory = Split-Path -Parent $LogPath
    if ($directory) { New-Item -ItemType Directory -Path $directory -Force | Out-Null }
    Add-Content -LiteralPath $LogPath -Encoding UTF8 -Value ((Get-Date -Format o) + ' ' + $Message)
}
function Get-Sha256Hex([string]$Path) {
    $stream = [System.IO.File]::OpenRead($Path)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $algorithm.ComputeHash($stream)
        return ([System.BitConverter]::ToString($bytes)).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $algorithm.Dispose()
        $stream.Dispose()
    }
}
function Write-InstallReceipt([string]$Status, [string]$CurrentVersion, [string]$Message) {
    $directory = Split-Path -Parent $ReceiptPath
    if ($directory) { [System.IO.Directory]::CreateDirectory($directory) | Out-Null }
    $payload = [ordered]@{
        schema_version = 1
        status = $Status
        from_version = $FromVersion
        to_version = $ToVersion
        current_version = $CurrentVersion
        delivery_kind = 'single-exe'
        message = $Message
        finished_at = [DateTime]::UtcNow.ToString('o')
    }
    $temporary = $ReceiptPath + '.tmp'
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText(
        $temporary,
        ((ConvertTo-Json -InputObject $payload -Compress) + [Environment]::NewLine),
        $utf8
    )
    Move-Item -LiteralPath $temporary -Destination $ReceiptPath -Force
}
try {
    try { Wait-Process -Id $ParentPid -Timeout 300 -ErrorAction SilentlyContinue } catch {}
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        if (-not (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 500
    }
    if (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue) {
        throw '主程序在等待时间内没有退出'
    }
    $actual = Get-Sha256Hex $StagedPath
    if ($actual -ne $ExpectedSha256.ToLowerInvariant()) { throw '暂存 EXE 的 SHA-256 校验失败' }
    Remove-Item -LiteralPath $backupPath -Force -ErrorAction SilentlyContinue
    $replaced = $false
    for ($attempt = 0; $attempt -lt 60; $attempt++) {
        try {
            [System.IO.File]::Replace($StagedPath, $TargetPath, $backupPath, $true)
            $replaced = $true
            break
        }
        catch {
            if ($attempt -ge 59) { throw }
            Start-Sleep -Milliseconds 500
        }
    }
    if (-not $replaced) { throw '无法替换正在使用的旧 EXE' }
    $installed = Get-Sha256Hex $TargetPath
    if ($installed -ne $ExpectedSha256.ToLowerInvariant()) { throw '替换后的 EXE 校验失败' }
    Remove-Item -LiteralPath $DownloadedPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PendingManifest -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $backupPath -Force -ErrorAction SilentlyContinue
    try {
        Write-InstallReceipt 'succeeded' $ToVersion '单 EXE 已完成替换和 SHA-256 复核'
    }
    catch {
        Write-UpdateLog ('无法写入更新成功回执：' + $_.Exception.Message)
    }
    Remove-Item -LiteralPath $IntentManifest -Force -ErrorAction SilentlyContinue
    Write-UpdateLog ('更新安装成功：' + $TargetPath)
    if ($Restart -eq 1) {
        $json = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($RestartArgsBase64))
        $arguments = @((ConvertFrom-Json -InputObject $json))
        if ($arguments.Count -gt 0) {
            Start-Process -FilePath $TargetPath -ArgumentList $arguments
        }
        else {
            Start-Process -FilePath $TargetPath
        }
    }
}
catch {
    $failureMessage = $_.Exception.Message
    Write-UpdateLog ('更新安装失败：' + $failureMessage)
    if (Test-Path -LiteralPath $backupPath) {
        Copy-Item -LiteralPath $backupPath -Destination $TargetPath -Force -ErrorAction SilentlyContinue
    }
    try {
        Write-InstallReceipt 'failed' $FromVersion $failureMessage
    }
    catch {
        Write-UpdateLog ('无法写入更新失败回执：' + $_.Exception.Message)
    }
    Remove-Item -LiteralPath $IntentManifest -Force -ErrorAction SilentlyContinue
    if (($Restart -eq 1) -and (Test-Path -LiteralPath $TargetPath)) {
        Start-Process -FilePath $TargetPath
    }
}
finally {
    Remove-Item -LiteralPath $StagedPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $backupPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
}
'''

def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
