from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.github_mirrors import (
    ROUTE_AUTO,
    ROUTE_DIRECT,
    github_download_routes,
    normalize_mirror_base_url,
    parse_custom_mirror_urls,
)
from app.core.update_service import UpdateService
from app.ui.github_route_presentation import github_route_display_name
from app.ui.i18n import format_text as ui_format
from app.ui.i18n import runtime_text, text as ui_text


class GithubMirrorDialog(QDialog):
    """Manage and probe GitHub metadata and Release-asset routes."""

    def __init__(
        self,
        settings_page: Any,
        parent: QWidget | None = None,
    ):
        super().__init__(parent or settings_page)  # type: ignore[arg-type]
        self.settings_page = settings_page
        self.update_service: UpdateService = settings_page.window.update_service
        self._closed = False

        self.setWindowTitle(ui_text("GitHub Download Routes"))
        self.resize(820, 430)
        layout = QVBoxLayout(self)

        note = QLabel(
            ui_text(
                "Routes are used for both Release metadata and assets. "
                "Third-party CDNs/proxies may lag behind upstream, so detected "
                "versions may not be real-time latest; assets are still "
                "SHA-256 verified."
            )
        )
        note.setObjectName("mutedText")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(
            [
                ui_text("Route"),
                ui_text("Address"),
                ui_text("Detected Rule"),
                ui_text("Capabilities"),
                ui_text("Latency"),
                ui_text("Status"),
            ]
        )
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeToContents)
        layout.addWidget(self.tree, 1)

        actions = QHBoxLayout()
        self.probe_button = QPushButton(ui_text("Test All"))
        self.probe_button.clicked.connect(self.probe_routes)
        self.select_button = QPushButton(ui_text("Use Selected Route"))
        self.select_button.clicked.connect(self.select_route)
        add_button = QPushButton(ui_text("Add Custom"))
        add_button.clicked.connect(self.add_custom_route)
        self.remove_button = QPushButton(ui_text("Remove Custom"))
        self.remove_button.clicked.connect(self.remove_custom_route)
        close_button = QPushButton(ui_text("Done"))
        close_button.clicked.connect(self.accept)
        actions.addWidget(self.probe_button)
        actions.addWidget(self.select_button)
        actions.addWidget(add_button)
        actions.addWidget(self.remove_button)
        actions.addStretch(1)
        actions.addWidget(close_button)
        layout.addLayout(actions)

        self.status = QLabel(
            ui_text("Auto mode prefers the fastest recently tested safe route.")
        )
        self.status.setObjectName("mutedText")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.tree.itemSelectionChanged.connect(self._update_actions)
        self.update_service.route_probe_finished.connect(self._probe_finished)
        self.update_service.route_probe_failed.connect(self._probe_failed)
        self.refresh_rows()
        if self._probe_running():
            self._set_probe_running(
                ui_text(
                    "A route test is already running; results will appear "
                    "when it completes."
                )
            )

    def _probe_running(self) -> bool:
        return self.update_service.runtime_active("route_probe")

    def _set_probe_running(self, message: str) -> None:
        self.probe_button.setEnabled(False)
        self.status.setText(message)

    def _disconnect_service_signals(self) -> None:
        for signal, callback in (
            (self.update_service.route_probe_finished, self._probe_finished),
            (self.update_service.route_probe_failed, self._probe_failed),
        ):
            try:
                signal.disconnect(callback)
            except (RuntimeError, TypeError):
                pass

    def _mark_closed(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._disconnect_service_signals()

    def done(self, result: int) -> None:
        self._mark_closed()
        super().done(result)

    def closeEvent(self, event) -> None:
        self._mark_closed()
        super().closeEvent(event)

    def refresh_rows(self) -> None:
        selected_id = ""
        selected = self.tree.currentItem()
        if selected is not None:
            selected_id = str(selected.data(0, Qt.UserRole) or "")
        self.tree.clear()
        for route in github_download_routes(self.settings_page.github_mirror_urls):
            probe = self.update_service.route_probe_results.get(route.id, {})
            latency = int(probe.get("latency_ms") or 0)
            status = runtime_text(probe.get("status") or ui_text("Not tested"))
            address = (
                "https://data.jsdelivr.com/ · https://cdn.jsdelivr.net/"
                if route.kind == "jsdelivr"
                else route.base_url or "https://github.com/"
            )
            detected_kind = str(
                probe.get("detected_kind") or route.kind or "auto"
            ).casefold()
            kind_text = {
                "direct": ui_text("GitHub Direct"),
                "prefix": ui_text("Full URL Prefix"),
                "host": ui_text("Host Replacement"),
                "jsdelivr": "jsDelivr /gh/",
                "auto": ui_text("Auto-detect pending"),
            }.get(detected_kind, detected_kind)
            capabilities: list[str] = []
            if probe.get("metadata_ok"):
                capabilities.append(ui_text("Metadata"))
            if probe.get("asset_ok"):
                capabilities.append(ui_text("Assets"))
            if probe.get("cdn_ok"):
                capabilities.append("CDN /gh/")
            if not probe and route.kind == "jsdelivr":
                capabilities.append(ui_text("Metadata/CDN"))
            capability_text = " + ".join(capabilities) or "—"
            metadata_latency = int(probe.get("metadata_latency_ms") or 0)
            asset_latency = int(probe.get("asset_latency_ms") or 0)
            latency_parts = []
            if metadata_latency:
                latency_parts.append(f"M {metadata_latency} ms")
            if asset_latency:
                latency_parts.append(f"A {asset_latency} ms")
            latency_text = " / ".join(latency_parts) or (
                f"{latency} ms" if latency else "—"
            )
            item = QTreeWidgetItem(
                [
                    github_route_display_name(route.id, route.name),
                    address,
                    kind_text,
                    capability_text,
                    latency_text,
                    status,
                ]
            )
            item.setData(0, Qt.UserRole, route.id)
            item.setData(0, Qt.UserRole + 1, route.base_url)
            item.setData(0, Qt.UserRole + 2, route.third_party)
            item.setToolTip(1, address)
            if route.kind == "jsdelivr":
                item.setToolTip(
                    1,
                    address
                    + "\n\n"
                    + ui_text(
                        "Uses the jsDelivr Public API to read third-party GitHub "
                        "repository versions. EXE/ZIP files uploaded separately "
                        "to GitHub Releases automatically use an asset-capable route."
                    ),
                )
            self.tree.addTopLevelItem(item)
            if route.id == selected_id:
                self.tree.setCurrentItem(item)
        if self.tree.currentItem() is None and self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        self._update_actions()

    def _update_actions(self) -> None:
        item = self.tree.currentItem()
        route_id = str(item.data(0, Qt.UserRole) or "") if item else ""
        self.select_button.setEnabled(bool(item))
        self.remove_button.setEnabled(route_id.startswith("custom:"))

    def probe_routes(self) -> None:
        try:
            self.update_service.set_download_routes(
                str(
                    self.settings_page.github_download_route.currentData()
                    or ROUTE_AUTO
                ),
                self.settings_page.github_mirror_urls,
                self.settings_page.github_route_profiles,
            )
        except ValueError as exc:
            QMessageBox.warning(
                self,
                ui_text("Invalid Route Configuration"),
                runtime_text(exc),
            )
            return

        started = self.update_service.probe_download_routes()
        if started:
            self._set_probe_running(
                ui_text(
                    "Testing full-URL prefix, host replacement, jsDelivr /gh/, "
                    "metadata and Release assets in parallel…"
                )
            )
        elif self._probe_running():
            self._set_probe_running(
                ui_text(
                    "A route test is already running; results will appear "
                    "when it completes."
                )
            )
        else:
            self.probe_button.setEnabled(True)
            self.status.setText(
                ui_text(
                    "The route test could not be started. Check the update "
                    "service state and try again."
                )
            )

    @Slot(object)
    def _probe_finished(self, results) -> None:
        if self._closed:
            return
        self.probe_button.setEnabled(True)
        self.settings_page.github_route_profiles = (
            self.update_service.serialized_route_profiles()
        )
        self.settings_page.window.app_settings.set(
            "github_route_profiles",
            self.settings_page.github_route_profiles,
        )
        self.settings_page.window.app_settings.sync()
        usable = [row for row in results if row.get("usable")]
        if usable:
            fastest = min(
                usable,
                key=lambda row: int(row.get("latency_ms") or 10**9),
            )
            self.status.setText(
                ui_format(
                    "Route detection completed and was saved. Fastest available "
                    "route: {name} · {latency} ms. Auto mode selects metadata "
                    "and asset routes separately.",
                    name=fastest.get("name"),
                    latency=fastest.get("latency_ms"),
                )
            )
        else:
            self.status.setText(
                ui_text("Test complete, but no usable route was found.")
            )
        self.refresh_rows()

    @Slot(str)
    def _probe_failed(self, error: str) -> None:
        if self._closed:
            return
        self.probe_button.setEnabled(True)
        self.status.setText(ui_text("Route test failed: ") + runtime_text(error))

    def select_route(self) -> None:
        item = self.tree.currentItem()
        if item is None:
            return
        route_id = str(item.data(0, Qt.UserRole) or ROUTE_DIRECT)
        self.settings_page.refresh_github_route_combo(route_id)
        self.status.setText(
            ui_format(
                "Selected: {route}. Save Settings to persist the choice.",
                route=item.text(0),
            )
        )

    def add_custom_route(self) -> None:
        value, accepted = QInputDialog.getText(
            self,
            ui_text("Add GitHub Proxy"),
            ui_text("HTTP/HTTPS prefix, e.g. http://proxy.example/"),
        )
        if not accepted or not value.strip():
            return
        try:
            normalized = normalize_mirror_base_url(value)
            urls = list(
                parse_custom_mirror_urls(self.settings_page.github_mirror_urls)
            )
            if normalized not in urls:
                urls.append(normalized)
            self.settings_page.github_mirror_urls = "\n".join(urls)
        except ValueError as exc:
            QMessageBox.warning(
                self,
                ui_text(
                    "Invalid Proxy Address",
                    context="github_proxy.invalid_address",
                ),
                runtime_text(exc),
            )
            return
        self.settings_page.refresh_github_route_combo()
        self.refresh_rows()

    def remove_custom_route(self) -> None:
        item = self.tree.currentItem()
        if item is None or not str(
            item.data(0, Qt.UserRole) or ""
        ).startswith("custom:"):
            return
        base_url = str(item.data(0, Qt.UserRole + 1) or "")
        urls = [
            url
            for url in parse_custom_mirror_urls(
                self.settings_page.github_mirror_urls
            )
            if url != base_url
        ]
        self.settings_page.github_mirror_urls = "\n".join(urls)
        self.settings_page.refresh_github_route_combo()
        self.refresh_rows()
