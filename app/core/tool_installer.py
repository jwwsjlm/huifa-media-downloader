from __future__ import annotations

import os
import re
import shutil
import struct
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.core.local_components import wheel_distribution_version
from app.core.paths import application_dir, data_dir
from app.core.ytdlp_ejs import required_ytdlp_ejs_version, ytdlp_ejs_version_compatible


class ToolInstallError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ToolInstallResult:
    component: str
    paths: tuple[str, ...]
    location: str


def _component_key(component: str) -> str:
    return str(component or "").strip().lower().replace("_", "-")


def _install_roots() -> tuple[Path, ...]:
    """Prefer the EXE folder, then persistent data for installed applications."""
    app_root = application_dir()
    persistent_root = data_dir()
    managed = persistent_root.resolve() != (app_root / "data").resolve()
    roots = (persistent_root,) if managed else (app_root, persistent_root)
    unique: list[Path] = []
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            resolved = root
        if resolved not in unique:
            unique.append(resolved)
    return tuple(unique)


def _relative_targets(component: str) -> dict[str, Path]:
    arch = "x64" if struct.calcsize("P") * 8 >= 64 else "x86"
    key = _component_key(component)
    if key == "ffmpeg":
        base = Path("tools") / "ffmpeg" / arch
        return {"ffmpeg.exe": base / "ffmpeg.exe", "ffprobe.exe": base / "ffprobe.exe"}
    if key == "node.js":
        return {"node.exe": Path("tools") / "node" / arch / "node.exe"}
    if key == "deno":
        return {"deno.exe": Path("tools") / "deno" / arch / "deno.exe"}
    if key == "yt-dlp":
        return {"yt-dlp.exe": Path("tools") / "yt-dlp" / arch / "yt-dlp.exe"}
    raise ToolInstallError(f"{component} 暂不支持自动安装，请打开项目发布页查看官方安装说明")


def _validate_windows_executable(path: Path, display_name: str) -> None:
    try:
        with path.open("rb") as stream:
            header = stream.read(2)
    except OSError as exc:
        raise ToolInstallError(f"无法读取 {display_name}：{exc}") from exc
    if header != b"MZ":
        raise ToolInstallError(f"{display_name} 不是有效的 Windows 可执行文件")


def _select_zip_members(archive: zipfile.ZipFile, required: dict[str, Path]) -> dict[str, zipfile.ZipInfo]:
    candidates: dict[str, list[zipfile.ZipInfo]] = {name: [] for name in required}
    for info in archive.infolist():
        if info.is_dir():
            continue
        normalized = PurePosixPath(info.filename.replace("\\", "/"))
        if normalized.is_absolute() or ".." in normalized.parts:
            continue
        basename = normalized.name.lower()
        if basename in candidates:
            # Reject unexpectedly huge entries before allocating or writing.
            if info.file_size <= 0 or info.file_size > 600 * 1024 * 1024:
                continue
            candidates[basename].append(info)

    selected: dict[str, zipfile.ZipInfo] = {}
    for basename, matches in candidates.items():
        if not matches:
            raise ToolInstallError(f"压缩包中缺少必需文件：{basename}")
        # Official portable archives put runtime binaries near their root/bin.
        # Choosing the shortest normalized path avoids debug/example copies.
        matches.sort(key=lambda info: (len(PurePosixPath(info.filename.replace("\\", "/")).parts), info.filename.lower()))
        selected[basename] = matches[0]
    return selected


def _stage_payloads(
    component: str,
    source: Path,
    staging_dir: Path,
    cancel_event: threading.Event | None,
) -> dict[str, Path]:
    required = _relative_targets(component)
    staged: dict[str, Path] = {}
    suffix = source.suffix.lower()
    if suffix == ".exe":
        if len(required) != 1:
            raise ToolInstallError(f"{component} 的发行资源必须包含完整运行时文件")
        basename = next(iter(required))
        target = staging_dir / basename
        shutil.copy2(source, target)
        _validate_windows_executable(target, basename)
        staged[basename] = target
        return staged
    if suffix != ".zip":
        raise ToolInstallError("当前仅支持自动安装官方 EXE 或 ZIP 便携资源")

    try:
        with zipfile.ZipFile(source) as archive:
            selected = _select_zip_members(archive, required)
            for basename, info in selected.items():
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("工具安装已取消")
                target = staging_dir / basename
                with archive.open(info) as source_stream, target.open("wb") as target_stream:
                    shutil.copyfileobj(source_stream, target_stream, length=1024 * 1024)
                _validate_windows_executable(target, basename)
                staged[basename] = target
    except zipfile.BadZipFile as exc:
        raise ToolInstallError("下载的便携资源不是有效 ZIP 压缩包") from exc
    return staged


def _install_payload_set(root: Path, required: dict[str, Path], staged: dict[str, Path]) -> list[str]:
    """Replace a component's files as one rollback-capable transaction."""
    prepared: list[tuple[Path, Path, Path]] = []
    moved_originals: list[tuple[Path, Path]] = []
    committed: list[Path] = []
    try:
        for basename, relative in required.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            incoming = target.with_name(target.name + ".new")
            backup = target.with_name(target.name + ".previous")
            incoming.unlink(missing_ok=True)
            backup.unlink(missing_ok=True)
            shutil.copy2(staged[basename], incoming)
            _validate_windows_executable(incoming, target.name)
            prepared.append((target, incoming, backup))

        for target, incoming, backup in prepared:
            if target.exists():
                os.replace(target, backup)
                moved_originals.append((target, backup))
            os.replace(incoming, target)
            committed.append(target)
    except Exception:
        for target in reversed(committed):
            target.unlink(missing_ok=True)
        for target, backup in reversed(moved_originals):
            if backup.exists():
                os.replace(backup, target)
        for _, incoming, backup in prepared:
            incoming.unlink(missing_ok=True)
            backup.unlink(missing_ok=True)
        raise
    for _, _, backup in prepared:
        backup.unlink(missing_ok=True)
    return [str(target) for target, _, _ in prepared]


