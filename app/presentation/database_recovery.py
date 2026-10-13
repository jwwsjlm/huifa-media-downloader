from __future__ import annotations

from typing import Any

from app.presentation.i18n import format_text as ui_format
from app.presentation.i18n import text as ui_text


def database_recovery_notice(database: Any) -> tuple[str, str, str] | None:
    """Describe a durable SQLite recovery outcome for either Qt frontend."""

    report = database.recovery_report
    if not report.requires_notice:
        return None

    if report.status == "restored":
        title = ui_text("Database Restored Automatically")
        summary = ui_text(
            "The local database was damaged and has been restored from the most recent healthy backup.",
        )
        consequence = ui_text(
            "A small number of task states created after the last backup may need to be checked again.",
        )
        location = report.quarantine_dir or str(database.path.parent / "recovery")
        detail = ui_format(
            "{summary}\n\n{consequence}\n\nThe original files were quarantined at:\n{location}",
            summary=summary,
            consequence=consequence,
            location=location,
        )
    elif report.status == "schema_reset":
        title = ui_text("Database Schema Updated")
        summary = ui_text(
            "The database schema was not the current development version and has been recreated with the latest schema.",
        )
        consequence = ui_text(
            "No automatic migration was performed; the original database was quarantined for recovery or support.",
        )
        location = report.quarantine_dir or str(database.path.parent / "recovery")
        detail = ui_format(
            "{summary}\n\n{consequence}\n\nThe original files were quarantined at:\n{location}",
            summary=summary,
            consequence=consequence,
            location=location,
        )
    else:
        title = ui_text("Database Rebuilt Safely")
        summary = ui_text(
            "The local database was damaged and no usable backup was found, so a new empty database was created.",
        )
        consequence = ui_text(
            "The original database was not deleted and can be provided to support for further analysis.",
        )
        location = report.quarantine_dir or str(database.path.parent / "recovery")
        detail = ui_format(
            "{summary}\n\n{consequence}\n\nThe original files were quarantined at:\n{location}",
            summary=summary,
            consequence=consequence,
            location=location,
        )
    return title, summary, detail
