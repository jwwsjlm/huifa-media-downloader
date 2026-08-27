from __future__ import annotations

import html as html_lib
import json
import re
import os
import platform
import shutil
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Iterable, Mapping
from urllib.parse import quote, unquote, urlsplit

import requests
from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot

from app.core.qt_lifecycle import delete_unstarted_worker
from app.core.component_update_cache import (
    read_component_cache,
    write_component_cache,
)
from app.core.update_download import (
    AssetDownloadWorker,
    is_supported_github_download_url,
    is_supported_github_source_archive_url,
    normalize_expected_download_size,
)
from app.core.paths import application_dir, data_dir, tool_runtime_roots
from app.core.external_ytdlp import (
    clear_external_ytdlp_version_cache,
    remember_external_ytdlp_version,
)
from app.core.local_components import activate_local_ejs, local_ejs_component
from app.core.github_mirrors import (
    GithubDownloadRoute,
    ROUTE_AUTO,
    ROUTE_DIRECT,
    github_download_routes,
    normalize_github_route,
    parse_custom_mirror_urls,
    route_download_url,
    route_metadata_probe_url,
    selected_download_routes,
)
from app.core.tool_installer import ToolInstallResult, install_tool_component
from app.core.tool_resolver import normalize_runtime_component, resolve_ffprobe_tool, resolve_runtime_tool
from app.core.version import APP_VERSION


GITHUB_RELEASE_REPOS = {
    "yt-dlp": "yt-dlp/yt-dlp",
    "yt-dlp-ejs": "yt-dlp/ejs",
    "FFmpeg": "yt-dlp/FFmpeg-Builds",
    "Deno": "denoland/deno",
}

FFMPEG_BUILD_LATEST = "latest"
FFMPEG_BUILD_NVENC_LEGACY = "nvenc_13_0"
_FFMPEG_NVENC_LEGACY_TAG = "autobuild-2026-05-31-15-28"
_FFMPEG_NVENC_LEGACY_BUILD_DATE = "20260531"
_FFMPEG_NVENC_LEGACY_COMMIT = "054dffd133"
_FFMPEG_NVENC_LEGACY_VERSION = "N-124716-g054dffd133-20260531"
_FFMPEG_NVENC_LEGACY_ASSETS = (
    (
        "ffmpeg-N-124716-g054dffd133-win64-gpl.zip",
        "b368f2dd90d460f9a0836dd6faacf9d084a603d99d60ac01cc8d6ff69308cac0",
        221_454_403,
    ),
    (
        "ffmpeg-N-124716-g054dffd133-win32-gpl.zip",
        "0715ac6638181ead338ff05a054e19c1dc34a1a52e4249c425598136ede788d0",
        0,
    ),
    (
        "ffmpeg-N-124716-g054dffd133-winarm64-gpl.zip",
        "28f2737f2f0cbae5e049cb87762c30a63664a1d56b562fc8984c5510868f246d",
        0,
    ),
)

# These libraries are implementation details bundled into the packaged
# application. End users must never be asked to install or update them next
# to HuifaVideoDownloader.exe. Keep the guard separate from the repository
# mapping so a stale/custom caller cannot accidentally re-add one to the UI.
_BUNDLED_RUNTIME_COMPONENTS = frozenset({"pyside6"})
_APPLICATION_MANAGED_COMPONENTS = frozenset()


# Keep this list in sync with ``tool_installer``. Showing an unsupported
# package as downloadable creates a dead button and a confusing install error.
_UPDATE_FILE_SUFFIX_SCORES = {
    ".exe": 80,
    ".whl": 60,
    ".zip": 45,
}
_BLOCKED_RELEASE_ASSET_MARKERS = (
    "checksum",
    "checksums",
    "sha256",
    "sha512",
    "signature",
    "symbols",
    "debug",
    "source",
    "sbom",
    "linux",
    "darwin",
    "macos",
    "osx",
    "android",
    "freebsd",
)
_AUTO_INSTALL_COMPONENTS = frozenset({"yt-dlp", "yt-dlp-ejs", "ffmpeg", "deno"})

# The default repositories are checked independently. Running those network
# requests one after another makes the dialog wait for the sum of every GitHub
# timeout. A small bounded pool keeps the common path fast without creating an
# unbounded number of sockets when a custom repository map is supplied.
_UPDATE_CHECK_MAX_WORKERS = 4
_UPDATE_HTTP_TIMEOUT = (3, 8)
_BACKGROUND_ROUTE_PROBE_MAX_WORKERS = 3
_BACKGROUND_ROUTE_PROBE_RETRY_SECONDS = 60
_BACKGROUND_ROUTE_PROBE_FRESH_SECONDS = 60 * 60


def normalize_ffmpeg_build_channel(value: str | None) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized == FFMPEG_BUILD_NVENC_LEGACY:
        return FFMPEG_BUILD_NVENC_LEGACY
    return FFMPEG_BUILD_LATEST


def _ffmpeg_nvenc_legacy_release_payload() -> dict[str, Any]:
    release_url = (
        "https://github.com/yt-dlp/FFmpeg-Builds/releases/tag/"
        f"{_FFMPEG_NVENC_LEGACY_TAG}"
    )
    download_root = (
        "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/"
        f"{_FFMPEG_NVENC_LEGACY_TAG}"
    )
    assets: list[dict[str, Any]] = []
    for name, sha256, size in _FFMPEG_NVENC_LEGACY_ASSETS:
        asset: dict[str, Any] = {
            "name": name,
            "state": "uploaded",
            "browser_download_url": f"{download_root}/{name}",
            "digest": f"sha256:{sha256}",
            "created_at": "2026-05-31T15:28:00Z",
            "updated_at": "2026-05-31T15:28:00Z",
        }
        if size > 0:
            asset["size"] = size
        assets.append(asset)
    return {
        "tag_name": _FFMPEG_NVENC_LEGACY_TAG,
        "name": "FFmpeg NVENC API 13.0 compatibility build",
        "html_url": release_url,
        "published_at": "2026-05-31T15:28:00Z",
        "created_at": "2026-05-31T15:28:00Z",
        "assets": assets,
        "_metadata_route": ROUTE_DIRECT,
        "_metadata_route_name": "GitHub 固定版本清单",
    }


def _ffmpeg_legacy_build_installed(current: str) -> bool:
    value = str(current or "").casefold()
    return re.search(
        rf"(?<![0-9a-f])g{re.escape(_FFMPEG_NVENC_LEGACY_COMMIT)}(?![0-9a-f])",
        value,
    ) is not None


def _version_build_date(value: str) -> str:
    dates = re.findall(
        r"(?<!\d)(20\d{6})(?!\d)",
        str(value or ""),
    )
    return max(dates, default="")


def run_disposable_jobs(
    jobs: list[Callable[[], Any]],
    *,
    max_workers: int,
    cancel_event: threading.Event,
    thread_name_prefix: str = "disposable-job",
    on_result: Callable[[int, Any], None] | None = None,
) -> list[Any]:
    """Run disposable read-only checks without delaying interpreter shutdown.

    Python's ThreadPoolExecutor registers non-daemon workers that are joined at
    interpreter exit. These checks do not install or replace files, so daemon
    workers are a better fit: cancellation returns immediately while an
    in-flight socket or version command is discarded when the process exits.
    """
    if not jobs:
        return []
    task_queue: Queue[tuple[int, Callable[[], Any]]] = Queue()
    result_queue: Queue[tuple[int, Any, BaseException | None]] = Queue()
    for index, job in enumerate(jobs):
        task_queue.put((index, job))

    def run_jobs() -> None:
        while not cancel_event.is_set():
            try:
                index, job = task_queue.get_nowait()
            except Empty:
                return
            try:
                result_queue.put((index, job(), None))
            except BaseException as exc:
                result_queue.put((index, None, exc))

    workers = min(max(1, int(max_workers)), len(jobs))
    for index in range(workers):
        threading.Thread(
            target=run_jobs,
            name=f"{thread_name_prefix}-{index + 1}",
            daemon=True,
        ).start()

    results: list[Any] = [None] * len(jobs)
    remaining = len(jobs)
    while remaining:
        if cancel_event.is_set():
            raise InterruptedError("后台检查已取消")
        try:
            index, result, error = result_queue.get(timeout=0.05)
        except Empty:
            continue
        if error is not None:
            raise error
        results[index] = result
        if on_result is not None:
            on_result(index, result)
        remaining -= 1
    return results
_COMPONENT_CACHE_SCHEMA_VERSION = 1
_COMPONENT_CACHE_TTL_SECONDS = 6 * 60 * 60
_COMPONENT_CACHE_FILENAME = "update-component-cache.json"


class _UpdateCheckCancelled(RuntimeError):
    """Internal control flow used to stop a component probe cooperatively."""


@dataclass(frozen=True, slots=True)
class _InstalledComponentState:
    current: str
    source: str
    runtime_path: str

    @property
    def missing(self) -> bool:
        normalized = str(self.current or "").strip().casefold()
        return normalized in {"未安装", "未检测", "not installed", "not detected"}


@dataclass(frozen=True, slots=True)
class _ComponentReleaseState:
    latest: str
    display_version: str
    assets: list[dict[str, Any]]
    compatible_asset: dict[str, Any] | None
    rolling: bool
    local_build_date: str
    remote_build_date: str
    rolling_update_available: bool
    channel_switch_required: bool


@dataclass(frozen=True, slots=True)
class _ComponentVersionState:
    display_version: str
    local_build_date: str
    remote_build_date: str
    rolling_update_available: bool


@dataclass(frozen=True, slots=True)
class _ComponentUpdateDecision:
    has_update: bool
    install_available: bool
    auto_install_supported: bool
    managed_by_application: bool
    upstream_update_available: bool


def _component_key(name: str) -> str:
    return str(name or "").strip().lower().replace("_", "-")


def component_auto_install_supported(name: str) -> bool:
    """Whether the downloaded release asset can be installed by this app."""
    return _component_key(name) in _AUTO_INSTALL_COMPONENTS


def component_managed_by_application(name: str) -> bool:
    """Whether updating the main executable is the only effective update path."""
    return _component_key(name) in _APPLICATION_MANAGED_COMPONENTS


def component_visible_in_update_list(name: str) -> bool:
    """Whether a runtime belongs in the user-facing external-tool list.

    PySide6 is the GUI runtime already embedded by PyInstaller.  It is not a
    side-by-side executable and cannot be installed meaningfully from a GitHub
    release, so neither stale configuration nor a custom caller may expose it
    as an uninstalled/downloadable tool.
    """
    return _component_key(name) not in _BUNDLED_RUNTIME_COMPONENTS


def windows_architecture() -> str:
    machine = platform.machine().lower()
    if "arm" in machine or "aarch64" in machine:
        return "arm64"
    return "x64" if struct.calcsize("P") * 8 >= 64 else "x86"


def _normalize_asset_architecture(value: str) -> str:
    compact = re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())
    if compact in {"arm64", "aarch64", "winarm64"}:
        return "arm64"
    if compact in {"x8664", "x64", "amd64", "win64"}:
        return "x64"
    if compact in {"x8632", "x86", "i386", "i686", "ia32", "win32"}:
        return "x86"
    return compact


def _asset_architecture(name: str) -> str:
    compact = re.sub(r"[^a-z0-9]+", "", name.lower())
    if "arm64" in compact or "aarch64" in compact or "winarm64" in compact:
        return "arm64"
    if any(marker in compact for marker in ("x8664", "x64", "amd64", "win64")):
        return "x64"
    if any(marker in compact for marker in ("x8632", "x86", "i386", "i686", "ia32", "win32")):
        return "x86"
    return "generic"


