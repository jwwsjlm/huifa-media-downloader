from __future__ import annotations

import re

from PySide6.QtCore import QTimer, Qt, QUrl
from PySide6.QtGui import QBrush, QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from app.core.github_mirrors import ROUTE_DIRECT
from app.core.update_service import (
    UpdateService,
    component_visible_in_update_list,
    installed_component_details,
    select_release_asset,
)
from app.ui.i18n import format_text as ui_format, runtime_text, text as ui_text
from app.ui.github_route_presentation import github_route_display_name
from app.ui.runtime_component_presentation import runtime_component_install_needed


_COMPONENT_STATE_TEXT = {
    "未检测": "Not detected",
    "未安装": "Not installed",
    "检测失败": "Detection failed",
    "未知": "Unknown",
}
_MISSING_COMPONENT_STATES = frozenset({
    "未检测",
    "未安装",
    "not detected",
    "not installed",
})


def component_state_text(value: object, *, fallback: str) -> str:
    """Localize app-owned state sentinels while preserving tool output."""

    raw = str(value or "").strip()
    if not raw:
        return ui_text(fallback)
    key = _COMPONENT_STATE_TEXT.get(raw)
    return ui_text(key) if key else raw


def component_is_missing(value: object) -> bool:
    return str(value or "").strip().casefold() in _MISSING_COMPONENT_STATES


