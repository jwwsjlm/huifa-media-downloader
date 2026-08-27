from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


class PortableDataDirectoryError(RuntimeError):
    """Raised when the app-local data directory cannot be used safely."""

    def __init__(self, path: Path, cause: BaseException):
        self.path = Path(path)
        self.cause = cause
        super().__init__(
            f"程序数据目录不可写：{self.path}。请将完整软件目录移动到可写位置后重试。"
        )


_WRITABLE_DATA_DIRECTORIES: set[str] = set()


def application_dir() -> Path:
    """Return the portable application directory for source and packaged runs."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _portable_data_path() -> Path:
    """Return the data directory defined by the current deployment layout."""
    try:
        from app.core.application_updater import velopack_persistent_data_dir

        managed_path = velopack_persistent_data_dir(application_dir())
    except Exception:
        managed_path = None
    return managed_path or (application_dir() / "data")


def _ensure_writable_directory(path: Path) -> Path:
    """Create and verify one required application-owned directory."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise NotADirectoryError(f"路径不是目录：{path}")
        cache_key = os.path.normcase(str(path.resolve()))
        if cache_key not in _WRITABLE_DATA_DIRECTORIES:
            handle, probe_name = tempfile.mkstemp(prefix=".huifa-write-test-", dir=path)
            os.close(handle)
            Path(probe_name).unlink()
            _WRITABLE_DATA_DIRECTORIES.add(cache_key)
    except PortableDataDirectoryError:
        raise
    except OSError as exc:
        raise PortableDataDirectoryError(path, exc) from exc
    return path


def data_dir() -> Path:
    return _ensure_writable_directory(_portable_data_path())


def downloads_dir() -> Path:
    return _ensure_writable_directory(data_dir() / "downloads")


def resolve_portable_path(
    value: str | Path,
    application_root: str | Path | None = None,
) -> Path:
    """Resolve a saved relative path against the application directory."""
    root = Path(application_root) if application_root is not None else application_dir()
    path = Path(str(value or "").strip()).expanduser()
    if not path.is_absolute():
        path = root / path
    try:
        return path.resolve()
    except OSError:
        return path


def portable_path_value(
    value: str | Path,
    *,
    application_root: str | Path | None = None,
    persistent_root: str | Path | None = None,
) -> str:
    """Store app-owned paths relatively while preserving explicit externals.

    A directory-portable build can therefore be moved to another drive or PC
    without leaving stale drive letters in ``settings.ini``.  Paths selected
    outside the application/data trees remain absolute because they are an
    explicit user choice.
    """
    raw = str(value or "").strip()
    if not raw:
        return ""
    # Plain command names (``sau``, ``ffmpeg``) are PATH lookups, not paths.
    if not Path(raw).is_absolute() and not any(char in raw for char in ("/", "\\")):
        return raw
    app_root = Path(application_root) if application_root is not None else application_dir()
    data_root = Path(persistent_root) if persistent_root is not None else data_dir()
    resolved = resolve_portable_path(raw, app_root)
    owned = False
    for root in (app_root, data_root):
        try:
            resolved.relative_to(root.resolve())
            owned = True
            break
        except (OSError, ValueError):
            continue
    if not owned:
        return str(resolved)
    try:
        relative = os.path.relpath(resolved, app_root.resolve())
    except ValueError:
        # Windows cannot express a relative path across drive letters. This
        # only occurs for an explicitly external location (or a mocked test
        # root), so retaining the absolute value is the correct fallback.
        return str(resolved)
    return Path(relative).as_posix()


def tool_runtime_roots(
    application_root: str | Path | None = None,
    persistent_root: str | Path | None = None,
) -> list[Path]:
    """Return external-tool search roots in deployment-aware priority order.

    Portable/source builds prefer files beside the executable as requested by
    users. Velopack replaces its ``current`` directory during upgrades, so a
    managed installation must prefer its persistent data directory instead.
    The PyInstaller extraction directory remains a later bundled fallback.
    """
    app_root = Path(application_root) if application_root is not None else application_dir()
    data_root = Path(persistent_root) if persistent_root is not None else data_dir()
    managed = data_root.resolve() != (app_root / "data").resolve()
    roots = [data_root, app_root] if managed else [app_root, data_root]
    extracted = getattr(sys, "_MEIPASS", "")
    if extracted:
        roots.append(Path(extracted))
    roots.append(Path(__file__).resolve().parents[2])
    unique: list[Path] = []
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            resolved = root
        if resolved not in unique:
            unique.append(resolved)
    return unique


def initialize_data_layout() -> Path:
    """Create the storage layout used by the current application version."""
    target = data_dir()
    for name in ("browser", "downloads", "languages", "logs", "temp"):
        _ensure_writable_directory(target / name)
    return target