def _component_asset_score(component: str, name: str) -> int | None:
    """Return a relevance score, or ``None`` for an unrelated asset."""
    compact = re.sub(r"[^a-z0-9]+", "", name.lower())
    component_key = str(component or "").strip().lower()
    if component_key == "yt-dlp":
        if name.lower() in {"yt-dlp.exe", "yt_dlp.exe"}:
            return 1000
        return 180 if "ytdlp" in compact else None
    if component_key == "yt-dlp-ejs":
        lowered = name.casefold()
        if lowered.startswith("yt_dlp_ejs-") and lowered.endswith("-py3-none-any.whl"):
            return 1000
        return None
    if component_key == "ffmpeg":
        return 180 if "ffmpeg" in compact else None
    if component_key == "node.js":
        return 180 if compact.startswith("node") else None
    if component_key == "deno":
        return 180 if compact.startswith("deno") else None
    if component_key == "汇发视频下载工具":
        if name.lower() == "huifavideodownloader.exe":
            return 1000
        return 220 if ("huifa" in compact and "downloader" in compact) else None
    return None


def _release_asset_candidate(
    component: str,
    asset: object,
    requested_arch: str,
) -> tuple[int, str, dict[str, Any]] | None:
    if not isinstance(asset, dict):
        return None

    raw_name = str(asset.get("name") or "").strip()
    name = Path(raw_name).name
    lowered = name.casefold()
    suffix = Path(name).suffix.casefold()
    if (
        not name
        or name != raw_name
        or suffix not in _UPDATE_FILE_SUFFIX_SCORES
        or any(marker in lowered for marker in _BLOCKED_RELEASE_ASSET_MARKERS)
    ):
        return None
    if str(asset.get("state") or "uploaded").casefold() != "uploaded":
        return None
    try:
        if asset.get("size") is not None and int(asset["size"]) <= 0:
            return None
    except (TypeError, ValueError):
        return None
    if not is_supported_github_download_url(
        str(asset.get("browser_download_url") or "")
    ):
        return None

    component_score = _component_asset_score(component, name)
    if component_score is None:
        return None
    asset_arch = _asset_architecture(name)
    if asset_arch != "generic" and asset_arch != requested_arch:
        return None

    compact = re.sub(r"[^a-z0-9]+", "", lowered)
    windows_named = any(
        marker in compact
        for marker in ("windows", "win32", "win64", "winarm64")
    )
    if suffix == ".zip" and not windows_named:
        # A generic archive is usually source code. The EJS wheel is already
        # platform-independent and never reaches this ZIP-specific branch.
        return None

    score = component_score + _UPDATE_FILE_SUFFIX_SCORES[suffix]
    score += 45 if asset_arch == requested_arch else 5
    score += 35 if windows_named else 0
    score += 8 if "portable" in lowered else 0
    score -= 12 if "shared" in lowered else 0
    return score, lowered, asset


def select_release_asset(component: str, assets: list[dict[str, Any]], arch: str | None = None) -> dict[str, Any] | None:
    """Select one compatible Windows release asset without unsafe fallback.

    GitHub releases often mix binaries for several operating systems and CPU
    architectures with checksums, signatures and source archives. Returning
    the first filename containing ``win`` can silently download ARM/x86 builds
    or even metadata. This selector requires a relevant, downloadable Windows
    artifact and ranks exact portable executables above archives.
    """
    requested_arch = _normalize_asset_architecture(arch or windows_architecture())
    candidates = [
        candidate
        for asset in assets or []
        if (candidate := _release_asset_candidate(
            component,
            asset,
            requested_arch,
        )) is not None
    ]

    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][2]