class UpdateDialog(QDialog):
    def __init__(self, results: list[dict], update_service: UpdateService, parent=None):
        super().__init__(parent)
        self._initialize_state(results, update_service)
        self._configure_dialog()
        layout = QVBoxLayout(self)
        self.tree = self._build_component_tree()
        layout.addWidget(self.tree, 1)
        self.detail = self._build_detail_label()
        layout.addWidget(self.detail)
        layout.addLayout(self._build_button_row())
        self._connect_update_signals()
        self._select_initial_result()

    def _initialize_state(
        self,
        results: list[dict],
        update_service: UpdateService,
    ) -> None:
        self.update_service = update_service
        self.results = [
            result
            for result in results
            if component_visible_in_update_list(str(result.get("name") or ""))
        ]
        self._bulk_queue: list[tuple[dict, dict]] = []
        self._bulk_total = 0
        self._bulk_completed: list[str] = []
        self._bulk_errors: list[str] = []
        self._bulk_active_component = ""
        self._closed = False
        self._signals_connected = False

    def _configure_dialog(self) -> None:
        self.setWindowTitle(ui_text('Check Runtime Components'))
        self.resize(1020, 440)

    def _build_component_tree(self) -> QTreeWidget:
        tree = QTreeWidget()
        tree.setHeaderLabels([
            ui_text('Component'), ui_text('Current'),
            ui_text('Source'), ui_text('Runtime Path'),
            ui_text('Latest'), ui_text('Metadata Route'),
            ui_text('Status'),
        ])
        tree.setRootIsDecorated(False)
        tree.setAlternatingRowColors(True)
        tree.setUniformRowHeights(True)
        tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._configure_tree_header(tree.header())
        for result in self.results:
            tree.addTopLevelItem(self._component_item(result))
        tree.itemSelectionChanged.connect(self.update_detail)
        return tree

    @staticmethod
    def _configure_tree_header(header: QHeaderView) -> None:
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeToContents)

    def _component_item(self, result: dict) -> QTreeWidgetItem:
        component = str(result.get("name") or "")
        raw_current = str(result.get("current") or "未检测")
        current = component_state_text(raw_current, fallback="Not detected")
        source = str(result.get("source") or "—")
        runtime_path = str(result.get("runtime_path") or "—")
        latest = component_state_text(result.get("latest"), fallback="Unknown")
        metadata_route = github_route_display_name(
            str(result.get("metadata_route") or ROUTE_DIRECT),
            str(result.get("metadata_route_name") or "GitHub 直连"),
        )
        if result.get("metadata_cached"):
            metadata_route += ui_text(' · Cached')
        status_display, status_color = self._component_status(result, raw_current)
        item = QTreeWidgetItem([
            component,
            current,
            source,
            runtime_path,
            latest,
            metadata_route,
            status_display,
        ])
        item.setData(0, Qt.UserRole, result)
        self._apply_component_tooltips(
            item,
            result,
            component=component,
            current=current,
            source=source,
            runtime_path=runtime_path,
            status=status_display,
        )
        if result.get("metadata_third_party") or result.get("metadata_cached"):
            item.setForeground(5, QBrush(QColor("#b26a00")))
        item.setForeground(6, QBrush(QColor(status_color)))
        return item

    @staticmethod
    def _component_status(result: dict, current: str) -> tuple[str, str]:
        managed = bool(result.get("managed_by_application"))
        missing = component_is_missing(current)
        if managed and missing:
            status, color = "Embedded download core not loaded", "#c2413a"
        elif managed and result.get("upstream_update_available"):
            status, color = "Upstream update available (update the app)", "#2f7bdc"
        elif managed:
            status = (
                "Bundled (updated with the app)"
                if not result.get("error")
                else "Available (online check failed)"
            )
            color = "#138a4b" if not result.get("error") else "#b26a00"
        elif result.get("error"):
            status, color = "", "#c2413a"
        elif result.get("install_available"):
            status, color = "Not installed; can install automatically", "#b26a00"
        elif result.get("channel_switch_required"):
            status, color = "FFmpeg build switch required", "#2f7bdc"
        elif result.get("has_update"):
            status = (
                "Update available; download it"
                if result.get("auto_install_supported") and result.get("assets")
                else "Update available; open the release page"
            )
            color = "#2f7bdc"
        else:
            status = "Up to date" if not missing else "Not installed; no automatic installer available"
            color = "#138a4b" if not missing else "#6f7b8c"
        if result.get("error"):
            status = ui_format(
                'Check failed: {error}',
                error=runtime_text(result.get("error")),
            )
        else:
            status = ui_text(status)
        return status, color

    def _apply_component_tooltips(
        self,
        item: QTreeWidgetItem,
        result: dict,
        *,
        component: str,
        current: str,
        source: str,
        runtime_path: str,
        status: str,
    ) -> None:
        item.setToolTip(1, ui_format(
            '{component} current available version: {current}',
            component=component,
            current=current,
        ))
        item.setToolTip(2, self._source_tooltip(component, source))
        item.setToolTip(
            3,
            ui_format('Current runtime path: {path}', path=runtime_path)
            if runtime_path != "—"
            else ui_text('No usable runtime file was found'),
        )
        metadata_warning = str(result.get("metadata_warning") or "")
        item.setToolTip(5, metadata_warning or ui_text(
            'Metadata was fetched directly from GitHub.',
        ))
        item.setToolTip(6, status)

    @staticmethod
    def _build_detail_label() -> QLabel:
        detail = QLabel(ui_text(
            'The app folder is checked first, followed by bundled components and the system PATH.',
        ))
        detail.setObjectName("mutedText")
        detail.setWordWrap(True)
        return detail

    def _build_button_row(self) -> QHBoxLayout:
        buttons = QHBoxLayout()
        self.download_button = QPushButton(ui_text('Download and Install'))
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(self.download_selected)
        self.install_all_button = QPushButton(ui_text('Install/Update All Local Cores'))
        self.install_all_button.setToolTip(ui_text(
            "Install yt-dlp, FFmpeg/FFprobe, Deno and yt-dlp-ejs sequentially into the app's own tools folder.",
        ))
        self.install_all_button.clicked.connect(self.install_all_local_cores)
        self.open_release_button = QPushButton(ui_text('Open Release Page'))
        self.open_release_button.setEnabled(False)
        self.open_release_button.clicked.connect(self.open_release)
        close = QPushButton(ui_text('Close'))
        close.clicked.connect(self.accept)
        buttons.addWidget(self.download_button)
        buttons.addWidget(self.install_all_button)
        buttons.addWidget(self.open_release_button)
        buttons.addStretch(1)
        buttons.addWidget(close)
        return buttons

    def _connect_update_signals(self) -> None:
        self.update_service.download_finished.connect(self.download_finished)
        self.update_service.download_failed.connect(self.download_failed)
        self.update_service.install_finished.connect(self.install_finished)
        self.update_service.install_failed.connect(self.install_failed)
        self._signals_connected = True

    def _select_initial_result(self) -> None:
        if self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
            self.update_detail()
        self._refresh_install_all_button()

    @staticmethod
    def _source_tooltip(component: str, source: str) -> str:
        if source and source != "—":
            return ui_format(
                'Source reported for {component}: {source}',
                component=component,
                source=source,
            )
        return ui_text('No usable source was detected.')

    def closeEvent(self, event) -> None:
        # The service outlives this modal dialog. Disconnect its signals so
        # reopening the dialog does not retain stale dialogs.
        self._deactivate()
        self._disconnect_update_signals()
        super().closeEvent(event)

    def done(self, result: int) -> None:
        # QDialog.accept()/reject() can hide the dialog without delivering the
        # same close-event path on every Qt platform.
        self._deactivate()
        self._disconnect_update_signals()
        super().done(result)

    def _deactivate(self) -> None:
        """Stop dialog-owned orchestration while leaving service work intact."""

        self._closed = True
        self._bulk_queue.clear()
        self._bulk_total = 0
        self._bulk_active_component = ""

    def _disconnect_update_signals(self) -> None:
        if not self._signals_connected:
            return
        connections = (
            (self.update_service.download_finished, self.download_finished),
            (self.update_service.download_failed, self.download_failed),
            (self.update_service.install_finished, self.install_finished),
            (self.update_service.install_failed, self.install_failed),
        )
        for signal, callback in connections:
            try:
                signal.disconnect(callback)
            except (RuntimeError, TypeError):
                pass
        self._signals_connected = False

    def selected_result(self) -> dict | None:
        item = self.tree.currentItem()
        return item.data(0, Qt.UserRole) if item else None

    def _bulk_install_candidates(self) -> list[tuple[dict, dict]]:
        order = {
            "yt-dlp": 0,
            "ffmpeg": 1,
            "deno": 2,
            "yt-dlp-ejs": 3,
        }
        candidates: list[tuple[dict, dict]] = []
        for result in self.results:
            component = str(result.get("name") or "")
            if (
                component.casefold() not in order
                or not result.get("auto_install_supported")
                or not runtime_component_install_needed(result)
            ):
                continue
            asset = select_release_asset(component, result.get("assets") or [])
            if asset is not None:
                candidates.append((result, asset))
        candidates.sort(key=lambda item: order[str(item[0].get("name") or "").casefold()])
        return candidates

    def _refresh_install_all_button(self) -> None:
        busy = bool(self._bulk_queue) or self.update_service.runtime_active(
            "download",
            "install",
        )
        self.install_all_button.setEnabled(bool(self._bulk_install_candidates()) and not busy)

    def _select_component_item(self, component: str) -> None:
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            payload = item.data(0, Qt.UserRole) or {}
            if str(payload.get("name") or "").casefold() == component.casefold():
                self.tree.setCurrentItem(item)
                return

    def install_all_local_cores(self) -> None:
        if self._closed:
            return
        candidates = self._bulk_install_candidates()
        if not candidates:
            self._refresh_install_all_button()
            return
        unsigned = [
            str(result.get("name") or "")
            for result, asset in candidates
            if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", str(asset.get("digest") or ""))
        ]
        if unsigned:
            answer = QMessageBox.warning(
                self,
                ui_text('Some components have no publisher checksum'),
                ui_format(
                    'The following official GitHub Release assets do not provide publisher SHA-256 values:\n{components}\n\nThe app will still validate file structure and executable format. Continue?',
                    components=ui_text(', ', context="list.separator").join(unsigned),
                ),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        self._bulk_queue = list(candidates)
        self._bulk_total = len(candidates)
        self._bulk_completed = []
        self._bulk_errors = []
        self._bulk_active_component = ""
        self.download_button.setEnabled(False)
        self.install_all_button.setEnabled(False)
        self._start_next_bulk_install()

    def _start_next_bulk_install(self) -> None:
        if self._closed:
            return
        if self.update_service.runtime_active("download", "install"):
            QTimer.singleShot(100, self._start_next_bulk_install)
            return
        if not self._bulk_queue:
            total = self._bulk_total
            completed = len(self._bulk_completed)
            errors = "\n".join(self._bulk_errors)
            self.detail.setText(ui_format(
                'Local core setup finished: {completed}/{total} succeeded{errors}',
                completed=completed,
                total=total,
                errors=f"\n{errors}" if errors else "",
            ))
            QMessageBox.information(
                self,
                ui_text('Local Core Setup Finished'),
                self.detail.text(),
            )
            self._bulk_total = 0
            self._bulk_active_component = ""
            self._refresh_install_all_button()
            self.update_detail()
            return
        result, asset = self._bulk_queue.pop(0)
        component = str(result.get("name") or "")
        self._bulk_active_component = component
        self._select_component_item(component)
        current = len(self._bulk_completed) + len(self._bulk_errors) + 1
        self.detail.setText(ui_format(
            'Installing local core {current}/{total}: {component}',
            current=current,
            total=self._bulk_total,
            component=component,
        ))
        self.update_service.download_asset(asset, component)

    def update_detail(self) -> None:
        self.download_button.setText(ui_text('Download and Install'))
        result = self.selected_result()
        if not result:
            self.download_button.setEnabled(False)
            self.open_release_button.setEnabled(False)
            return
        self.open_release_button.setEnabled(bool(result.get("url")))
        if result.get("managed_by_application"):
            self._update_managed_component_detail(result)
            return
        self._update_external_component_detail(result)

    def _update_managed_component_detail(self, result: dict) -> None:
        host = self.parent()
        self.download_button.setText(ui_text('Check App Update'))
        self.download_button.setEnabled(
            callable(getattr(host, "check_application_update", None))
        )
        raw_current = str(result.get("current") or "未检测")
        current = component_state_text(raw_current, fallback="Not detected")
        source = str(result.get("source") or ui_text('No usable source was detected.'))
        latest = component_state_text(result.get("latest"), fallback="Unknown")
        if component_is_missing(raw_current):
            text = ui_text(
                'No usable download core was detected. Install the official yt-dlp.exe or download the complete app with its bundled fallback module.',
            )
        elif result.get("upstream_update_available"):
            text = ui_format(
                'Current download core: yt-dlp {current}; latest upstream: {latest}. The official standalone yt-dlp.exe can be installed from Runtime Components.',
                current=current,
                latest=latest,
            )
        else:
            text = ui_format(
                'Download core: yt-dlp {current} · {source}. It is bundled in HuifaVideoDownloader.exe and updates with the app; PySide6 or yt-dlp do not need to be installed separately.',
                current=current,
                source=source,
            )
        self.detail.setText(text)

    def _update_external_component_detail(self, result: dict) -> None:
        assets = result.get("assets") or []
        asset = select_release_asset(str(result.get("name") or ""), assets)
        if result.get("channel_switch_required"):
            self.download_button.setText(ui_text('Switch and Install'))
        if not result.get("auto_install_supported"):
            self.download_button.setEnabled(False)
            if result.get("has_update"):
                self.detail.setText(
                    ui_text('A newer version is available, but this app does not install it. Open the official release page and follow the project instructions.')
                )
            else:
                self.detail.setText(ui_text('This component is maintained externally and cannot be installed automatically. Open the official release page for instructions.'))
            return
        if not runtime_component_install_needed(result):
            self.download_button.setEnabled(False)
            self.detail.setText(ui_text('The local version is up to date.'))
            return
        can_download = (
            asset is not None
            and not self._bulk_total
            and not self.update_service.runtime_active("download", "install")
        )
        self.download_button.setEnabled(can_download)
        if asset is not None:
            self.detail.setText(self._download_asset_detail(result, asset))
        elif assets:
            self.detail.setText(ui_text('This release has assets, but none are trusted for the current Windows architecture. Open the release page for details.'))
        else:
            self.detail.setText(ui_text('This release has no downloadable GitHub asset. Open the release page for installation instructions.'))

    @staticmethod
    def _download_asset_detail(result: dict, asset: dict) -> str:
        try:
            size = int(asset.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        size_text = f" · {size / (1024 * 1024):.1f} MB" if size else ""
        digest = str(asset.get("digest") or "")
        verify_text = (
            ui_text(' · SHA-256 verified after download')
            if re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest)
            else ui_text(' · publisher did not provide SHA-256')
        )
        metadata_note = ""
        if result.get("metadata_third_party"):
            metadata_note = ui_text(
                '\nNote: Version and checksum metadata came through a third-party CDN/proxy and may lag upstream. SHA-256 checks download integrity, but source trust is lower than direct GitHub access.',
            )
        elif result.get("metadata_cached"):
            metadata_note = ui_text(
                '\nNote: Local cached metadata is in use, so version information may not be real-time latest.',
            )
        component = str(result.get("name") or "")
        install_text = (
            ui_text(' · installed in the app-local tools folder for independent updates')
            if component.lower() in {"yt-dlp", "yt-dlp-ejs", "ffmpeg", "deno"}
            else ""
        )
        return (
            ui_text('Will download: ')
            + f"{asset.get('name', '')}{size_text}{verify_text}{install_text}{metadata_note}"
        )

    def download_selected(self) -> None:
        if (
            self._bulk_total
            or self.update_service.runtime_active("download", "install")
        ):
            self.update_detail()
            return
        result = self.selected_result()
        if not result:
            return
        if result.get("managed_by_application"):
            callback = getattr(self.parent(), "check_application_update", None)
            if callable(callback):
                self.accept()
                callback()
            else:
                self.update_detail()
            return
        if not result.get("auto_install_supported"):
            self.update_detail()
            return
        if not runtime_component_install_needed(result):
            self.update_detail()
            return
        assets = result.get("assets") or []
        asset = select_release_asset(str(result.get("name") or ""), assets)
        if asset is None:
            self.update_detail()
            return
        digest = str(asset.get("digest") or "")
        if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
            answer = QMessageBox.warning(
                self,
                ui_text('Publisher Checksum Unavailable'),
                ui_text(
                    "This GitHub Release asset has no publisher-provided SHA-256. The app will still validate download integrity, file structure, and executable format where applicable.\n\nDownload and install it from the project's official GitHub Release?",
                ),
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        component = str(result.get("name") or "")
        self.detail.setText(ui_format(
            'Downloading and preparing to install: {name}', name=asset.get("name", "")
        ))
        self.download_button.setEnabled(False)
        self.update_service.download_asset(asset, component)

    def open_release(self) -> None:
        result = self.selected_result()
        if result and result.get("url"):
            try:
                release_url = str(result["url"])
                route_id = str(result.get("metadata_route") or ROUTE_DIRECT)
                route = next(
                    (candidate for candidate in self.update_service.available_download_routes() if candidate.id == route_id),
                    None,
                )
                if route is not None and route.third_party:
                    if route.release_page_supported:
                        release_url = self.update_service.route_url(
                            route, release_url, "asset"
                        )

                if not QDesktopServices.openUrl(QUrl(release_url)):
                    raise RuntimeError(ui_text('The release page could not be opened.'))
            except Exception as exc:
                QMessageBox.warning(self, ui_text('Cannot Open Release Page'), runtime_text(exc))

    def download_finished(self, path: str) -> None:
        if self._closed:
            return
        self.download_button.setEnabled(True)
        self.detail.setText(ui_format('Resource downloaded: {path}', path=path))
        self._refresh_install_all_button()

    def download_failed(self, error: str) -> None:
        if self._closed:
            return
        localized_error = runtime_text(error)
        self.detail.setText(ui_format('Download failed: {error}', error=localized_error))
        if self._bulk_total:
            component = self._bulk_active_component or ui_text('Unknown')
            self._bulk_active_component = ""
            self._bulk_errors.append(ui_format(
                '{component}: download failed: {error}',
                component=component,
                error=localized_error,
            ))
            QTimer.singleShot(100, self._start_next_bulk_install)
            return
        QTimer.singleShot(100, self.update_detail)

    def _refresh_installed_component_item(
        self,
        item: QTreeWidgetItem,
        payload: dict,
        *,
        component: str,
        current: str,
        source: str,
        runtime_path: str,
        fallback_location: str,
    ) -> None:
        source = str(source or fallback_location or "—")
        runtime_path = str(runtime_path or fallback_location or "—")
        current_display = component_state_text(current, fallback="Not detected")
        installed_state = {
            "current": current,
            "source": source,
            "runtime_path": runtime_path,
            "installed": True,
            "install_available": False,
            "has_update": False,
            "error": "",
        }
        payload.update(installed_state)
        item.setData(0, Qt.UserRole, payload)
        for result in self.results:
            if str(result.get("name") or "").casefold() == component.casefold():
                result.update(installed_state)
                break
        status = ui_text('Installed')
        item.setText(1, current_display)
        item.setText(2, source)
        item.setText(3, runtime_path)
        item.setText(6, status)
        item.setForeground(6, QBrush(QColor("#138a4b")))
        self._apply_component_tooltips(
            item,
            payload,
            component=component,
            current=current_display,
            source=source,
            runtime_path=runtime_path,
            status=status,
        )

    def install_finished(self, result) -> None:
        if self._closed:
            return
        component = str(result.component)
        configured = self.update_service.tool_overrides.get(component.strip().lower(), "")
        current, source, runtime_path = installed_component_details(component, configured)
        self._select_component_item(component)
        selected = self.tree.currentItem()
        if selected is not None:
            payload = selected.data(0, Qt.UserRole) or {}
            if str(payload.get("name") or "").casefold() == component.casefold():
                self._refresh_installed_component_item(
                    selected,
                    payload,
                    component=component,
                    current=current,
                    source=source,
                    runtime_path=runtime_path,
                    fallback_location=str(result.location or ""),
                )
        paths = "\n".join(result.paths)
        self.download_button.setEnabled(False)
        self.detail.setText(ui_format(
            '{component} installed: {paths}', component=component, paths=paths
        ))
        if self._bulk_total:
            self._bulk_completed.append(component)
            self._bulk_active_component = ""
            QTimer.singleShot(100, self._start_next_bulk_install)
            return
        QMessageBox.information(
            self,
            ui_text('Tool Installation Complete'),
            ui_format('{component} is installed and active now:\n{paths}', component=component, paths=paths),
        )
        self._refresh_install_all_button()

    def install_failed(self, error: str) -> None:
        if self._closed:
            return
        localized_error = runtime_text(error)
        self.detail.setText(ui_format('Installation failed: {error}', error=localized_error))
        if self._bulk_total:
            component = self._bulk_active_component or ui_text('Unknown')
            self._bulk_active_component = ""
            self._bulk_errors.append(ui_format(
                '{component}: installation failed: {error}',
                component=component,
                error=localized_error,
            ))
            QTimer.singleShot(100, self._start_next_bulk_install)
            return
        QMessageBox.warning(self, ui_text('Tool Installation Failed'), localized_error)
        QTimer.singleShot(100, self.update_detail)
