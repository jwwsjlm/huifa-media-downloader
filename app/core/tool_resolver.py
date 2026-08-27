from __future__ import annotations

import os
import platform
import shutil
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from app.core.paths import application_dir, tool_runtime_roots


@dataclass(frozen=True, slots=True)
class RuntimeToolResolution:
    """The executable selected by the same rules used at runtime."""

    component: str
    executable: str
    source: str
    found: bool


_COMPONENT_ALIASES = {
    "yt-dlp": "yt-dlp",
    "ytdlp": "yt-dlp",
    "yt_dlp": "yt-dlp",
    "yt-dlp-ejs": "yt-dlp-ejs",
    "yt_dlp_ejs": "yt-dlp-ejs",
    "ejs": "yt-dlp-ejs",
    "ffmpeg": "ffmpeg",
    "ffprobe": "ffprobe",
    "node": "node.js",
    "node.js": "node.js",
    "nodejs": "node.js",
    "deno": "deno",
}
_COMPONENT_PATH_ENVIRONMENT = {
    "node.js": "YT_DLP_NODE_PATH",
    "deno": "YT_DLP_DENO_PATH",
}


def normalize_runtime_component(component: str) -> str:
    key = str(component or "").strip().lower().replace("_", "-")
    return _COMPONENT_ALIASES.get(key, key)


def _runtime_architecture() -> str:
    machine = platform.machine().casefold()
    if "arm64" in machine or "aarch64" in machine:
        return "arm64"
    return "x64" if struct.calcsize("P") * 8 >= 64 else "x86"


def _relative_candidates(component: str) -> tuple[str, ...]:
    arch = _runtime_architecture()
    if component == "yt-dlp":
        return (
            "yt-dlp.exe",
            "yt_dlp.exe",
            f"tools/yt-dlp/{arch}/yt-dlp.exe",
            "tools/yt-dlp/yt-dlp.exe",
            "yt-dlp/yt-dlp.exe",
        )
    if component == "ffmpeg":
        return (
            "ffmpeg.exe",
            f"tools/ffmpeg/{arch}/ffmpeg.exe",
            "tools/ffmpeg/ffmpeg.exe",
            "ffmpeg/bin/ffmpeg.exe",
        )
    if component == "ffprobe":
        return (
            "ffprobe.exe",
            f"tools/ffmpeg/{arch}/ffprobe.exe",
            "tools/ffmpeg/ffprobe.exe",
            "ffmpeg/bin/ffprobe.exe",
        )
    if component == "node.js":
        return (
            "node.exe",
            f"tools/node/{arch}/node.exe",
            "tools/node/node.exe",
            "node/node.exe",
        )
    if component == "deno":
        return (
            "deno.exe",
            f"tools/deno/{arch}/deno.exe",
            "tools/deno/deno.exe",
            "deno/deno.exe",
        )
    raise ValueError(f"不支持的运行组件：{component}")


def _default_command(component: str) -> str:
    return {
        "yt-dlp": "yt-dlp",
        "ffmpeg": "ffmpeg",
        "ffprobe": "ffprobe",
        "node.js": "node",
        "deno": "deno",
    }[component]


def _unique_roots(paths: Sequence[str | Path]) -> list[Path]:
    unique: list[Path] = []
    for raw in paths:
        path = Path(raw)
        try:
            normalized = path.resolve()
        except OSError:
            normalized = path
        if normalized not in unique:
            unique.append(normalized)
    return unique


def _normalized_root(value: str | Path) -> Path:
    root = Path(value)
    try:
        return root.resolve()
    except OSError:
        return root


def _local_resolution(component: str, root: Path, relative_paths: tuple[str, ...], source_prefix: str) -> RuntimeToolResolution | None:
    for relative in relative_paths:
        candidate = root / Path(relative)
        if candidate.is_file():
            return RuntimeToolResolution(
                component=component,
                executable=str(candidate),
                source=f"{source_prefix} {Path(relative).as_posix()}",
                found=True,
            )
    return None


def _explicit_resolution(
    component: str,
    value: str,
    source_prefix: str,
    path_lookup: Callable[[str], str | None],
    application_root: Path,
) -> RuntimeToolResolution | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    looks_like_path = candidate.is_absolute() or "\\" in raw or "/" in raw
    if not candidate.is_absolute():
        # Settings are portable and therefore always relative to the
        # application directory, never to the process working directory.
        application_candidate = application_root / candidate
        if application_candidate.is_file():
            candidate = application_candidate
        elif looks_like_path:
            return None
    if candidate.is_absolute() and candidate.is_file():
        return RuntimeToolResolution(
            component=component,
            executable=str(candidate),
            source=f"{source_prefix} {candidate.name}",
            found=True,
        )
    if not looks_like_path:
        resolved = path_lookup(raw)
        if resolved:
            executable = Path(resolved)
            return RuntimeToolResolution(
                component=component,
                executable=str(executable),
                source=f"{source_prefix} {executable.name}",
                found=True,
            )
    return None