def _validate_ejs_wheel(path: Path) -> str:
    if ".whl" not in {suffix.casefold() for suffix in path.suffixes}:
        raise ToolInstallError("yt-dlp-ejs 必须使用官方 Python wheel 发行资源")
    try:
        with zipfile.ZipFile(path) as archive:
            files = [info for info in archive.infolist() if not info.is_dir()]
            if not files or len(files) > 1000:
                raise ToolInstallError("yt-dlp-ejs wheel 文件数量异常")
            total_size = 0
            has_package = False
            for info in files:
                normalized = PurePosixPath(info.filename.replace("\\", "/"))
                if normalized.is_absolute() or ".." in normalized.parts:
                    raise ToolInstallError("yt-dlp-ejs wheel 包含不安全路径")
                if info.file_size < 0 or info.file_size > 20 * 1024 * 1024:
                    raise ToolInstallError("yt-dlp-ejs wheel 包含异常文件")
                total_size += info.file_size
                if info.filename.casefold() == "yt_dlp_ejs/__init__.py":
                    has_package = True
            if total_size > 50 * 1024 * 1024 or not has_package:
                raise ToolInstallError("yt-dlp-ejs wheel 缺少有效运行包")
    except zipfile.BadZipFile as exc:
        raise ToolInstallError("下载的 yt-dlp-ejs 不是有效 wheel") from exc
    version = wheel_distribution_version(path, "yt-dlp-ejs")
    if not version or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", version):
        raise ToolInstallError("无法从 yt-dlp-ejs wheel 读取版本号")
    return version


def _install_ejs_wheel(
    source: Path,
    cancel_event: threading.Event | None,
) -> ToolInstallResult:
    version = _validate_ejs_wheel(source)
    if not ytdlp_ejs_version_compatible(version):
        required = required_ytdlp_ejs_version()
        raise ToolInstallError(
            f"yt-dlp-ejs {version} 与当前内置 yt-dlp 不兼容，需要配套版本 {required}"
        )
    last_permission_error: Exception | None = None
    for root in _install_roots():
        target_dir = root / "tools" / "yt-dlp-ejs"
        safe_name = f"yt_dlp_ejs-{version}-py3-none-any.whl"
        target = target_dir / safe_name
        incoming = target.with_name(target.name + ".new")
        backup = target.with_name(target.name + ".previous")
        try:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("工具安装已取消")
            target_dir.mkdir(parents=True, exist_ok=True)
            incoming.unlink(missing_ok=True)
            backup.unlink(missing_ok=True)
            shutil.copy2(source, incoming)
            _validate_ejs_wheel(incoming)
            if target.exists():
                os.replace(target, backup)
            try:
                os.replace(incoming, target)
            except Exception:
                if backup.exists():
                    os.replace(backup, target)
                raise
            backup.unlink(missing_ok=True)
            for old_wheel in target_dir.glob("*.whl"):
                if old_wheel != target:
                    try:
                        old_wheel.unlink()
                    except OSError:
                        # A running Python process may still hold the previous
                        # wheel open. It is ignored on the next activation and
                        # can be removed by a later update.
                        pass
            try:
                source.unlink()
            except OSError:
                pass
            return ToolInstallResult(
                component="yt-dlp-ejs",
                paths=(str(target),),
                location=str(root),
            )
        except PermissionError as exc:
            incoming.unlink(missing_ok=True)
            last_permission_error = exc
            continue
        except OSError as exc:
            incoming.unlink(missing_ok=True)
            if getattr(exc, "winerror", None) in {5, 32}:
                last_permission_error = exc
                continue
            raise ToolInstallError(f"安装 yt-dlp-ejs 失败：{exc}") from exc
    detail = f"：{last_permission_error}" if last_permission_error else ""
    raise ToolInstallError(f"程序目录和数据目录均不可写，无法安装 yt-dlp-ejs{detail}")


def install_tool_component(
    component: str,
    source_path: str | Path,
    cancel_event: threading.Event | None = None,
) -> ToolInstallResult:
    """Install only required app-local runtime payloads from a trusted asset."""
    source = Path(source_path)
    if not source.is_file():
        raise ToolInstallError(f"下载资源不存在：{source}")
    component_key = _component_key(component)
    if component_key == "yt-dlp-ejs":
        return _install_ejs_wheel(source, cancel_event)
    required = _relative_targets(component)
    staging_dir = source.parent / (source.name + ".installing")
    shutil.rmtree(staging_dir, ignore_errors=True)
    staging_dir.mkdir(parents=True, exist_ok=True)
    try:
        staged = _stage_payloads(component, source, staging_dir, cancel_event)
        last_permission_error: Exception | None = None
        for root in _install_roots():
            try:
                if cancel_event is not None and cancel_event.is_set():
                    raise InterruptedError("工具安装已取消")
                installed = _install_payload_set(root, required, staged)
                try:
                    source.unlink()
                except OSError:
                    pass
                return ToolInstallResult(component=component, paths=tuple(installed), location=str(root))
            except PermissionError as exc:
                last_permission_error = exc
                continue
            except OSError as exc:
                if getattr(exc, "winerror", None) in {5, 32}:
                    last_permission_error = exc
                    continue
                raise ToolInstallError(f"安装 {component} 失败：{exc}") from exc
        detail = f"：{last_permission_error}" if last_permission_error else ""
        raise ToolInstallError(f"程序目录和数据目录均不可写，无法安装 {component}{detail}")
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
