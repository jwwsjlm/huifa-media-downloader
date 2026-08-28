from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import QObject, QTimer, Slot
from PySide6.QtWidgets import QMessageBox

from app.core.update_service import (
    normalize_ffmpeg_build_channel,
    select_release_asset,
)
from app.ui.i18n import format_text as ui_format
from app.ui.i18n import runtime_text, text as ui_text
from app.ui.runtime_component_presentation import (
    runtime_component_install_needed,
    runtime_result_component,
)


class RuntimeComponentUpdateController(QObject):
    """Coordinate remote component checks and app-local installations."""

    def __init__(
        self,
        page: Any,
    ) -> None:
        super().__init__(page)
        self.page = page
        self.results: dict[str, dict] = {}
        self.remote_checking = False
        self.remote_recheck_pending = False
        self.remote_error = ""
        self.installing_component = ""

    def result(self, component: str) -> dict:
        return self.results.get(runtime_result_component(component), {})

    def ffmpeg_result_matches_selected_channel(self, result: Mapping[str, Any]) -> bool:
        return normalize_ffmpeg_build_channel(
            result.get("ffmpeg_build_channel")
        ) == normalize_ffmpeg_build_channel(
            self.page.ffmpeg_build_channel.currentData()
        )

    def refresh(
        self,
        *,
        force_remote: bool = False,
        force_local: bool = False,
    ) -> None:
        page = self.page
        page.refresh_local_core_versions(force=force_local)
        service = page.window.update_service
        service.start_background_route_probe()
        if service.last_results and not force_remote:
            self.remote_versions_ready(service.last_results)
            return
        if force_remote and service.runtime_active("check"):
            self.remote_recheck_pending = True
            self.remote_error = ""
            self.remote_checking = True
            page._render_runtime_component_statuses()
            return
        if not service.runtime_active("check"):
            self.results = {}
        self.remote_error = ""
        self.remote_checking = True
        page._render_runtime_component_statuses()
        if not service.runtime_active("check"):
            service.check("")

    def ffmpeg_build_channel_changed(self, channel: str) -> None:
        normalized = normalize_ffmpeg_build_channel(channel)
        self.page.window.update_service.set_ffmpeg_build_channel(normalized)
        self.results.pop("FFmpeg", None)
        self.page._render_runtime_component_status("FFmpeg")
        self.page._render_runtime_component_status("FFprobe")
        self.refresh(force_remote=True)

    def _start_pending_recheck(self) -> None:
        if not self.remote_recheck_pending:
            return
        if self.page.window.update_service.runtime_active("check"):
            QTimer.singleShot(50, self, self._start_pending_recheck)
            return
        self.remote_recheck_pending = False
        self.refresh(force_remote=True)

    @Slot(object)
    def remote_version_ready(self, result: object) -> None:
        if not isinstance(result, dict):
            return
        name = str(result.get("name") or "")
        if not name:
            return
        if name == "FFmpeg" and not self.ffmpeg_result_matches_selected_channel(result):
            return
        self.results[name] = dict(result)
        self.page._render_runtime_component_status(name)
        if name == "FFmpeg":
            self.page._render_runtime_component_status("FFprobe")
        completed = len(self.results)
        available = sum(1 for row in self.results.values() if not row.get("error"))
        self.page.update_status.setText(ui_format(
            '{completed} component results returned, {available} available; remaining components and routes continue in the background.',
            completed=completed,
            available=available,
        ))

    @Slot(object)
    def remote_versions_ready(self, results: object) -> None:
        rows = results if isinstance(results, list) else []
        normalized: dict[str, dict] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "").strip()
            if not name:
                continue
            if name == "FFmpeg" and not self.ffmpeg_result_matches_selected_channel(row):
                continue
            normalized[name] = dict(row)
        self.results = normalized
        self.remote_checking = self.remote_recheck_pending
        self.remote_error = ""
        self.page._render_runtime_component_statuses()
        failures = sum(1 for row in self.results.values() if row.get("error"))
        self.page.update_status.setText(ui_format(
            'Runtime component check complete: {count} results, {failures} failed; route testing did not block these results.',
            count=len(self.results),
            failures=failures,
        ))
        self._start_pending_recheck()

    @Slot(str)
    def remote_versions_failed(self, error: str) -> None:
        self.remote_checking = self.remote_recheck_pending
        self.remote_error = str(
            error or ui_text('Check failed', context="runtime.remote_error")
        )
        self.page._render_runtime_component_statuses()
        self._start_pending_recheck()

    def request_update(self, component: str) -> None:
        page = self.page
        actual_component = runtime_result_component(component)
        result = self.result(component)
        if not result or result.get("error"):
            self.refresh(force_remote=True)
            return
        asset = select_release_asset(actual_component, result.get("assets") or [])
        if asset is None or not result.get("auto_install_supported"):
            page._render_runtime_component_status(component)
            return
        if not runtime_component_install_needed(result):
            return
        service = page.window.update_service
        if service.runtime_active("download", "install"):
            page.window.settings_status(ui_text(
                'A component update is already running…'
            ))
            return
        digest = str(asset.get("digest") or "")
        if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
            answer = QMessageBox.warning(
                page,
                ui_text('Publisher Checksum Unavailable'),
                ui_text(
                    'This official GitHub Release asset has no publisher SHA-256. The app will still validate its structure and Windows executable format.\n\nDownload and install it in the app-local tools folder?',
                ),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        self.installing_component = actual_component
        page._render_runtime_component_statuses()
        page.window.settings_status(ui_format(
            'Downloading and updating {component}…',
            component=actual_component,
        ))
        service.download_asset(asset, actual_component)

    @Slot(object)
    def component_installed(self, result: object) -> None:
        component = runtime_result_component(str(getattr(result, "component", "")))
        owned = self.installing_component == component
        self.installing_component = ""
        try:
            self.page._apply_installed_runtime_paths(result)
        except Exception as exc:
            self.page.refresh_local_core_versions(force=True)
            self.page._render_runtime_component_statuses()
            QMessageBox.warning(
                self.page,
                ui_text('Save Failed'),
                runtime_text(exc),
            )
            return
        self.page.refresh_local_core_versions(force=True)
        if owned:
            QTimer.singleShot(
                100,
                self,
                lambda: self.refresh(force_remote=True),
            )
            paths = "\n".join(getattr(result, "paths", ()) or ())
            QMessageBox.information(
                self.page,
                ui_text('Local Core Updated'),
                ui_format(
                    '{component} was updated in the app-local folder and is active now:\n{paths}',
                    component=getattr(result, "component", component),
                    paths=paths,
                ),
            )

    @Slot(str)
    def component_download_failed(self, error: str) -> None:
        if not self.installing_component:
            return
        self.installing_component = ""
        self.page._render_runtime_component_statuses()
        QMessageBox.warning(
            self.page,
            ui_text('Component Download Failed'),
            runtime_text(error),
        )

    @Slot(str)
    def component_install_failed(self, error: str) -> None:
        if not self.installing_component:
            return
        self.installing_component = ""
        self.page._render_runtime_component_statuses()
        QMessageBox.warning(
            self.page,
            ui_text('Component Installation Failed'),
            runtime_text(error),
        )