def _environment_resolution(
    component: str,
    environment: Mapping[str, str],
    path_lookup: Callable[[str], str | None],
    application_root: Path,
) -> RuntimeToolResolution | None:
    environment_name = _COMPONENT_PATH_ENVIRONMENT.get(component, "")
    if not environment_name:
        return None
    return _explicit_resolution(
        component,
        str(environment.get(environment_name, "")),
        f"环境变量 {environment_name}",
        path_lookup,
        application_root,
    )


def _runtime_root_resolution(
    component: str,
    roots: Sequence[str | Path],
    relative_paths: tuple[str, ...],
    application_root: Path,
) -> RuntimeToolResolution | None:
    for root in _unique_roots(roots):
        if root == application_root:
            continue
        resolution = _local_resolution(
            component,
            root,
            relative_paths,
            "程序内置文件",
        )
        if resolution is not None:
            return resolution
    return None


def _system_path_resolution(
    component: str,
    path_lookup: Callable[[str], str | None],
) -> RuntimeToolResolution | None:
    resolved = path_lookup(_default_command(component))
    if not resolved:
        return None
    executable = Path(resolved)
    return RuntimeToolResolution(
        component=component,
        executable=str(executable),
        source=f"系统 PATH {executable.name}",
        found=True,
    )


def resolve_runtime_tool(
    component: str,
    configured: str = "",
    *,
    application_root: str | Path | None = None,
    runtime_roots: Sequence[str | Path] | None = None,
    environment: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> RuntimeToolResolution:
    """Resolve one external runtime with a single deployment-aware policy.

    Files beside ``HuifaVideoDownloader.exe`` (including its ``tools``
    subdirectories) intentionally win. A valid settings override or an
    applicable ``YT_DLP_*_PATH`` override comes next, followed by bundled/persistent roots and
    finally the system ``PATH``. Detection pages and execution call this same
    function so they cannot disagree about the active binary.
    """

    key = normalize_runtime_component(component)
    relative_paths = _relative_candidates(key)
    app_root = _normalized_root(
        application_root if application_root is not None else application_dir()
    )
    path_lookup = which or shutil.which
    env = os.environ if environment is None else environment

    # The user's portable file beside the application is always the clearest
    # deployment choice, even when a stale settings value or PATH entry exists.
    local = _local_resolution(key, app_root, relative_paths, "程序目录")
    if local is not None:
        return local

    explicit = _explicit_resolution(key, configured, "设置路径", path_lookup, app_root)
    if explicit is not None:
        return explicit

    environment_override = _environment_resolution(
        key,
        env,
        path_lookup,
        app_root,
    )
    if environment_override is not None:
        return environment_override

    configured_roots = (
        tool_runtime_roots(app_root)
        if runtime_roots is None
        else runtime_roots
    )
    bundled = _runtime_root_resolution(
        key,
        configured_roots,
        relative_paths,
        app_root,
    )
    if bundled is not None:
        return bundled

    system_path = _system_path_resolution(key, path_lookup)
    if system_path is not None:
        return system_path

    return RuntimeToolResolution(component=key, executable="", source="", found=False)


def resolve_ffprobe_tool(
    configured_ffmpeg: str = "",
    configured_ffprobe: str = "",
    *,
    application_root: str | Path | None = None,
    runtime_roots: Sequence[str | Path] | None = None,
    environment: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] | None = None,
) -> RuntimeToolResolution:
    """Resolve FFprobe paired with the active FFmpeg whenever possible."""
    explicit_ffprobe = str(configured_ffprobe or "").strip()
    if explicit_ffprobe:
        resolution = resolve_runtime_tool(
            "ffprobe",
            explicit_ffprobe,
            application_root=application_root,
            runtime_roots=runtime_roots,
            environment=environment,
            which=which,
        )
        if resolution.found:
            return resolution
    ffmpeg = resolve_runtime_tool(
        "ffmpeg",
        configured_ffmpeg,
        application_root=application_root,
        runtime_roots=runtime_roots,
        environment=environment,
        which=which,
    )
    if ffmpeg.found:
        executable = Path(ffmpeg.executable)
        sibling_names = (
            ("ffprobe.exe", "ffprobe")
            if executable.suffix.casefold() == ".exe"
            else ("ffprobe", "ffprobe.exe")
        )
        for name in sibling_names:
            sibling = executable.with_name(name)
            if sibling.is_file():
                return RuntimeToolResolution(
                    component="ffprobe",
                    executable=str(sibling),
                    source=f"{ffmpeg.source} 配套 {sibling.name}",
                    found=True,
                )
    return resolve_runtime_tool(
        "ffprobe",
        application_root=application_root,
        runtime_roots=runtime_roots,
        environment=environment,
        which=which,
    )
