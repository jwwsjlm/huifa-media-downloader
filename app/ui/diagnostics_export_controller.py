from __future__ import annotations

import platform
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QFileDialog, QMessageBox, QWidget

from app.core.update_service import (
    installed_component_details,
    runtime_component_presence,
)
from app.core.version import APP_NAME, APP_VERSION
from app.integrations.social_auto_upload import (
    core_status as publishing_core_status,
    resolve_chromium_executable,
    vendor_commit as publishing_core_commit,
    vendor_root as publishing_core_root,
)
from app.ui.i18n import format_text as ui_format
from app.ui.i18n import runtime_text, text as ui_text


def _probe_component(
    probe: Callable[..., tuple[object, object, object]],
    *args: object,
) -> tuple[str, dict[str, str]]:
    """Return one diagnostic row without letting a broken probe abort export."""

    try:
        version, source, runtime_path = probe(*args)
    except Exception as exc:
        return "unavailable", {
            "source": "probe failed",
            "path": "",
            "error_type": type(exc).__name__,
        }
    return str(version or ""), {
        "source": str(source or ""),
        "path": str(runtime_path or ""),
    }


def _publishing_runtime_summary() -> tuple[str, dict[str, str], str]:
    try:
        available, detail = publishing_core_status()
    except Exception as exc:
        available = False
        detail = "probe failed"
        status_error = type(exc).__name__
    else:
        status_error = ""

    try:
        root = str(publishing_core_root())
    except Exception as exc:
        root = ""
        root_error = type(exc).__name__
    else:
        root_error = ""

    if available:
        try:
            version = str(publishing_core_commit() or "")
        except Exception as exc:
            version = "unavailable"
            version_error = type(exc).__name__
        else:
            version_error = ""
    else:
        version = "unavailable"
        version_error = ""

    runtime = {
        "source": str(detail or ""),
        "path": root,
    }
    if status_error:
        runtime["error_type"] = status_error
    if root_error:
        runtime["path_error_type"] = root_error
    if version_error:
        runtime["version_error_type"] = version_error

    try:
        chromium = str(resolve_chromium_executable() or "")
    except Exception as exc:
        chromium = f"probe failed ({type(exc).__name__})"
    return version, runtime, chromium


def collect_diagnostic_summary(
    app_settings: Any,
    database: Callable[[], Any],
) -> dict[str, Any]:
    """Collect the support manifest while isolating optional runtime failures."""

    settings = app_settings
    yt_dlp, yt_dlp_runtime = _probe_component(
        installed_component_details,
        "yt-dlp",
    )
    ffmpeg, ffmpeg_runtime = _probe_component(
        runtime_component_presence,
        "FFmpeg",
        settings.get("ffmpeg_path"),
    )
    ffprobe, ffprobe_runtime = _probe_component(
        runtime_component_presence,
        "FFprobe",
        settings.get("ffmpeg_path"),
        settings.get("ffprobe_path"),
    )
    deno, deno_runtime = _probe_component(
        runtime_component_presence,
        "Deno",
        settings.get("deno_path"),
    )
    ejs, ejs_runtime = _probe_component(
        runtime_component_presence,
        "yt-dlp-ejs",
    )
    publishing, publishing_runtime, publishing_chromium = (
        _publishing_runtime_summary()
    )
    # Resolve this lazily because the main window can replace a deleted or
    # recovered SQLite connection while it remains open.
    current_database = database()

    return {
        "application": APP_NAME,
        "application_version": APP_VERSION,
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "yt_dlp": yt_dlp,
        "yt_dlp_runtime": yt_dlp_runtime,
        "ffmpeg": ffmpeg,
        "ffmpeg_runtime": ffmpeg_runtime,
        "deno": deno,
        "deno_runtime": deno_runtime,
        "yt_dlp_ejs": ejs,
        "yt_dlp_ejs_runtime": ejs_runtime,
        "ffprobe": ffprobe,
        "ffprobe_runtime": ffprobe_runtime,
        "social_auto_upload": publishing,
        "social_auto_upload_runtime": publishing_runtime,
        "publishing_chromium": publishing_chromium,
        "download_dir": settings.get("download_dir"),
        "proxy_configured": bool(settings.get("proxy")),
        "ffmpeg_path_configured": bool(settings.get("ffmpeg_path")),
        "ffprobe_path_configured": bool(settings.get("ffprobe_path")),
        "deno_path_configured": bool(settings.get("deno_path")),
        "ytdlp_ejs_source": settings.get("ytdlp_ejs_source"),
        "database": {
            "path": str(current_database.path),
            "recovery": current_database.recovery_report.as_dict(),
            "last_backup_path": current_database.last_backup_path,
            "last_backup_error": current_database.last_backup_error,
        },
    }


def normalized_diagnostics_target(target: str | Path) -> Path:
    path = Path(target)
    if path.suffix.casefold() != ".zip":
        path = path.with_name(path.name + ".zip")
    return path


def export_diagnostics(
    parent: QWidget,
    logs: Any,
    app_settings: Any,
    database: Callable[[], Any],
) -> None:
    default_name = str(logs.root.parent / "diagnostics.zip")
    target, _selected_filter = QFileDialog.getSaveFileName(
        parent,
        ui_text("Export Diagnostics"),
        default_name,
        ui_text("ZIP Archives (*.zip)"),
    )
    if not target:
        return

    try:
        summary = collect_diagnostic_summary(app_settings, database)
        bundle = logs.export_bundle(normalized_diagnostics_target(target), summary)
    except Exception as exc:
        QMessageBox.warning(
            parent,
            ui_text("Export Failed"),
            ui_format(
                "Unable to export diagnostics:\n{error}",
                error=runtime_text(exc),
            ),
        )
        return

    QMessageBox.information(
        parent,
        ui_text("Export Complete"),
        ui_format("Diagnostic bundle saved to:\n{path}", path=bundle),
    )
