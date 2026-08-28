from __future__ import annotations

import importlib
import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit, urlunsplit

import requests

from app.core.atomic_json import write_json_atomic
from app.core.version import APP_VERSION
from app.core.update_receipt import record_update_install_result


VELOPACK_VERSION = "1.2.0"
DEFAULT_AUTO_CHECK_INTERVAL = timedelta(hours=24)
_GITHUB_API_VERSION = "2026-03-10"


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


@dataclass(frozen=True, slots=True)
class VelopackUpdaterConfig:
    repository: str
    prerelease: bool = False
    access_token: str | None = field(default=None, repr=False)
    channel: str | None = None

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


@dataclass(frozen=True, slots=True)
class UpdaterRuntime:
    app_id: str
    current_version: str
    is_portable: bool


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


def _github_headers(access_token: str | None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        "User-Agent": "HuifaVideoDownloader-Updater",
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    return headers


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

    def __init__(
        self,
        config: VelopackUpdaterConfig,
        velopack_module: Any | None = None,
        session: Any | None = None,
    ):
        self.config = config
        self._velopack = velopack_module or load_velopack()
        self._session = session or requests.Session()
        self._source = self._velopack.GithubSource(
            config.repository_url(),
            config.access_token,
            config.prerelease,
        )
        self._options = self._velopack.UpdateOptions(
            False,
            10,
            config.channel,
        )
        # UpdateManager resolves the Velopack manifest in its constructor and
        # therefore raises when this code is run from an IDE/source checkout.
        # Create it lazily so merely constructing Settings/UI services never
        # crashes a source checkout that is not managed by Velopack.
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
        asset = getattr(native, "TargetFullRelease", native)
        embedded_notes = str(getattr(asset, "NotesMarkdown", "") or "")
        release_notes = self._fetch_github_release_notes(
            str(getattr(asset, "Version", "") or ""),
            embedded_notes,
        )
        return self._register(
            native,
            runtime,
            downloaded=False,
            release_notes_markdown=release_notes,
        )

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

    def _register(
        self,
        native: Any,
        runtime: UpdaterRuntime,
        downloaded: bool,
        release_notes_markdown: str | None = None,
    ) -> ApplicationUpdate:
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
            release_notes_markdown=(
                str(getattr(asset, "NotesMarkdown", "") or "")
                if release_notes_markdown is None
                else release_notes_markdown
            ),
            is_downgrade=bool(getattr(native, "IsDowngrade", False)),
            is_portable=runtime.is_portable,
            downloaded=downloaded,
        )

    def _fetch_github_release_notes(self, version: str, fallback: str) -> str:
        """Return the matching GitHub Release body without blocking updates on failure."""

        try:
            repository_url = self.config.repository_url()
            owner, repository = [
                part for part in urlsplit(repository_url).path.split("/") if part
            ]
            tag = "v" + str(version or "").strip().lstrip("vV")
            response = self._session.get(
                f"https://api.github.com/repos/{owner}/{repository}/releases/tags/{tag}",
                headers=_github_headers(self.config.access_token),
                timeout=(5, 15),
            )
            try:
                response.raise_for_status()
                payload = response.json()
            finally:
                _close_http_response(response)
            if isinstance(payload, dict) and not payload.get("draft"):
                return str(payload.get("body") or "").strip() or fallback
        except Exception:
            # The package feed already contains a copy of the release notes.
            # GitHub API limits or temporary network failures must never make
            # an otherwise valid application update unavailable.
            return fallback
        return fallback

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

def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
