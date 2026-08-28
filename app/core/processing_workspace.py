from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path

from app.core.disk_capacity import (
    CapacityEstimate,
    resolve_volume_identity,
)
from app.core.filename_rules import stable_ascii_component
from app.core.paths import resolve_portable_path


PROCESSING_TEMP_APP_DIR = "huifa-processing"


def processing_temp_workspace_path(
    root: str | Path,
    task_id: str,
    purpose: str = "download",
) -> Path | None:
    raw_root = str(root or "").strip()
    if not raw_root:
        return None
    safe_task_id = stable_ascii_component(
        task_id,
        fallback="task",
        digest_threshold=80,
        max_length=80,
    )
    safe_purpose = stable_ascii_component(
        purpose,
        fallback="download",
        digest_threshold=40,
        max_length=40,
    )
    return (
        resolve_portable_path(raw_root)
        / PROCESSING_TEMP_APP_DIR
        / safe_task_id
        / safe_purpose
    )


def processing_temp_workspace(
    root: str | Path,
    task_id: str,
    purpose: str = "download",
) -> Path | None:
    """Create one app-owned work directory below a user-selected temp root."""

    workspace = processing_temp_workspace_path(root, task_id, purpose)
    if workspace is None:
        return None
    workspace.mkdir(parents=True, exist_ok=True)
    if not workspace.is_dir():
        raise NotADirectoryError(f"临时处理路径不是目录：{workspace}")
    return workspace


def is_reparse_point(path: Path) -> bool:
    try:
        value = path.lstat()
    except OSError:
        return False
    attributes = int(getattr(value, "st_file_attributes", 0) or 0)
    reparse_flag = int(
        getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400) or 0x400
    )
    return path.is_symlink() or bool(attributes & reparse_flag)


def _remove_tree_without_following_links(
    path: Path,
    reparse_check: Callable[[Path], bool],
) -> None:
    """Delete a real directory tree while unlinking, never traversing, links."""

    with os.scandir(path) as iterator:
        entries = list(iterator)
    for entry in entries:
        child = Path(entry.path)
        if entry.is_symlink() or reparse_check(child):
            if child.is_symlink():
                child.unlink()
            else:
                attributes = int(
                    getattr(child.lstat(), "st_file_attributes", 0) or 0
                )
                directory_flag = int(
                    getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0x10) or 0x10
                )
                if attributes & directory_flag:
                    os.rmdir(child)
                else:
                    child.unlink()
            continue
        if entry.is_dir(follow_symlinks=False):
            _remove_tree_without_following_links(child, reparse_check)
        else:
            child.unlink(missing_ok=True)
    path.rmdir()


def cleanup_processing_workspace(
    workspace: Path | None,
    *,
    reparse_check: Callable[[Path], bool] = is_reparse_point,
) -> bool:
    """Remove only an exact task workspace created by this application."""

    if workspace is None:
        return True
    path = Path(workspace).expanduser().absolute()
    app_root = next(
        (
            candidate
            for candidate in (path, *path.parents)
            if candidate.name.casefold() == PROCESSING_TEMP_APP_DIR.casefold()
        ),
        None,
    )
    if app_root is None or path == app_root:
        return False
    try:
        relative_parts = path.relative_to(app_root).parts
    except ValueError:
        return False
    if len(relative_parts) != 2:
        return False
    current = app_root
    for part in ("", *relative_parts):
        if part:
            current = current / part
        if os.path.lexists(current) and reparse_check(current):
            return False
    if os.path.lexists(path):
        try:
            _remove_tree_without_following_links(path, reparse_check)
        except OSError:
            return False
    parent = path.parent
    while parent != app_root:
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent
    return True


def same_storage_volume(first: str | Path, second: str | Path) -> bool:
    try:
        return (
            resolve_volume_identity(first).key
            == resolve_volume_identity(second).key
        )
    except Exception:
        first_drive = os.path.splitdrive(str(Path(first).resolve()))[0].casefold()
        second_drive = os.path.splitdrive(str(Path(second).resolve()))[0].casefold()
        return bool(first_drive and first_drive == second_drive)


def final_output_capacity_estimate(
    estimate: CapacityEstimate,
) -> CapacityEstimate:
    """Reserve only final artifact bytes on a separate destination disk."""

    if not estimate.known:
        return CapacityEstimate.unknown(
            margin_bytes=estimate.margin_bytes,
            entry_count=estimate.entry_count,
        )
    return CapacityEstimate(
        known=True,
        download_bytes=0,
        final_bytes=estimate.final_bytes,
        peak_bytes=estimate.final_bytes,
        margin_bytes=estimate.margin_bytes,
        entry_count=estimate.entry_count,
        merge_entry_count=0,
        sources=tuple(estimate.sources) + ("final-destination",),
    )