def normalize_version(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", value or "")
    return tuple(int(number) for number in numbers[:8]) or (0,)


def _rolling_release_build_date(
    current: str,
    payload: Mapping[str, Any],
    compatible_asset: Mapping[str, Any] | None,
) -> tuple[str, str, bool]:
    """Compare moving ``latest`` releases using embedded/local build dates.

    yt-dlp's FFmpeg builds keep a moving release tag. Their executable version
    contains an eight-digit build date, while GitHub exposes an asset/release
    timestamp. This gives existing installations a useful update signal even
    though the tag itself never changes.
    """
    remote_dates: list[str] = []
    for value in (
        (compatible_asset or {}).get("updated_at"),
        (compatible_asset or {}).get("created_at"),
        payload.get("published_at"),
        payload.get("created_at"),
    ):
        match = re.search(r"(20\d{2})-(\d{2})-(\d{2})", str(value or ""))
        if match:
            remote_dates.append("".join(match.groups()))
    local_date = _version_build_date(current)
    remote_date = max(remote_dates, default="")
    return local_date, remote_date, bool(local_date and remote_date and remote_date > local_date)


def _ffmpeg_autobuild_tag(payload: Mapping[str, Any]) -> str:
    """Resolve the immutable autobuild tag behind FFmpeg's moving latest tag."""
    for value in (payload.get("name"), payload.get("body")):
        match = re.search(
            r"Build\s*\(\s*(20\d{2})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})\s*\)",
            str(value or ""),
            flags=re.IGNORECASE,
        )
        if match:
            return "autobuild-" + "-".join(match.groups())
    return ""


def _ffmpeg_build_version_from_assets(
    assets: Iterable[Mapping[str, Any]],
    build_date: str = "",
) -> str:
    selected = select_release_asset("FFmpeg", list(assets))
    name = str((selected or {}).get("name") or "")
    match = re.search(r"ffmpeg-(N-\d+-g[0-9a-f]{7,40})-", name, flags=re.IGNORECASE)
    if match is None:
        return ""
    normalized = match.group(1)
    return f"{normalized}-{build_date}" if build_date else normalized


def _command_version(executable: str | Path, *arguments: str) -> str:
    try:
        kwargs: dict[str, Any] = {
            "capture_output": True,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "timeout": 5,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        output = subprocess.run([str(executable), *arguments], **kwargs)
        return (output.stdout or output.stderr).strip()
    except Exception:
        return ""


def _yt_dlp_module_version() -> str:
    """Return the version from an importable yt-dlp module.

    PyInstaller does not necessarily preserve ``dist-info`` metadata, so
    ``importlib.metadata.version('yt-dlp')`` alone can incorrectly report the
    bundled downloader as missing. More importantly, diagnostics must verify
    the same ``yt_dlp.YoutubeDL`` entry point used by :class:`DownloadWorker`
    before reporting the download core as installed.
    """
    try:
        import yt_dlp
    except Exception:
        return ""

    if not callable(getattr(yt_dlp, "YoutubeDL", None)):
        return ""

    try:
        from yt_dlp.version import __version__ as module_version

        if module_version:
            return str(module_version).strip()
    except Exception:
        pass
    module_version = getattr(yt_dlp, "__version__", "")
    if module_version:
        return str(module_version).strip()
    # Some distribution variants expose neither ``yt_dlp.version`` nor a
    # package attribute but do retain dist-info.  Metadata is a valid fallback
    # only after the actual ``YoutubeDL`` entry point was verified above.
    try:
        metadata_version = str(package_version("yt-dlp")).strip()
        if metadata_version:
            return metadata_version
    except (PackageNotFoundError, ValueError):
        pass
    return ""


def ytdlp_python_core_available() -> bool:
    """Return whether the bundled in-process fallback is usable."""
    return bool(_yt_dlp_module_version())


def _resolve_ytdlp_executable():
    root = application_dir()
    return resolve_runtime_tool(
        "yt-dlp",
        application_root=root,
        runtime_roots=tool_runtime_roots(root),
        environment=os.environ,
        which=shutil.which,
    )


def _detect_ytdlp_installation() -> tuple[str, str]:
    """Report the executable selected by downloads, with module fallback."""
    resolution = _resolve_ytdlp_executable()
    module_version = _yt_dlp_module_version()
    if resolution.found:
        output = _command_version(resolution.executable, "--version")
        version = output.splitlines()[0].strip()[:120] if output else ""
        remember_external_ytdlp_version(resolution.executable, version)
        if version:
            source = f"{resolution.source}（外置下载核心，优先调用）"
            if module_version:
                source += "；内置 Python 模块作为安全回退"
            return version, source
        if not module_version:
            return "未安装", f"检测到 {resolution.source}，但无法读取版本"
    if module_version:
        source = "程序内置 yt-dlp 模块" if getattr(sys, "frozen", False) else "Python 环境 yt-dlp 模块"
        if resolution.found:
            source += f"；{resolution.source} 无法运行，已回退内置模块"
        return module_version, source
    return "未安装", "未检测到可用的外置 yt-dlp.exe 或内置 yt-dlp 模块"


def _detected_ytdlp_runtime_path(source: str, version: str) -> str:
    """Return the concrete executable path represented by a diagnostic source."""
    if version in {"未安装", "未检测"}:
        return ""
    if source.startswith("程序内置 yt-dlp 模块") or source.startswith("Python 环境 yt-dlp 模块"):
        return "内置 Python 模块"
    resolution = _resolve_ytdlp_executable()
    return resolution.executable if resolution.found else ""


def _detect_ytdlp_presence() -> tuple[str, str, str]:
    """Perform a non-blocking yt-dlp presence check for the GUI thread."""
    resolution = _resolve_ytdlp_executable()
    module_version = _yt_dlp_module_version()
    if resolution.found:
        executable = Path(resolution.executable)
        if executable.suffix.casefold() == ".exe":
            try:
                with executable.open("rb") as stream:
                    header = stream.read(2)
            except OSError as exc:
                if not module_version:
                    return "不可用", f"{resolution.source}；无法读取文件：{exc}", resolution.executable
            else:
                if header == b"MZ":
                    source = f"{resolution.source}（外置下载核心，优先调用）"
                    if module_version:
                        source += "；内置 Python 模块作为安全回退"
                    return "已找到", source, resolution.executable
                if not module_version:
                    return "不可用", f"{resolution.source}；不是有效的 Windows 可执行文件", resolution.executable
        else:
            source = f"{resolution.source}（外置下载核心，优先调用）"
            if module_version:
                source += "；内置 Python 模块作为安全回退"
            return "已找到", source, resolution.executable
    if module_version:
        return module_version, (
            "程序内置 yt-dlp 模块" if getattr(sys, "frozen", False) else "Python 环境 yt-dlp 模块"
        ), "内置 Python 模块"
    return "未安装", "未检测到可用的外置 yt-dlp.exe 或内置 yt-dlp 模块", ""


def _detect_external_tool_installation(
    component_key: str,
    configured: str = "",
    configured_ffprobe: str = "",
) -> tuple[str, str, str]:
    """Return version, source and the exact executable used by the app."""
    root = application_dir()
    resolver_kwargs = {
        "application_root": root,
        "runtime_roots": tool_runtime_roots(root),
        "environment": os.environ,
        "which": shutil.which,
    }
    resolution = (
        resolve_ffprobe_tool(configured, configured_ffprobe, **resolver_kwargs)
        if component_key == "ffprobe"
        else resolve_runtime_tool(component_key, configured, **resolver_kwargs)
    )
    if not resolution.found:
        return "未安装", "", resolution.executable

    version_arguments = ("-version",) if component_key in {"ffmpeg", "ffprobe"} else ("--version",)
    output = _command_version(resolution.executable, *version_arguments)
    if component_key in {"ffmpeg", "ffprobe"}:
        match = re.search(r"ffmpeg version\s+([^\s]+)", output)
        if component_key == "ffprobe":
            match = re.search(r"ffprobe version\s+([^\s]+)", output)
        version = match.group(1) if match else ""
    else:
        version = output.splitlines()[0][:120] if output else ""
    return version or "已找到（版本读取失败）", resolution.source, resolution.executable


def _detect_local_ejs_installation() -> tuple[str, str, str]:
    component = local_ejs_component()
    if component is None:
        return "未安装", "未检测到软件本地 yt-dlp-ejs wheel", ""
    return component.version, component.source, component.path


def installed_component_details(
    name: str,
    configured: str = "",
    configured_ffprobe: str = "",
) -> tuple[str, str, str]:
    """Return ``(version, source, runtime_path)`` for diagnostics and UI."""
    component_key = normalize_runtime_component(name)
    if component_key == "yt-dlp":
        version, source = _detect_ytdlp_installation()
        return version, source, _detected_ytdlp_runtime_path(source, version)
    if component_key == "yt-dlp-ejs":
        return _detect_local_ejs_installation()
    if component_key in {"ffmpeg", "ffprobe", "node.js", "deno"}:
        return _detect_external_tool_installation(component_key, configured, configured_ffprobe)
    version = installed_version(name)
    return version, "", ""


def runtime_component_presence(
    name: str,
    configured: str = "",
    configured_ffprobe: str = "",
) -> tuple[str, str, str]:
    """Resolve a component without launching an external process.

    This is intended for UI-thread preflight checks.  ``--version`` probes are
    useful when comparing releases, but a damaged or user-supplied executable
    can take several seconds to exit.  Presence checks therefore share the
    exact runtime resolver, validate Windows EXE headers with bounded file I/O,
    and leave full version probing to :class:`UpdateWorker`'s background
    thread.
    """
    component_key = normalize_runtime_component(name)
    if component_key == "yt-dlp":
        return _detect_ytdlp_presence()
    if component_key == "yt-dlp-ejs":
        return _detect_local_ejs_installation()
    if component_key not in {"ffmpeg", "ffprobe", "node.js", "deno"}:
        return installed_component_details(name, configured, configured_ffprobe)

    root = application_dir()
    resolver_kwargs = {
        "application_root": root,
        "runtime_roots": tool_runtime_roots(root),
        "environment": os.environ,
        "which": shutil.which,
    }
    resolution = (
        resolve_ffprobe_tool(configured, configured_ffprobe, **resolver_kwargs)
        if component_key == "ffprobe"
        else resolve_runtime_tool(component_key, configured, **resolver_kwargs)
    )
    if not resolution.found:
        return "未安装", "", resolution.executable

    executable = Path(resolution.executable)
    if executable.suffix.casefold() == ".exe":
        try:
            with executable.open("rb") as stream:
                header = stream.read(2)
        except OSError as exc:
            return "不可用", f"{resolution.source}；无法读取文件：{exc}", resolution.executable
        if header != b"MZ":
            return "不可用", f"{resolution.source}；不是有效的 Windows EXE", resolution.executable
    return "已找到", resolution.source, resolution.executable


def installed_component(name: str, configured: str = "") -> tuple[str, str]:
    """Return ``(version, source)`` for display and version comparison."""
    version, source, _runtime_path = installed_component_details(name, configured)
    return version, source


def installed_version(name: str, configured: str = "") -> str:
    """Detect the version actually available to the portable executable.

    Local files beside ``HuifaVideoDownloader.exe`` take precedence for
    external tools.  Python package metadata is only a fallback because a
    frozen application may not retain ``dist-info`` even though yt-dlp is
    bundled and importable.
    """
    component = str(name or "").strip()
    component_key = normalize_runtime_component(component)
    if component_key == "yt-dlp":
        return _detect_ytdlp_installation()[0]
    if component_key == "yt-dlp-ejs":
        return _detect_local_ejs_installation()[0]

    if component_key in {"ffmpeg", "ffprobe", "node.js", "deno"}:
        return _detect_external_tool_installation(component_key, configured)[0]

    try:
        return package_version(component)
    except PackageNotFoundError:
        return "未安装"


def latest_tag_from_html(
    repo: str,
    headers: Mapping[str, str],
    route: GithubDownloadRoute | None = None,
    route_kind: str = "",
) -> dict[str, Any]:
    """Best-effort tag lookup when the unauthenticated API quota is spent."""
    official_url = f"https://github.com/{repo}/tags"
    response = requests.get(
        route_download_url(route, official_url, route_kind) if route is not None else official_url,
        headers={"User-Agent": headers.get("User-Agent", "HuifaVideoDownloader")},
        timeout=_UPDATE_HTTP_TIMEOUT,
    )
    response.raise_for_status()
    html = response.text or ""
    patterns = (
        rf"/{re.escape(repo)}/releases/tag/([^\"/?#]+)",
        rf"/{re.escape(repo)}/tree/([^\"/?#]+)",
        rf"/{re.escape(repo)}/tags/([^\"/?#]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return {
                "tag_name": match.group(1),
                "assets": [],
                "html_url": f"https://github.com/{repo}/tags",
                "published_at": "",
            }
    raise RuntimeError("无法从 GitHub tags 页面识别最新版本")


def release_assets_from_html(
    repo: str,
    tag: str,
    headers: Mapping[str, str],
    route: GithubDownloadRoute | None = None,
    route_kind: str = "",
) -> list[dict[str, Any]]:
    """Read public GitHub release attachments without consuming REST quota.

    Shared public IP addresses regularly exhaust GitHub's unauthenticated API
    allowance. The normal ``releases/latest`` redirect still reveals the
    version in that state, but returning an empty asset list made Deno appear
    to have "no download source". GitHub's own expanded-assets fragment is a
    public release page, so use only its same-repository ``releases/download``
    links as a bounded fallback.
    """
    normalized_repo = str(repo or "").strip().strip("/")
    normalized_tag = str(tag or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.+%-]{1,200}", normalized_tag):
        return []
    official_url = (
        f"https://github.com/{normalized_repo}/releases/expanded_assets/"
        f"{quote(normalized_tag, safe='%')}"
    )
    response = requests.get(
        route_download_url(route, official_url, route_kind) if route is not None else official_url,
        headers={"User-Agent": headers.get("User-Agent", "HuifaVideoDownloader")},
        timeout=_UPDATE_HTTP_TIMEOUT,
    )
    response.raise_for_status()
    html = response.text or ""
    prefix = f"/{normalized_repo}/releases/download/"
    assets: list[dict[str, Any]] = []
    seen: set[str] = set()
    blocks = re.findall(r"<li\b[^>]*>.*?</li>", html, flags=re.IGNORECASE | re.DOTALL)
    if not blocks:
        blocks = [html]
    for block in blocks:
        href_match = re.search(r'href=["\']([^"\']+)["\']', block, flags=re.IGNORECASE)
        if href_match is None:
            continue
        href = html_lib.unescape(href_match.group(1))
        if not href.startswith(prefix):
            continue
        url = f"https://github.com{href}"
        name = Path(unquote(urlsplit(url).path)).name
        if not name or name in seen or not is_supported_github_download_url(url):
            continue
        seen.add(name)
        asset = {
            "name": name,
            "state": "uploaded",
            "browser_download_url": url,
        }
        digest_match = re.search(r"sha256:\s*([0-9a-fA-F]{64})", block, flags=re.IGNORECASE)
        if digest_match:
            asset["digest"] = f"sha256:{digest_match.group(1).lower()}"
        assets.append(asset)
        if len(assets) >= 100:
            break
    return assets


def _component_cache_path() -> Path:
    """Return the small persistent cache used by component update checks."""
    return data_dir() / _COMPONENT_CACHE_FILENAME


class UpdateWorker(QObject):
    result_ready = Signal(object)
    finished = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        repos: dict[str, str],
        app_repo: str = "",
        tool_overrides: dict[str, str] | None = None,
        github_route_mode: str = ROUTE_AUTO,
        github_mirror_urls: str = "",
        route_probe_results: Mapping[str, Mapping[str, Any]] | None = None,
        ffmpeg_build_channel: str = FFMPEG_BUILD_LATEST,
    ):
        super().__init__()
        self.repos = repos
        self.app_repo = app_repo.strip()
        self.tool_overrides = {
            normalize_runtime_component(name): str(value or "").strip()
            for name, value in (tool_overrides or {}).items()
        }
        self.github_route_mode = normalize_github_route(github_route_mode)
        self.github_mirror_urls = "\n".join(parse_custom_mirror_urls(github_mirror_urls))
        self.route_probe_results = {
            str(route_id): dict(result)
            for route_id, result in (route_probe_results or {}).items()
        }
        self.ffmpeg_build_channel = normalize_ffmpeg_build_channel(ffmpeg_build_channel)
        self._cancelled = threading.Event()

    def _installed_component(self, name: str) -> tuple[str, str, str]:
        component = normalize_runtime_component(name)
        configured = self.tool_overrides.get(component, "")
        # Resolve once so version, source and effective path always describe
        # the same file even if PATH/settings change during a background check.
        if component == "ffprobe":
            return installed_component_details(
                name,
                self.tool_overrides.get("ffmpeg", ""),
                configured,
            )
        return installed_component_details(name, configured)

    def cancel(self) -> None:
        self._cancelled.set()

    def _raise_if_cancelled(self) -> None:
        if self._cancelled.is_set():
            raise _UpdateCheckCancelled("运行组件检查已取消")

    def _request_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "HuifaVideoDownloader",
        }
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _metadata_routes(self) -> tuple[GithubDownloadRoute, ...]:
        selected_routes = selected_download_routes(
            self.github_route_mode, self.github_mirror_urls
        )
        routes = [
            route
            for route in selected_routes
            if route.metadata_supported
            and (
                route.id == ROUTE_DIRECT
                or self.route_probe_results.get(route.id, {}).get("metadata_ok") is not False
            )
        ]
        if self.github_route_mode == ROUTE_AUTO:
            routes.sort(key=lambda route: (
                route.id != ROUTE_DIRECT,
                not bool(self.route_probe_results.get(route.id, {}).get("metadata_ok")),
                int(self.route_probe_results.get(route.id, {}).get("metadata_latency_ms") or 10**9),
                route.third_party,
            ))
        else:
            selected_id = selected_routes[0].id if selected_routes else ""
            routes.extend(
                route
                for route in github_download_routes(self.github_mirror_urls)
                if route.id != selected_id
                and route.metadata_supported
                and self.route_probe_results.get(route.id, {}).get("metadata_ok") is not False
            )
        return tuple(routes)

    def _fast_metadata_routes(self) -> tuple[GithubDownloadRoute, ...]:
        """Return the short route chain used by interactive version checks."""
        routes = self._metadata_routes()
        if not routes:
            return ()
        if self.github_route_mode != ROUTE_AUTO:
            selected = routes[0]
            direct = next((route for route in routes if route.id == ROUTE_DIRECT), None)
            return tuple(dict.fromkeys(
                route for route in (selected, direct) if route is not None
            ))

        direct = next((route for route in routes if route.id == ROUTE_DIRECT), None)
        successful = [
            route
            for route in routes
            if route.id != ROUTE_DIRECT
            and self.route_probe_results.get(route.id, {}).get("metadata_ok") is True
        ]
        fallback = min(
            successful,
            key=lambda route: int(
                self.route_probe_results.get(route.id, {}).get("metadata_latency_ms")
                or 10**9
            ),
            default=None,
        )
        if fallback is None:
            fallback = next((route for route in routes if route.id != ROUTE_DIRECT), None)
        return tuple(route for route in (direct, fallback) if route is not None)

    def _fast_release_page_routes(self) -> tuple[GithubDownloadRoute, ...]:
        routes = self._release_page_routes()
        if not routes:
            return ()
        if self.github_route_mode != ROUTE_AUTO:
            selected = routes[0]
            direct = next((route for route in routes if route.id == ROUTE_DIRECT), None)
            return tuple(dict.fromkeys(
                route for route in (selected, direct) if route is not None
            ))

        direct = next((route for route in routes if route.id == ROUTE_DIRECT), None)
        successful = [
            route
            for route in routes
            if route.id != ROUTE_DIRECT
            and self.route_probe_results.get(route.id, {}).get("asset_ok") is True
        ]
        fallback = min(
            successful,
            key=lambda route: int(
                self.route_probe_results.get(route.id, {}).get("asset_latency_ms")
                or 10**9
            ),
            default=None,
        )
        if fallback is None:
            fallback = next((route for route in routes if route.id != ROUTE_DIRECT), None)
        return tuple(route for route in (direct, fallback) if route is not None)

    def _route_kind(self, route: GithubDownloadRoute, capability: str) -> str:
        profile = self.route_probe_results.get(route.id, {})
        detected = str(
            profile.get(f"{capability}_kind")
            or profile.get("detected_kind")
            or ""
        ).strip().casefold()
        if detected in {"direct", "prefix", "host", "jsdelivr"}:
            return detected
        return route.kind

    def _route_kinds(self, route: GithubDownloadRoute, capability: str) -> tuple[str, ...]:
        detected = self._route_kind(route, capability)
        if detected == "auto":
            return ("prefix", "host")
        return (detected,)

    def _route_url(
        self,
        route: GithubDownloadRoute,
        official_url: str,
        capability: str,
    ) -> str:
        return route_download_url(
            route,
            official_url,
            self._route_kind(route, capability),
        )

    def _release_page_routes(self) -> tuple[GithubDownloadRoute, ...]:
        routes = list(github_download_routes(self.github_mirror_urls))
        selected = selected_download_routes(self.github_route_mode, self.github_mirror_urls)
        metadata_only_selected = bool(selected and not selected[0].release_page_supported)
        preferred = (
            {}
            if metadata_only_selected
            else {route.id: index for index, route in enumerate(self._metadata_routes())}
        )
        routes = [route for route in routes if route.release_page_supported]
        routes.sort(key=lambda route: (
            preferred.get(route.id, 10**6),
            not bool(self.route_probe_results.get(route.id, {}).get("asset_ok")),
            int(self.route_probe_results.get(route.id, {}).get("asset_latency_ms") or 10**9),
            (not route.third_party) if metadata_only_selected else route.third_party,
        ))
        return tuple(routes)

    @staticmethod
    def _headers_for_route(headers: Mapping[str, str], route: GithubDownloadRoute) -> dict[str, str]:
        routed = dict(headers)
        if route.third_party:
            # A GitHub access token is scoped for GitHub itself. Never reveal
            # it to an independently operated CDN/proxy.
            for key in tuple(routed):
                if key.casefold() in {
                    "authorization", "if-none-match", "if-modified-since",
                }:
                    routed.pop(key, None)
        return routed

    @staticmethod
    def _annotate_metadata_payload(
        payload: Mapping[str, Any],
        route: GithubDownloadRoute,
        *,
        cached: bool = False,
    ) -> dict[str, Any]:
        annotated = dict(payload)
        annotated["_metadata_route"] = route.id
        annotated["_metadata_route_name"] = route.name
        annotated["_metadata_third_party"] = route.third_party
        annotated["_metadata_cached"] = bool(cached)
        warnings: list[str] = []
        if route.third_party:
            warnings.append("元数据来自第三方 CDN/代理，可能因同步延迟而不是上游实时最新版本")
        if cached:
            warnings.append("当前显示的是本地缓存元数据")
        annotated["_metadata_warning"] = "；".join(warnings)
        return annotated

    @staticmethod
    def _canonicalize_routed_payload(
        payload: Mapping[str, Any],
        route: GithubDownloadRoute,
    ) -> dict[str, Any]:
        """Strip a route prefix if a proxy rewrites GitHub URLs in JSON."""
        normalized = dict(payload)

        def official_url(value: Any) -> str:
            raw = str(value or "").strip()
            if route.third_party and route.base_url and raw.startswith(route.base_url):
                candidate = raw[len(route.base_url):].lstrip("/")
                if candidate.startswith("https://"):
                    return candidate
                if route.kind == "host" and re.match(
                    r"^[^/?#]+/[^/?#]+/releases/(?:tag|download)/",
                    candidate,
                    flags=re.IGNORECASE,
                ):
                    return "https://github.com/" + candidate
            return raw

        if normalized.get("html_url"):
            normalized["html_url"] = official_url(normalized.get("html_url"))
        raw_assets = normalized.get("assets")
        if isinstance(raw_assets, list):
            assets: list[Any] = []
            for raw_asset in raw_assets:
                if not isinstance(raw_asset, Mapping):
                    assets.append(raw_asset)
                    continue
                asset = dict(raw_asset)
                if asset.get("browser_download_url"):
                    asset["browser_download_url"] = official_url(asset.get("browser_download_url"))
                assets.append(asset)
            normalized["assets"] = assets
        return normalized

    def _release_assets_for_tag(
        self,
        repo: str,
        tag: str,
        headers: Mapping[str, str],
    ) -> tuple[list[dict[str, Any]], str]:
        """Resolve Release attachments through a route that supports pages."""
        normalized_tag = str(tag or "").strip()
        tag_candidates = [normalized_tag]
        if normalized_tag and not normalized_tag.casefold().startswith("v"):
            tag_candidates.append("v" + normalized_tag)
        failures: list[str] = []
        for route in self._fast_release_page_routes():
            routed_headers = self._headers_for_route(headers, route)
            for route_kind in self._route_kinds(route, "asset"):
                for candidate in tag_candidates:
                    try:
                        assets = release_assets_from_html(
                            repo,
                            candidate,
                            routed_headers,
                            route,
                            route_kind,
                        )
                        if assets:
                            return assets, candidate
                    except Exception as exc:
                        failures.append(f"{route.name}/{route_kind}/{candidate}：{exc}")
        return [], normalized_tag

    def _fetch_latest_payload_from_jsdelivr(
        self,
        repo: str,
        headers: Mapping[str, str],
        route: GithubDownloadRoute,
    ) -> dict[str, Any]:
        """Use jsDelivr's public GitHub package API without repository changes."""
        response = requests.get(
            f"https://data.jsdelivr.com/v1/package/gh/{repo}",
            headers={"User-Agent": headers.get("User-Agent", "HuifaVideoDownloader")},
            timeout=_UPDATE_HTTP_TIMEOUT,
        )
        self._raise_if_cancelled()
        response.raise_for_status()
        document = response.json()
        if not isinstance(document, Mapping):
            raise RuntimeError("jsDelivr 返回了无效的版本数据")
        raw_versions = document.get("versions")
        versions = [
            str(value).strip()
            for value in raw_versions
            if str(value or "").strip()
        ] if isinstance(raw_versions, list) else []
        tags = document.get("tags")
        tag = ""
        if isinstance(tags, Mapping):
            tag = str(tags.get("latest") or "").strip()
        # jsDelivr's latest alias can lag or be absent for date-style tags.
        # Its ordered versions list is the standard API's most useful source
        # for third-party repositories we do not control.
        if versions:
            tag = versions[0]
        if not tag:
            raise RuntimeError("jsDelivr 尚未同步到可识别的仓库版本")
        assets, release_tag = self._release_assets_for_tag(repo, tag, headers)
        payload = {
            "tag_name": tag,
            "assets": assets,
            "html_url": f"https://github.com/{repo}/releases/tag/{quote(release_tag or tag, safe='%')}",
            "published_at": "",
        }
        annotated = self._annotate_metadata_payload(payload, route)
        warning = str(annotated.get("_metadata_warning") or "")
        if not assets:
            warning += "；jsDelivr 仅提供仓库版本/文件，Release 附件仍需其他可用线路"
        annotated["_metadata_warning"] = warning.strip("；")
        write_component_cache(
            _component_cache_path(),
            repo,
            annotated,
            endpoint="jsdelivr-package",
            schema_version=_COMPONENT_CACHE_SCHEMA_VERSION,
            ttl_seconds=_COMPONENT_CACHE_TTL_SECONDS,
        )
        return annotated

    @staticmethod
    def _payload_version(payload: Mapping[str, Any]) -> str:
        return str(payload.get("tag_name") or payload.get("name") or "").strip()

    @classmethod
    def _require_version_payload(
        cls,
        payload: Any,
        *,
        source: str,
    ) -> dict[str, Any]:
        if not isinstance(payload, Mapping) or not cls._payload_version(payload):
            raise RuntimeError(f"{source}未返回可识别的版本数据")
        return dict(payload)

    def _store_route_payload(
        self,
        repo: str,
        payload: Any,
        route: GithubDownloadRoute,
        *,
        endpoint: str,
        response_headers: Mapping[str, Any] | None = None,
        source: str = "GitHub",
    ) -> dict[str, Any]:
        validated = self._require_version_payload(payload, source=source)
        normalized = self._canonicalize_routed_payload(validated, route)
        annotated = self._annotate_metadata_payload(normalized, route)
        write_component_cache(
            _component_cache_path(),
            repo,
            annotated,
            endpoint=endpoint,
            schema_version=_COMPONENT_CACHE_SCHEMA_VERSION,
            ttl_seconds=_COMPONENT_CACHE_TTL_SECONDS,
            response_headers=response_headers,
        )
        return annotated

    @staticmethod
    def _rate_limited(response: Any) -> bool:
        return bool(
            getattr(response, "status_code", 0) == 403
            and str((getattr(response, "headers", {}) or {}).get("X-RateLimit-Remaining") or "")
            == "0"
        )

    def _conditional_request_headers(
        self,
        route: GithubDownloadRoute,
        base_headers: Mapping[str, str],
        cache_entry: Mapping[str, Any] | None,
    ) -> dict[str, str]:
        request_headers = dict(base_headers)
        cached_payload = cache_entry.get("payload") if cache_entry else None
        cached_route = (
            str(cached_payload.get("_metadata_route") or ROUTE_DIRECT)
            if isinstance(cached_payload, Mapping)
            else ""
        )
        if (
            route.third_party
            or not cache_entry
            or cached_route != route.id
            or str(cache_entry.get("endpoint") or "latest") != "latest"
        ):
            return request_headers
        etag = str(cache_entry.get("etag") or "").strip()
        last_modified = str(cache_entry.get("last_modified") or "").strip()
        if etag:
            request_headers["If-None-Match"] = etag
        elif last_modified:
            request_headers["If-Modified-Since"] = last_modified
        return request_headers

    def _cached_version_payload(
        self,
        cache_entry: Mapping[str, Any] | None,
    ) -> dict[str, Any] | None:
        cached = cache_entry.get("payload") if cache_entry else None
        if not isinstance(cached, Mapping) or not self._payload_version(cached):
            return None
        return dict(cached)

    def _fetch_tags_fallback(
        self,
        repo: str,
        base_headers: Mapping[str, str],
        route: GithubDownloadRoute,
    ) -> dict[str, Any]:
        response = requests.get(
            self._route_url(
                route,
                f"https://api.github.com/repos/{repo}/tags?per_page=1",
                "metadata",
            ),
            headers=base_headers,
            timeout=_UPDATE_HTTP_TIMEOUT,
        )
        self._raise_if_cancelled()
        if self._rate_limited(response):
            payload = latest_tag_from_html(
                repo,
                base_headers,
                route,
                self._route_kind(route, "asset"),
            )
        else:
            response.raise_for_status()
            document = response.json()
            if (
                not isinstance(document, list)
                or not document
                or not isinstance(document[0], Mapping)
            ):
                raise RuntimeError("GitHub Tags 未返回可识别的版本数据")
            tag = str(document[0].get("name") or "").strip()
            if not tag:
                raise RuntimeError("GitHub Tags 未返回可识别的版本数据")
            payload = {
                **dict(document[0]),
                "tag_name": tag,
                "assets": [],
                "html_url": f"https://github.com/{repo}/tags",
            }
        self._raise_if_cancelled()
        return self._store_route_payload(
            repo,
            payload,
            route,
            endpoint="tags",
            source="GitHub Tags ",
        )

    def _fetch_rate_limit_fallback(
        self,
        repo: str,
        base_headers: Mapping[str, str],
        route: GithubDownloadRoute,
    ) -> dict[str, Any]:
        redirect = requests.get(
            self._route_url(
                route,
                f"https://github.com/{repo}/releases/latest",
                "asset",
            ),
            headers={"User-Agent": "HuifaVideoDownloader"},
            allow_redirects=False,
            timeout=_UPDATE_HTTP_TIMEOUT,
        )
        self._raise_if_cancelled()
        location = str((getattr(redirect, "headers", {}) or {}).get("Location") or "")
        match = re.search(r"/tag/([^/?#]+)", location)
        if not match:
            payload = latest_tag_from_html(
                repo,
                base_headers,
                route,
                self._route_kind(route, "asset"),
            )
        else:
            tag = match.group(1)
            release_url = (
                location
                if location.startswith("https://")
                else f"https://github.com{location}"
            )
            try:
                assets = release_assets_from_html(
                    repo,
                    tag,
                    base_headers,
                    route,
                    self._route_kind(route, "asset"),
                )
            except (OSError, requests.RequestException, ValueError):
                # A version remains useful even when the release page cannot
                # provide installable assets during this check.
                assets = []
            payload = {
                "tag_name": tag,
                "assets": assets,
                "html_url": release_url,
                "published_at": "",
            }
        self._raise_if_cancelled()
        return self._store_route_payload(
            repo,
            payload,
            route,
            endpoint="html-release",
            source="GitHub Release 页面",
        )

    def _fetch_latest_payload_from_route(
        self,
        repo: str,
        headers: dict[str, str],
        route: GithubDownloadRoute,
    ) -> dict[str, Any]:
        """Fetch one repository release through one selected network route."""
        self._raise_if_cancelled()
        if route.kind == "jsdelivr":
            return self._fetch_latest_payload_from_jsdelivr(repo, headers, route)
        base_headers = self._headers_for_route(headers, route)
        cache_entry = read_component_cache(
            _component_cache_path(),
            repo,
            schema_version=_COMPONENT_CACHE_SCHEMA_VERSION,
        )
        request_headers = self._conditional_request_headers(
            route,
            base_headers,
            cache_entry,
        )
        latest_api_url = f"https://api.github.com/repos/{repo}/releases/latest"
        response = requests.get(
            self._route_url(route, latest_api_url, "metadata"),
            headers=request_headers,
            timeout=_UPDATE_HTTP_TIMEOUT,
        )
        self._raise_if_cancelled()
        if response.status_code == 304:
            cached_payload = self._cached_version_payload(cache_entry)
            if cached_payload is not None:
                write_component_cache(
                    _component_cache_path(),
                    repo,
                    cached_payload,
                    endpoint=str((cache_entry or {}).get("endpoint") or "latest"),
                    schema_version=_COMPONENT_CACHE_SCHEMA_VERSION,
                    ttl_seconds=_COMPONENT_CACHE_TTL_SECONDS,
                    response_headers={
                        "ETag": (cache_entry or {}).get("etag", ""),
                        "Last-Modified": (cache_entry or {}).get("last_modified", ""),
                    },
                )
                # A 304 means the server validated the cached representation;
                # it is not an offline/stale-cache fallback.
                return cached_payload
            # A stale conditional marker without a usable payload is not a
            # valid update result. Retry once without validators instead of
            # silently treating an empty response as the latest release.
            response = requests.get(
                self._route_url(route, latest_api_url, "metadata"),
                headers=base_headers,
                timeout=_UPDATE_HTTP_TIMEOUT,
            )
            self._raise_if_cancelled()
            if response.status_code == 304:
                raise RuntimeError("GitHub 返回 304，但本地没有可用的版本缓存")
        if response.status_code == 404:
            return self._fetch_tags_fallback(repo, base_headers, route)
        if self._rate_limited(response):
            return self._fetch_rate_limit_fallback(repo, base_headers, route)
        response.raise_for_status()
        payload = response.json()
        self._raise_if_cancelled()
        return self._store_route_payload(
            repo,
            payload,
            route,
            endpoint="latest",
            response_headers=getattr(response, "headers", {}) or {},
        )

    def _fetch_latest_payload(self, repo: str, headers: dict[str, str]) -> dict[str, Any]:
        """Fetch metadata through the short route chain, then local cache."""
        failures: list[str] = []
        for route in self._fast_metadata_routes():
            self._raise_if_cancelled()
            route_kinds = self._route_kinds(route, "metadata")
            for route_kind in route_kinds:
                effective_route = GithubDownloadRoute(
                    route.id,
                    route.name,
                    route.base_url,
                    route.third_party,
                    kind=route_kind,
                    metadata_supported=route.metadata_supported,
                    release_page_supported=route.release_page_supported,
                    asset_supported=route.asset_supported,
                )
                try:
                    return self._fetch_latest_payload_from_route(repo, headers, effective_route)
                except _UpdateCheckCancelled:
                    raise
                except Exception as exc:
                    failures.append(f"{route.name}/{route_kind}：{exc}")

        cache_entry = read_component_cache(
            _component_cache_path(),
            repo,
            schema_version=_COMPONENT_CACHE_SCHEMA_VERSION,
        )
        cached_payload = cache_entry.get("payload") if cache_entry else None
        if isinstance(cached_payload, Mapping) and str(
            cached_payload.get("tag_name") or cached_payload.get("name") or ""
        ).strip():
            cached = dict(cached_payload)
            cached["_metadata_cached"] = True
            warning = str(cached.get("_metadata_warning") or "").strip("；")
            cache_warning = "所有在线线路均不可用，当前显示的是本地缓存元数据"
            cached["_metadata_warning"] = "；".join(
                part for part in (warning, cache_warning) if part
            )
            return cached
        raise RuntimeError("；".join(failures) or "没有可用的 GitHub 元数据线路")

    def _error_result(self, name: str, repo: str, error: Exception | str) -> dict[str, Any]:
        try:
            if name == "汇发视频下载工具":
                installed = _InstalledComponentState(
                    APP_VERSION,
                    "当前程序",
                    str(application_dir()),
                )
            else:
                installed = _InstalledComponentState(*self._installed_component(name))
        except Exception as detection_error:
            installed = _InstalledComponentState(
                "未检测",
                f"本地组件检测失败：{detection_error}",
                "",
            )
        result = {
            "name": name,
            "repo": repo,
            "current": installed.current,
            "source": installed.source,
            "runtime_path": installed.runtime_path,
            "latest": "",
            "url": f"https://github.com/{repo}",
            "assets": [],
            "published_at": "",
            "installed": not installed.missing,
            "has_update": False,
            "install_available": False,
            "auto_install_supported": component_auto_install_supported(name),
            "managed_by_application": component_managed_by_application(name),
            "upstream_update_available": False,
            "error": str(error),
        }
        if normalize_runtime_component(name) == "ffmpeg":
            result.update({
                "ffmpeg_build_channel": self.ffmpeg_build_channel,
                "channel_switch_required": False,
            })
        return result

    def _ffmpeg_rolling_build_version(
        self,
        repo: str,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
        fallback_build_date: str,
    ) -> str:
        autobuild_tag = _ffmpeg_autobuild_tag(payload)
        if not autobuild_tag:
            release_url = f"https://github.com/{repo}/releases/tag/latest"
            for route in self._release_page_routes():
                for route_kind in self._route_kinds(route, "asset"):
                    self._raise_if_cancelled()
                    try:
                        response = requests.get(
                            route_download_url(route, release_url, route_kind),
                            headers=self._headers_for_route(headers, route),
                            timeout=_UPDATE_HTTP_TIMEOUT,
                        )
                        response.raise_for_status()
                        autobuild_tag = _ffmpeg_autobuild_tag({
                            "name": response.text or "",
                        })
                    except (AttributeError, OSError, requests.RequestException, ValueError):
                        continue
                    if autobuild_tag:
                        break
                if autobuild_tag:
                    break
        tag_date_match = re.search(
            r"autobuild-(20\d{2})-(\d{2})-(\d{2})-",
            autobuild_tag,
        )
        build_date = (
            "".join(tag_date_match.groups())
            if tag_date_match is not None
            else fallback_build_date
        )
        version = _ffmpeg_build_version_from_assets(
            payload.get("assets") or (),
            build_date,
        )
        if version:
            return version
        if autobuild_tag:
            for route in self._release_page_routes():
                for route_kind in self._route_kinds(route, "asset"):
                    self._raise_if_cancelled()
                    try:
                        assets = release_assets_from_html(
                            repo,
                            autobuild_tag,
                            self._headers_for_route(headers, route),
                            route,
                            route_kind,
                        )
                    except (OSError, requests.RequestException, ValueError):
                        continue
                    version = _ffmpeg_build_version_from_assets(assets, build_date)
                    if version:
                        return version
        return f"latest-{fallback_build_date}" if fallback_build_date else "latest"

    def _installed_component_state(self, name: str) -> _InstalledComponentState:
        if name == "汇发视频下载工具":
            return _InstalledComponentState(
                APP_VERSION,
                "当前程序",
                str(application_dir()),
            )
        return _InstalledComponentState(*self._installed_component(name))

    @staticmethod
    def _moving_release_has_update(
        local_build_date: str,
        remote_build_date: str,
        *,
        missing: bool,
    ) -> bool:
        if missing or not remote_build_date:
            return False
        # A date-less local FFmpeg build cannot be proven equivalent to the
        # moving yt-dlp build. Offer the known dated build instead of silently
        # claiming that an arbitrary system/PATH installation is current.
        return not local_build_date or remote_build_date > local_build_date

    @staticmethod
    def _legacy_ffmpeg_version_state(
        installed: _InstalledComponentState,
    ) -> _ComponentVersionState:
        return _ComponentVersionState(
            display_version=_FFMPEG_NVENC_LEGACY_VERSION,
            local_build_date=_version_build_date(installed.current),
            remote_build_date=_FFMPEG_NVENC_LEGACY_BUILD_DATE,
            rolling_update_available=False,
        )

    def _standard_component_version_state(
        self,
        component_key: str,
        repo: str,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
        installed: _InstalledComponentState,
        compatible_asset: Mapping[str, Any] | None,
        *,
        latest: str,
        rolling: bool,
    ) -> _ComponentVersionState:
        local_build_date, remote_build_date, _comparison = (
            _rolling_release_build_date(
                installed.current,
                payload,
                compatible_asset,
            )
            if rolling else ("", "", False)
        )
        display_version = latest
        if component_key == "ffmpeg" and rolling:
            display_version = self._ffmpeg_rolling_build_version(
                repo,
                payload,
                headers,
                remote_build_date,
            )
            display_build_date = _version_build_date(display_version)
            if display_build_date:
                remote_build_date = display_build_date

        return _ComponentVersionState(
            display_version=display_version,
            local_build_date=local_build_date,
            remote_build_date=remote_build_date,
            rolling_update_available=self._moving_release_has_update(
                local_build_date,
                remote_build_date,
                missing=installed.missing,
            ),
        )

    @staticmethod
    def _ffmpeg_channel_switch_required(
        component_key: str,
        installed: _InstalledComponentState,
        *,
        legacy_ffmpeg: bool,
        rolling: bool,
        remote_build_date: str,
    ) -> bool:
        if component_key != "ffmpeg" or installed.missing:
            return False

        legacy_installed = _ffmpeg_legacy_build_installed(installed.current)
        if legacy_ffmpeg:
            return not legacy_installed
        if not rolling or not legacy_installed:
            return False
        return (
            not remote_build_date
            or remote_build_date != _FFMPEG_NVENC_LEGACY_BUILD_DATE
        )

    def _component_release_state(
        self,
        name: str,
        component_key: str,
        repo: str,
        payload: Mapping[str, Any],
        headers: Mapping[str, str],
        installed: _InstalledComponentState,
        *,
        legacy_ffmpeg: bool,
    ) -> _ComponentReleaseState:
        latest = str(payload.get("tag_name") or payload.get("name") or "")
        assets = [
            dict(asset)
            for asset in (payload.get("assets") or ())
            if isinstance(asset, Mapping)
        ]
        compatible_asset = select_release_asset(name, assets)
        rolling = latest.strip().casefold() in {"latest", "master", "main"}
        version = (
            self._legacy_ffmpeg_version_state(installed)
            if legacy_ffmpeg
            else self._standard_component_version_state(
                component_key,
                repo,
                payload,
                headers,
                installed,
                compatible_asset,
                latest=latest,
                rolling=rolling,
            )
        )

        return _ComponentReleaseState(
            latest=latest,
            display_version=version.display_version,
            assets=assets,
            compatible_asset=compatible_asset,
            rolling=rolling,
            local_build_date=version.local_build_date,
            remote_build_date=version.remote_build_date,
            rolling_update_available=version.rolling_update_available,
            channel_switch_required=self._ffmpeg_channel_switch_required(
                component_key,
                installed,
                legacy_ffmpeg=legacy_ffmpeg,
                rolling=rolling,
                remote_build_date=version.remote_build_date,
            ),
        )

    @staticmethod
    def _component_update_decision(
        name: str,
        component_key: str,
        installed: _InstalledComponentState,
        release: _ComponentReleaseState,
    ) -> _ComponentUpdateDecision:
        managed_by_application = component_managed_by_application(name)
        auto_install_supported = component_auto_install_supported(name)
        version_update_available = (
            bool(release.latest)
            and normalize_version(release.latest)
            > normalize_version(installed.current)
        )
        upstream_update_available = (
            managed_by_application
            and not release.rolling
            and not installed.missing
            and version_update_available
        )
        if managed_by_application or installed.missing:
            has_update = False
        elif component_key == "ffmpeg" and release.channel_switch_required:
            has_update = True
        elif release.rolling:
            has_update = release.rolling_update_available
        else:
            has_update = version_update_available
        return _ComponentUpdateDecision(
            has_update=has_update,
            install_available=(
                bool(release.latest)
                and installed.missing
                and release.compatible_asset is not None
                and auto_install_supported
            ),
            auto_install_supported=auto_install_supported,
            managed_by_application=managed_by_application,
            upstream_update_available=upstream_update_available,
        )

    @staticmethod
    def _component_result_url(repo: str, payload: Mapping[str, Any]) -> str:
        value = payload.get("html_url")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return f"https://github.com/{repo}/releases"

    def _component_result(
        self,
        name: str,
        repo: str,
        component_key: str,
        payload: Mapping[str, Any],
        installed: _InstalledComponentState,
        release: _ComponentReleaseState,
        decision: _ComponentUpdateDecision,
    ) -> dict[str, Any]:
        return {
            "name": name,
            "repo": repo,
            "current": installed.current,
            "source": installed.source,
            "runtime_path": installed.runtime_path,
            "latest": release.display_version,
            "url": self._component_result_url(repo, payload),
            "assets": release.assets,
            "published_at": str(payload.get("published_at") or ""),
            "metadata_route": str(payload.get("_metadata_route") or ROUTE_DIRECT),
            "metadata_route_name": str(
                payload.get("_metadata_route_name") or "GitHub 直连"
            ),
            "metadata_third_party": bool(payload.get("_metadata_third_party")),
            "metadata_cached": bool(payload.get("_metadata_cached")),
            "metadata_warning": str(payload.get("_metadata_warning") or ""),
            "installed": not installed.missing,
            "has_update": decision.has_update,
            "install_available": decision.install_available,
            "auto_install_supported": decision.auto_install_supported,
            "managed_by_application": decision.managed_by_application,
            "upstream_update_available": decision.upstream_update_available,
            "rolling_release": release.rolling,
            "local_build_date": release.local_build_date,
            "remote_build_date": release.remote_build_date,
            "ffmpeg_build_channel": (
                self.ffmpeg_build_channel if component_key == "ffmpeg" else ""
            ),
            "channel_switch_required": release.channel_switch_required,
        }

    def _check_component(self, name: str, repo: str, headers: dict[str, str]) -> dict[str, Any]:
        """Check one independent component; safe to call from the bounded pool."""
        component_key = normalize_runtime_component(name)
        legacy_ffmpeg = (
            component_key == "ffmpeg"
            and self.ffmpeg_build_channel == FFMPEG_BUILD_NVENC_LEGACY
        )
        if legacy_ffmpeg:
            payload = _ffmpeg_nvenc_legacy_release_payload()
        else:
            try:
                payload = self._fetch_latest_payload(repo, headers)
            except _UpdateCheckCancelled:
                raise
            except Exception as exc:
                self._raise_if_cancelled()
                return self._error_result(name, repo, exc)

        self._raise_if_cancelled()
        payload = dict(payload)
        installed = self._installed_component_state(name)
        self._raise_if_cancelled()
        release = self._component_release_state(
            name,
            component_key,
            repo,
            payload,
            headers,
            installed,
            legacy_ffmpeg=legacy_ffmpeg,
        )
        decision = self._component_update_decision(
            name,
            component_key,
            installed,
            release,
        )
        return self._component_result(
            name,
            repo,
            component_key,
            payload,
            installed,
            release,
            decision,
        )

    @Slot()
    def run(self) -> None:
        try:
            entries = [
                (name, repo)
                for name, repo in self.repos.items()
                if component_visible_in_update_list(name)
            ]
            if self.app_repo:
                entries.append(("汇发视频下载工具", self.app_repo))
            if not entries:
                self.finished.emit([])
                return

            headers = self._request_headers()
            ordered_results: list[dict[str, Any] | None] = [None] * len(entries)
            max_workers = min(_UPDATE_CHECK_MAX_WORKERS, len(entries))

            def check_entry(index: int, name: str, repo: str) -> tuple[int, dict[str, Any]]:
                self._raise_if_cancelled()
                try:
                    result = self._check_component(name, repo, headers)
                except _UpdateCheckCancelled:
                    raise
                except Exception as exc:
                    # One repository failure must not discard healthy results.
                    result = self._error_result(name, repo, exc)
                return index, result

            def publish_result(_job_index: int, value: tuple[int, dict[str, Any]]) -> None:
                result_index, result = value
                ordered_results[result_index] = result
                self.result_ready.emit(result)

            try:
                run_disposable_jobs(
                    [
                        lambda index=index, name=name, repo=repo: check_entry(index, name, repo)
                        for index, (name, repo) in enumerate(entries)
                    ],
                    max_workers=max_workers,
                    cancel_event=self._cancelled,
                    thread_name_prefix="huifa-update-check",
                    on_result=publish_result,
                )
            except (InterruptedError, _UpdateCheckCancelled):
                self.cancelled.emit()
                return

            self.finished.emit([result for result in ordered_results if result is not None])
        except Exception as exc:
            self.failed.emit(str(exc))


class GithubRouteProbeWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        official_url: str,
        routes: tuple[GithubDownloadRoute, ...],
        has_digest: bool = False,
        require_digest: bool = False,
    ):
        super().__init__()
        self.official_url = official_url
        self.routes = routes
        self.has_digest = bool(has_digest)
        self.require_digest = bool(require_digest)
        self._cancelled = threading.Event()

    @staticmethod
    def _probe_request(url: str, accept: str) -> tuple[bool, int, str]:
        started = time.monotonic()
        try:
            with requests.get(
                url,
                headers={
                    "User-Agent": "HuifaVideoDownloader",
                    "Accept": accept,
                    "Range": "bytes=0-0",
                },
                stream=True,
                timeout=(3, 5),
            ) as response:
                response.raise_for_status()
                latency = max(1, round((time.monotonic() - started) * 1000))
                return True, latency, ""
        except Exception as exc:
            return False, 0, str(exc)

    @staticmethod
    def _capability_status(metadata_ok: bool, asset_ok: bool) -> str:
        if metadata_ok and asset_ok:
            return "可用（元数据与附件）"
        if metadata_ok:
            return "可用（仅元数据）"
        if asset_ok:
            return "可用（仅附件）"
        return "不可用"

    def _probe_jsdelivr_route(self, route: GithubDownloadRoute) -> dict[str, Any]:
        metadata_ok, metadata_latency, metadata_error = self._probe_request(
            "https://data.jsdelivr.com/v1/package/gh/yt-dlp/yt-dlp",
            "application/json",
        )
        cdn_ok, cdn_latency, cdn_error = self._probe_request(
            route.base_url + "gh/yt-dlp/yt-dlp@2026.08.19/README.md",
            "text/plain,*/*",
        )
        errors = [
            value
            for value in (
                "Public API：" + metadata_error if metadata_error else "",
                "CDN：" + cdn_error if cdn_error else "",
            )
            if value
        ]
        return {
            "id": route.id,
            "name": route.name,
            "url": route.base_url,
            "third_party": True,
            "latency_ms": metadata_latency or cdn_latency,
            "status": (
                "可用（元数据与 CDN）"
                if metadata_ok and cdn_ok
                else "可用（仅元数据）" if metadata_ok else "不可用"
            ),
            "usable": metadata_ok,
            "error": "；".join(errors),
            "metadata_only": True,
            "metadata_ok": metadata_ok,
            "asset_ok": False,
            "cdn_ok": cdn_ok,
            "metadata_latency_ms": metadata_latency,
            "asset_latency_ms": 0,
            "cdn_latency_ms": cdn_latency,
            "metadata_kind": "jsdelivr",
            "asset_kind": "",
            "detected_kind": "jsdelivr",
            "tested_at": int(time.time()),
        }

    @staticmethod
    def _route_probe_kinds(route: GithubDownloadRoute) -> tuple[str, ...]:
        if not route.third_party:
            return ("direct",)
        if route.kind in {"prefix", "host"}:
            return (route.kind,)
        return ("prefix", "host")

    @staticmethod
    def _build_route_attempts(
        route: GithubDownloadRoute,
        metadata_official: str,
        asset_official: str,
    ) -> list[dict[str, Any]]:
        attempts: list[dict[str, Any]] = []
        for kind in GithubRouteProbeWorker._route_probe_kinds(route):
            attempt: dict[str, Any] = {"kind": kind}
            try:
                attempt["metadata_url"] = route_download_url(
                    route, metadata_official, kind
                )
                attempt["asset_url"] = route_download_url(route, asset_official, kind)
            except ValueError as exc:
                attempt["metadata_error"] = str(exc)
                attempt["asset_error"] = str(exc)
            attempts.append(attempt)
        return attempts

    def _probe_route_attempts(self, attempts: list[dict[str, Any]]) -> None:
        jobs: list[Callable[[], Any]] = []
        for attempt_index, attempt in enumerate(attempts):
            for capability, accept in (
                ("asset", "application/octet-stream"),
                ("metadata", "application/vnd.github+json"),
            ):
                url = str(attempt.get(f"{capability}_url") or "")
                if not url:
                    continue
                jobs.append(
                    lambda attempt_index=attempt_index, capability=capability,
                    url=url, accept=accept: (
                        attempt_index,
                        capability,
                        *self._probe_request(url, accept),
                    )
                )
        for attempt_index, capability, ok, latency, error in run_disposable_jobs(
            jobs,
            max_workers=max(1, len(jobs)),
            cancel_event=self._cancelled,
            thread_name_prefix="github-route-capability",
        ):
            attempt = attempts[attempt_index]
            attempt[f"{capability}_ok"] = ok
            attempt[f"{capability}_latency_ms"] = latency
            attempt[f"{capability}_error"] = error

    @staticmethod
    def _best_capability_attempt(
        attempts: list[dict[str, Any]],
        capability: str,
    ) -> dict[str, Any]:
        return min(
            attempts,
            key=lambda item: (
                not bool(item.get(f"{capability}_ok")),
                int(item.get(f"{capability}_latency_ms") or 10**9),
            ),
        )

    @classmethod
    def _standard_route_result(
        cls,
        route: GithubDownloadRoute,
        attempts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        metadata = cls._best_capability_attempt(attempts, "metadata")
        asset = cls._best_capability_attempt(attempts, "asset")
        metadata_ok = bool(metadata.get("metadata_ok"))
        asset_ok = bool(asset.get("asset_ok"))
        metadata_kind = str(metadata.get("kind") or route.kind)
        asset_kind = str(asset.get("kind") or route.kind)
        successful_kinds = {
            kind
            for ok, kind in (
                (metadata_ok, metadata_kind),
                (asset_ok, asset_kind),
            )
            if ok and kind
        }
        detected_kind = (
            next(iter(successful_kinds))
            if len(successful_kinds) == 1
            else route.kind
        )
        errors = [
            value
            for value in (
                "元数据：" + str(metadata.get("metadata_error") or "")
                if not metadata_ok and metadata.get("metadata_error") else "",
                "附件：" + str(asset.get("asset_error") or "")
                if not asset_ok and asset.get("asset_error") else "",
            )
            if value
        ]
        latencies = [
            int(value)
            for ok, value in (
                (metadata_ok, metadata.get("metadata_latency_ms")),
                (asset_ok, asset.get("asset_latency_ms")),
            )
            if ok and int(value or 0) > 0
        ]
        return {
            "id": route.id,
            "name": route.name,
            "url": route.base_url or "https://github.com/",
            "third_party": route.third_party,
            "latency_ms": min(latencies) if latencies else 0,
            "status": cls._capability_status(metadata_ok, asset_ok),
            "usable": metadata_ok or asset_ok,
            "error": "；".join(errors),
            "metadata_only": metadata_ok and not asset_ok,
            "metadata_ok": metadata_ok,
            "asset_ok": asset_ok,
            "metadata_latency_ms": int(metadata.get("metadata_latency_ms") or 0),
            "asset_latency_ms": int(asset.get("asset_latency_ms") or 0),
            "metadata_kind": metadata_kind,
            "asset_kind": asset_kind,
            "detected_kind": detected_kind,
            "tested_at": int(time.time()),
        }

    def _probe_route(self, route: GithubDownloadRoute) -> dict[str, Any]:
        metadata_official = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
        asset_official = (
            self.official_url
            if is_supported_github_download_url(self.official_url)
            else "https://github.com/yt-dlp/yt-dlp/releases/download/2026.08.19/yt-dlp.exe"
        )
        if route.kind == "jsdelivr":
            return self._probe_jsdelivr_route(route)

        attempts = self._build_route_attempts(
            route,
            metadata_official,
            asset_official,
        )
        self._probe_route_attempts(attempts)
        return self._standard_route_result(route, attempts)

    def cancel(self) -> None:
        self._cancelled.set()

    @Slot()
    def run(self) -> None:
        try:
            max_workers = min(_BACKGROUND_ROUTE_PROBE_MAX_WORKERS, max(1, len(self.routes)))
            results = run_disposable_jobs(
                [lambda route=route: self._probe_route(route) for route in self.routes],
                max_workers=max_workers,
                cancel_event=self._cancelled,
                thread_name_prefix="github-route-probe",
            )
            self.finished.emit(results)
        except InterruptedError:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))


class ToolInstallWorker(QObject):
    finished = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, component: str, source_path: str | Path):
        super().__init__()
        self.component = component
        self.source_path = Path(source_path)
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    @Slot()
    def run(self) -> None:
        try:
            result = install_tool_component(self.component, self.source_path, self._cancelled)
            if self._cancelled.is_set():
                self.cancelled.emit()
            else:
                self.finished.emit(result)
        except InterruptedError:
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))


@dataclass(frozen=True, slots=True)
class _UpdateRuntime:
    kind: str
    thread: QThread
    worker: QObject


class UpdateService(QObject):
    _RUNTIME_KINDS = frozenset({"check", "download", "install", "route_probe"})
    _RUNTIME_SHUTDOWN_ORDER = ("route_probe", "install", "download", "check")

    result_ready = Signal(object)
    finished = Signal(object)
    failed = Signal(str)
    download_finished = Signal(str)
    download_failed = Signal(str)
    install_finished = Signal(object)
    install_failed = Signal(str)
    route_probe_finished = Signal(object)
    route_probe_failed = Signal(str)

    def __init__(
        self,
        updates_dir: str | Path,
        tool_overrides: dict[str, str] | None = None,
        ffmpeg_build_channel: str = FFMPEG_BUILD_LATEST,
    ):
        super().__init__()
        self.updates_dir = Path(updates_dir)
        self.updates_dir.mkdir(parents=True, exist_ok=True)
        self._runtimes: dict[str, _UpdateRuntime] = {}
        self._deferred_thread_finishes: set[tuple[str, QThread]] = set()
        self._download_component = ""
        self._shutting_down = False
        self.tool_overrides: dict[str, str] = {}
        self.ffmpeg_build_channel = normalize_ffmpeg_build_channel(ffmpeg_build_channel)
        self.github_route_mode = ROUTE_AUTO
        self.github_mirror_urls = ""
        self.route_probe_results: dict[str, dict[str, Any]] = {}
        self.last_results: list[dict[str, Any]] = []
        self._background_route_probe_started = False
        self._background_route_probe_last_attempt = 0.0
        self.set_tool_overrides(tool_overrides or {})

    def _prepare_runtime(
        self,
        kind: str,
        worker: QObject,
        outcome_connections: tuple[tuple[Any, Any], ...],
    ) -> _UpdateRuntime:
        """Build and fully wire a worker before publishing busy state."""
        if kind not in self._RUNTIME_KINDS:
            raise ValueError(f"未知的更新运行时类型：{kind}")
        thread = QThread(self)
        runtime = _UpdateRuntime(kind, thread, worker)
        try:
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            for signal, receiver in outcome_connections:
                signal.connect(receiver, Qt.QueuedConnection)
            self._connect_runtime_lifecycle(runtime)
        except Exception:
            delete_unstarted_worker(worker, thread)
            raise
        return runtime

    def _connect_runtime_lifecycle(self, runtime: _UpdateRuntime) -> None:
        thread = runtime.thread
        worker = runtime.worker
        for terminal_signal in (
            worker.finished,
            worker.failed,
            worker.cancelled,
        ):
            terminal_signal.connect(thread.quit)
            terminal_signal.connect(worker.deleteLater)
        thread.setProperty("update_runtime_kind", runtime.kind)
        thread.finished.connect(
            self._runtime_thread_finished_from_signal,
            Qt.QueuedConnection,
        )

    def _publish_runtime(self, runtime: _UpdateRuntime) -> None:
        if runtime.kind in self._runtimes:
            raise RuntimeError("更新运行时已被占用")
        self._runtimes[runtime.kind] = runtime

    def runtime_active(self, *kinds: str) -> bool:
        selected = kinds or tuple(self._RUNTIME_KINDS)
        return any(kind in self._runtimes for kind in selected)

    def _start_runtime(
        self,
        runtime: _UpdateRuntime,
        *,
        error_prefix: str,
        emit_error: Callable[[str], None],
    ) -> bool:
        try:
            self._publish_runtime(runtime)
            runtime.thread.start()
        except Exception as exc:
            self._discard_failed_runtime_start(
                runtime.kind,
                runtime.thread,
                runtime.worker,
            )
            emit_error(f"{error_prefix}：{exc}")
            return False
        return True

    def set_tool_overrides(self, overrides: dict[str, str]) -> None:
        self.tool_overrides = {
            normalize_runtime_component(name): str(value or "").strip()
            for name, value in overrides.items()
        }

    def set_ffmpeg_build_channel(self, channel: str) -> None:
        normalized = normalize_ffmpeg_build_channel(channel)
        if normalized == self.ffmpeg_build_channel:
            return
        self.ffmpeg_build_channel = normalized
        self.last_results = []

    def set_download_routes(
        self,
        route_mode: str = ROUTE_AUTO,
        custom_urls: str = "",
        route_profiles: str | Mapping[str, Any] = "",
    ) -> None:
        normalized_urls = parse_custom_mirror_urls(custom_urls)
        normalized_mode = normalize_github_route(route_mode)
        normalized_url_text = "\n".join(normalized_urls)
        route_configuration_changed = (
            normalized_mode != self.github_route_mode
            or normalized_url_text != self.github_mirror_urls
        )
        self.github_route_mode = normalized_mode
        self.github_mirror_urls = normalized_url_text
        if route_configuration_changed:
            self._background_route_probe_started = False
            self._background_route_probe_last_attempt = 0.0
        raw_profiles: Any = route_profiles
        if isinstance(route_profiles, str):
            try:
                raw_profiles = json.loads(route_profiles or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                raw_profiles = {}
        if isinstance(raw_profiles, Mapping):
            self.route_probe_results = {
                str(route_id): dict(profile)
                for route_id, profile in raw_profiles.items()
                if isinstance(profile, Mapping)
            }

    def serialized_route_profiles(self) -> str:
        safe: dict[str, dict[str, Any]] = {}
        allowed = {
            "id", "name", "url", "third_party", "latency_ms", "status",
            "usable", "error", "metadata_only", "metadata_ok", "asset_ok",
            "metadata_latency_ms", "asset_latency_ms", "metadata_kind",
            "asset_kind", "detected_kind", "tested_at",
            "cdn_ok", "cdn_latency_ms",
        }
        for route_id, profile in self.route_probe_results.items():
            safe[str(route_id)] = {
                key: value for key, value in profile.items() if key in allowed
            }
        return json.dumps(safe, ensure_ascii=False, separators=(",", ":"))

    def available_download_routes(self) -> tuple[GithubDownloadRoute, ...]:
        return github_download_routes(self.github_mirror_urls)

    def route_url(
        self,
        route: GithubDownloadRoute,
        official_url: str,
        capability: str = "asset",
    ) -> str:
        profile = self.route_probe_results.get(route.id, {})
        kind = str(
            profile.get(f"{capability}_kind")
            or profile.get("detected_kind")
            or route.kind
        ).strip().casefold()
        return route_download_url(route, official_url, kind)

    def route_kinds(self, route: GithubDownloadRoute, capability: str = "asset") -> tuple[str, ...]:
        profile = self.route_probe_results.get(route.id, {})
        kind = str(
            profile.get(f"{capability}_kind")
            or profile.get("detected_kind")
            or route.kind
        ).strip().casefold()
        return ("prefix", "host") if kind == "auto" else (kind,)

    @property
    def active_thread_count(self) -> int:
        # A stopped worker still owns an operation until its queued outcome
        # and cleanup callbacks are delivered on this service's thread.
        return len(self._runtimes)

    def probe_download_routes(
        self,
        asset: Mapping[str, Any] | None = None,
        *,
        routes: tuple[GithubDownloadRoute, ...] | None = None,
    ) -> bool:
        if self._shutting_down or self.runtime_active("route_probe"):
            return False
        if asset is None:
            official_url = "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
            has_digest = False
            require_digest = False
        else:
            official_url = str(asset.get("browser_download_url") or "")
            if not is_supported_github_download_url(official_url):
                self.route_probe_failed.emit("没有可用于测速的官方 GitHub Release 附件")
                return False
            digest = str(asset.get("digest") or "")
            has_digest = bool(re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest))
            require_digest = True
        selected_routes = tuple(routes) if routes is not None else self.available_download_routes()
        if not selected_routes:
            return False
        try:
            worker = GithubRouteProbeWorker(
                official_url,
                selected_routes,
                has_digest,
                require_digest,
            )
            runtime = self._prepare_runtime(
                "route_probe",
                worker,
                (
                    (worker.finished, self._route_probe_completed),
                    (worker.failed, self.route_probe_failed),
                ),
            )
        except Exception as exc:
            self.route_probe_failed.emit(f"无法准备更新线路检测线程：{exc}")
            return False
        return self._start_runtime(
            runtime,
            error_prefix="无法启动更新线路检测线程",
            emit_error=self.route_probe_failed.emit,
        )

    def start_background_route_probe(self) -> None:
        """Refresh stale route health and allow a failed probe to retry later."""
        if self._shutting_down or self._background_route_probe_started:
            return
        now = time.time()
        stale_routes: list[GithubDownloadRoute] = []
        for route in self.available_download_routes():
            row = self.route_probe_results.get(route.id, {})
            tested_at = int(row.get("tested_at") or 0)
            expected_url = route.base_url or "https://github.com/"
            if (
                not tested_at
                or now - tested_at >= _BACKGROUND_ROUTE_PROBE_FRESH_SECONDS
                or str(row.get("url") or "") != expected_url
            ):
                stale_routes.append(route)
        if not stale_routes:
            return
        if now - self._background_route_probe_last_attempt < _BACKGROUND_ROUTE_PROBE_RETRY_SECONDS:
            return
        if self.probe_download_routes(routes=tuple(stale_routes)):
            self._background_route_probe_started = True
            self._background_route_probe_last_attempt = now

    @Slot(object)
    def _route_probe_completed(self, results: list[dict[str, Any]]) -> None:
        current_ids = {route.id for route in self.available_download_routes()}
        merged = {
            route_id: dict(row)
            for route_id, row in self.route_probe_results.items()
            if route_id in current_ids
        }
        merged.update({str(row.get("id") or ""): dict(row) for row in results})
        self.route_probe_results = merged
        self.route_probe_finished.emit(results)

    def _asset_route_health_key(
        self,
        route: GithubDownloadRoute,
    ) -> tuple[bool, int, str]:
        profile = self.route_probe_results.get(route.id, {})
        return (
            not bool(profile.get("asset_ok")),
            int(profile.get("asset_latency_ms") or 10**9),
            route.name.casefold(),
        )

    def _automatic_asset_routes(
        self,
        routes: Iterable[GithubDownloadRoute],
    ) -> list[GithubDownloadRoute]:
        available = list(routes)
        direct = [route for route in available if route.id == ROUTE_DIRECT]
        mirrors = [route for route in available if route.id != ROUTE_DIRECT]
        mirrors.sort(key=self._asset_route_health_key)
        return [*direct, *mirrors]

    def _eligible_asset_routes(
        self,
        digest: str,
        *,
        allow_unverified_third_party: bool,
    ) -> list[GithubDownloadRoute]:
        has_digest = bool(re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest))
        selected = list(selected_download_routes(
            self.github_route_mode,
            self.github_mirror_urls,
        ))
        routes = [route for route in selected if route.asset_supported]
        automatic_fallback = self.github_route_mode == ROUTE_AUTO or not routes
        if not routes:
            # Metadata-only routes such as jsDelivr cannot carry GitHub
            # Release attachments. Fall back to the normal attachment routes.
            routes = [
                route
                for route in self.available_download_routes()
                if route.asset_supported
            ]

        selected_route = routes[0] if not automatic_fallback and routes else None
        if selected_route is not None and selected_route.third_party:
            if not has_digest and not allow_unverified_third_party:
                raise ValueError(
                    "所选第三方 GitHub 线路需要发布方 SHA-256；"
                    "请改用自动或 GitHub 直连"
                )
            # A manually selected mirror remains first, but an official
            # fallback avoids turning a temporary outage into a dead button.
            routes.extend(
                route
                for route in self.available_download_routes()
                if route.id == ROUTE_DIRECT and route.asset_supported
            )
            return routes

        if not has_digest and not allow_unverified_third_party:
            routes = [route for route in routes if not route.third_party]
        if automatic_fallback:
            return self._automatic_asset_routes(routes)
        return routes

    @staticmethod
    def _asset_candidate_name(
        route: GithubDownloadRoute,
        route_kind: str,
        kind_count: int,
    ) -> str:
        if kind_count <= 1:
            return route.name
        rule = "主机替换" if route_kind == "host" else "完整 URL 前缀"
        return f"{route.name} · {rule}"

    def _build_asset_download_candidates(
        self,
        routes: Iterable[GithubDownloadRoute],
        official_url: str,
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for route in routes:
            route_kinds = self.route_kinds(route, "asset")
            for route_kind in route_kinds:
                url = route_download_url(route, official_url, route_kind)
                if url in seen:
                    continue
                seen.add(url)
                candidates.append({
                    "url": url,
                    "name": self._asset_candidate_name(
                        route,
                        route_kind,
                        len(route_kinds),
                    ),
                    "route_id": route.id,
                    "route_kind": route_kind,
                    "third_party": route.third_party,
                })
        return candidates

    def _asset_download_candidates(
        self,
        official_url: str,
        digest: str,
        *,
        allow_unverified_third_party: bool = False,
    ) -> list[dict[str, Any]]:
        routes = self._eligible_asset_routes(
            digest,
            allow_unverified_third_party=allow_unverified_third_party,
        )
        return self._build_asset_download_candidates(routes, official_url)

    def check(
        self,
        app_repo: str = "",
        *,
        repos: Mapping[str, str] | None = None,
    ) -> bool:
        if self._shutting_down or self.runtime_active("check"):
            return False
        try:
            worker = UpdateWorker(
                dict(GITHUB_RELEASE_REPOS if repos is None else repos),
                app_repo,
                self.tool_overrides,
                self.github_route_mode,
                self.github_mirror_urls,
                self.route_probe_results,
                self.ffmpeg_build_channel,
            )
            runtime = self._prepare_runtime(
                "check",
                worker,
                (
                    (worker.result_ready, self.result_ready),
                    (worker.finished, self._check_completed),
                    (worker.failed, self.failed),
                ),
            )
        except Exception as exc:
            self.failed.emit(f"无法准备更新检查线程：{exc}")
            return False
        return self._start_runtime(
            runtime,
            error_prefix="无法启动更新检查线程",
            emit_error=self.failed.emit,
        )

    def check_component(self, component: str) -> bool:
        """Check one runtime component without waiting on unrelated repositories."""
        requested = normalize_runtime_component(component)
        selected = {
            name: repo
            for name, repo in GITHUB_RELEASE_REPOS.items()
            if normalize_runtime_component(name) == requested
        }
        if not selected:
            self.failed.emit(f"不支持检查运行组件：{component}")
            return False
        return self.check(repos=selected)

    @Slot(object)
    def _check_completed(self, results: list[dict[str, Any]]) -> None:
        filtered_results = [
            dict(result)
            for result in results
            if normalize_runtime_component(str(result.get("name") or "")) != "ffmpeg"
            or normalize_ffmpeg_build_channel(result.get("ffmpeg_build_channel"))
            == self.ffmpeg_build_channel
        ]
        self.last_results = filtered_results
        self.finished.emit(filtered_results)

    def download_asset(self, asset: dict[str, Any], component: str = "") -> None:
        if self._shutting_down:
            self.download_failed.emit("程序正在退出，不能开始下载更新资源")
            return
        url = str(asset.get("browser_download_url") or "")
        name = Path(str(asset.get("name") or "update.bin")).name
        if not url:
            self.download_failed.emit("更新资源没有可用下载地址")
            return
        source_install = bool(asset.get("source_install"))
        if not is_supported_github_download_url(url) and not (
            source_install and is_supported_github_source_archive_url(url)
        ):
            self.download_failed.emit("更新资源地址不是受支持的 GitHub HTTPS 下载地址")
            return
        if self.runtime_active("download", "install"):
            self.download_failed.emit("已有更新资源正在下载")
            return
        digest = str(asset.get("digest") or "")
        try:
            expected_size = normalize_expected_download_size(asset.get("size"))
            candidates = self._asset_download_candidates(
                url,
                digest,
                allow_unverified_third_party=bool(asset.get("source_install")),
            )
        except ValueError as exc:
            self.download_failed.emit(str(exc))
            return
        if not candidates:
            self.download_failed.emit("没有符合安全策略的更新下载线路")
            return
        target = self.updates_dir / name
        try:
            worker = AssetDownloadWorker(
                candidates,
                target,
                digest,
                expected_size=expected_size,
                allow_unverified_third_party=source_install,
                allow_source_archive=source_install,
            )
            runtime = self._prepare_runtime(
                "download",
                worker,
                (
                    (worker.finished, self._asset_downloaded),
                    (worker.failed, self.download_failed),
                ),
            )
        except Exception as exc:
            self.download_failed.emit(f"无法准备更新下载线程：{exc}")
            return
        self._download_component = str(component or "").strip()
        self._start_runtime(
            runtime,
            error_prefix="无法启动更新下载线程",
            emit_error=self.download_failed.emit,
        )

    @Slot(str)
    def _asset_downloaded(self, path: str) -> None:
        component = self._download_component
        self._download_component = ""
        if self._shutting_down:
            return
        if not component_auto_install_supported(component):
            self.download_finished.emit(path)
            return
        if self.runtime_active("install"):
            self.install_failed.emit("已有工具安装任务正在运行")
            return
        try:
            worker = ToolInstallWorker(component, path)
            runtime = self._prepare_runtime(
                "install",
                worker,
                (
                    (worker.finished, self._install_completed),
                    (worker.failed, self.install_failed),
                ),
            )
        except Exception as exc:
            self.install_failed.emit(f"无法准备工具安装线程：{exc}")
            return
        self._start_runtime(
            runtime,
            error_prefix="无法启动工具安装线程",
            emit_error=self.install_failed.emit,
        )

    @Slot()
    def _runtime_thread_finished_from_signal(self) -> None:
        thread = self.sender()
        if not isinstance(thread, QThread):
            return
        kind = str(thread.property("update_runtime_kind") or "")
        if kind in {"route_probe", "check", "download", "install"}:
            self._defer_runtime_cleanup(kind, thread)

    def _defer_runtime_cleanup(self, kind: str, thread: QThread) -> None:
        """Keep runtime ownership until queued worker outcomes have landed."""
        key = (kind, thread)
        if key in self._deferred_thread_finishes:
            return
        self._deferred_thread_finishes.add(key)
        QTimer.singleShot(0, partial(self._complete_runtime_cleanup, kind, thread))

    def _complete_runtime_cleanup(self, kind: str, thread: QThread) -> None:
        self._deferred_thread_finishes.discard((kind, thread))
        self._clear_runtime_references(kind, thread)
        try:
            thread.deleteLater()
        except RuntimeError:
            pass

    def _clear_runtime_references(self, kind: str, thread: QThread | None = None) -> None:
        runtime = self._runtimes.get(kind)
        if runtime is None:
            return
        if thread is not None and runtime.thread is not thread:
            return
        del self._runtimes[kind]
        if kind == "download":
            self._download_component = ""
        elif kind == "route_probe":
            self._background_route_probe_started = False

    def _discard_failed_runtime_start(
        self,
        kind: str,
        thread: QThread,
        worker: QObject,
    ) -> None:
        self._deferred_thread_finishes.discard((kind, thread))
        self._clear_runtime_references(kind, thread)
        delete_unstarted_worker(worker, thread)

    @Slot(object)
    def _install_completed(self, result: ToolInstallResult) -> None:
        component = normalize_runtime_component(result.component)
        if component == "yt-dlp-ejs":
            activate_local_ejs()
        elif component == "yt-dlp":
            # A previous startup probe may have cached the old executable as
            # unusable. Clear every path variant immediately; the following
            # asynchronous local-version refresh will repopulate the cache.
            clear_external_ytdlp_version_cache()
        self.install_finished.emit(result)

    def request_shutdown(self) -> None:
        """Cooperatively cancel update workers without force-terminating Python."""
        if self._shutting_down:
            return
        self._shutting_down = True
        for kind in self._RUNTIME_SHUTDOWN_ORDER:
            runtime = self._runtimes.get(kind)
            if runtime is None:
                continue
            thread = runtime.thread
            try:
                runtime.worker.cancel()
            except RuntimeError:
                pass
            try:
                if thread.isRunning():
                    thread.requestInterruption()
                    thread.quit()
            except RuntimeError:
                pass

    def shutdown(self, timeout_ms: int = 5000) -> bool:
        """Request shutdown, optionally wait, and preserve references if still live."""
        self.request_shutdown()
        threads = [
            self._runtimes[kind].thread
            for kind in self._RUNTIME_SHUTDOWN_ORDER
            if kind in self._runtimes
        ]
        if timeout_ms > 0 and threads:
            deadline = time.monotonic() + timeout_ms / 1000.0
            for thread in threads:
                remaining = max(0, int((deadline - time.monotonic()) * 1000))
                if thread.isRunning() and remaining > 0:
                    thread.wait(remaining)
        # Real QThreads deliver worker outcomes and finished cleanup through
        # the owning Qt event loop. Retain them until that delivery completes;
        # otherwise the application may close state used by a late callback.
        # Lightweight test doubles have no signal delivery, so they can be
        # synchronously retired once stopped.
        for kind in self._RUNTIME_SHUTDOWN_ORDER:
            runtime = self._runtimes.get(kind)
            if runtime is None:
                continue
            thread = runtime.thread
            if (
                not isinstance(thread, QThread)
                and not thread.isRunning()
            ):
                self._clear_runtime_references(kind)
        return not self._runtimes
