from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QMessageBox,
    QSystemTrayIcon,
)

from app.core.app_settings import AppSettings
from app.core.application_update_service import ApplicationUpdateService
from app.core.application_updater import velopack_persistent_data_dir
from app.core.cover_service import (
    CoverService,
)
from app.core.download_service import DownloadService
from app.core.download_performance import (
    effective_download_performance,
)
from app.core.language_packs import language_pack_directory
from app.core.paths import application_dir, initialize_data_layout
from app.core.publish_service import PublishService
from app.core.update_service import UpdateService
from app.core.version import APP_VERSION
from app.ui.theme import (
    THEME_LIGHT,
    THEME_SYSTEM,
    build_application_stylesheet,
    normalize_theme,
    resolve_theme,
)
from app.ui.i18n import (
    apply_runtime_translation,
    application_name_text,
    format_text as ui_format,
    runtime_text,
    text as ui_text,
)
from app.ui.navigation import (
    SidebarNavigation,
    configure_main_navigation,
    navigation_icon,
)
from app.ui.account_hub import AccountHubPage
from app.ui.about_page import AboutPage
from app.ui.completed_page import CompletedPage
from app.ui.dashboard_page import DashboardPage
from app.ui.publish_queue import PublishQueuePage
from app.ui.settings_page import SettingsPage
from app.ui.application_update_controller import (
    ApplicationUpdateController,
)
from app.ui.desktop_notification_controller import (
    DesktopNotificationController,
)
from app.ui.cover_workflow_controller import (
    CoverWorkflowController,
)
from app.ui.database_lifecycle_controller import (
    DatabaseLifecycleController,
)
from app.ui.runtime_update_dialog_controller import (
    RuntimeUpdateDialogController,
)
from app.ui.settings_save_controller import (
    SettingsSaveController,
)
from app.ui.shutdown_controller import ShutdownController
from app.ui.publish_ui_controller import PublishUiController
from app.ui.task_status_summary_controller import (
    TaskStatusSummaryController,
)
from app.storage.database import Database
from app.storage.secure_store import SecureStore

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._initialize_window_geometry()
        data_dir = initialize_data_layout()
        self._initialize_core_services(data_dir)
        self._initialize_main_pages()
        self._initialize_status_summary()
        self._connect_runtime_signals()
        self._restore_startup_state()

    def _initialize_window_geometry(self) -> None:
        self.setWindowTitle(application_name_text())
        self.setMinimumSize(900, 620)
        screen = QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry().size()
            self.resize(
                max(900, min(1180, int(available.width() * 0.88))),
                max(620, min(780, int(available.height() * 0.88))),
            )
        else:
            self.resize(1080, 700)

    def _initialize_core_services(self, data_dir: Path) -> None:
        self.app_settings = AppSettings()
        self._theme_choice = normalize_theme(self.app_settings.get("appearance_theme"))
        self._effective_theme = THEME_LIGHT
        self._style_hints = QApplication.styleHints()
        try:
            self._style_hints.colorSchemeChanged.connect(self._on_system_color_scheme_changed)
        except (AttributeError, RuntimeError):
            # Older Qt builds may not expose the system color-scheme signal;
            # the explicit light/dark choices remain fully functional.
            pass
        self.apply_theme(self._theme_choice)
        try:
            self.desktop_notifications_available = bool(
                QSystemTrayIcon.isSystemTrayAvailable()
                and QSystemTrayIcon.supportsMessages()
            )
        except RuntimeError:
            self.desktop_notifications_available = False
        self.secure_store = SecureStore()
        self.cover_service = CoverService()
        self.db = Database(data_dir / "app.db")
        self.update_service = UpdateService(
            data_dir / "updates",
            {
                "ffmpeg": self.app_settings.get("ffmpeg_path"),
                "ffprobe": self.app_settings.get("ffprobe_path"),
                "deno": self.app_settings.get("deno_path"),
            },
            ffmpeg_build_channel=self.app_settings.get("ffmpeg_build_channel"),
        )
        self.update_service.set_download_routes(
            self.app_settings.get("github_download_route"),
            self.app_settings.get("github_mirror_urls"),
            self.app_settings.get("github_route_profiles"),
        )
        self.update_service.route_probe_finished.connect(self._persist_github_route_profiles)
        application_update_dir = data_dir / "updates" / "application"
        self.application_update_dir = application_update_dir
        managed_update_data = velopack_persistent_data_dir(application_dir())
        if managed_update_data is not None:
            self.application_update_mode = "velopack"
            updater_factory = None
        else:
            self.application_update_mode = ""
            updater_factory = None
        self.application_update_service = ApplicationUpdateService(
            application_update_dir,
            updater_factory=updater_factory,
            parent=self,
        )
        self.application_updates_supported = bool(self.application_update_mode)
        task_workers, fragment_workers, request_delay = effective_download_performance(
            self.app_settings
        )
        self.download_service = DownloadService(
            self.db,
            max_concurrent=task_workers,
            request_delay=request_delay,
            fragment_concurrent=fragment_workers,
            ytdlp_core_mode=self.app_settings.get("ytdlp_core_mode"),
            deno_path=self.app_settings.get("deno_path"),
            ffprobe_path=self.app_settings.get("ffprobe_path"),
            ytdlp_ejs_source=self.app_settings.get("ytdlp_ejs_source"),
            cover_convert_jpeg=self.app_settings.get_bool("download_cover_convert_jpeg", False),
            cover_jpeg_quality=self.app_settings.get_int("cover_jpeg_quality", 90, 50, 100),
        )
        self.publish_service = PublishService(self.db)
        # A forced exit can leave a persistent row in ``uploading`` even
        # though no worker exists after restart.  Recover it before the queue
        # is first rendered so the user can retry instead of seeing a stuck
        # task forever.
        self.publish_service.recover_stale_tasks()

    def _initialize_main_pages(self) -> None:
        self.tabs = SidebarNavigation()
        configure_main_navigation(self.tabs)
        self.tabs.setCollapsed(self.app_settings.get_bool("navigation_collapsed", False))
        self.tabs.collapsedChanged.connect(self._navigation_collapsed_changed)
        self.setCentralWidget(self.tabs)
        self.cover_workflow = CoverWorkflowController(
            self,
            self.app_settings,
            self.cover_service,
        )
        self.settings = SettingsPage(self)
        self.dashboard = DashboardPage(self)
        self.account_hub = AccountHubPage(self)
        self.completed = CompletedPage(self)
        self.publish_queue = PublishQueuePage(self)
        self.about = AboutPage(self)
        self.application_update_controller = ApplicationUpdateController(self)
        self.desktop_notification_controller = DesktopNotificationController(self)
        self.publish_ui = PublishUiController(self)
        self.runtime_update_dialog_controller = RuntimeUpdateDialogController(
            self,
            self.update_service,
            self.settings.update_status.setText,
            self.settings_status,
        )
        self.database_lifecycle_controller = DatabaseLifecycleController(self)
        self.settings_save_controller = SettingsSaveController(self)
        self.about.supported_sites_requested.connect(self.dashboard.show_supported_sites)
        for name, page, icon_key in [
            (ui_text('Download Tasks'), self.dashboard, "download"),
            (ui_text('Accounts'), self.account_hub, "accounts"),
            (ui_text('Completed', context="navigation.completed"), self.completed, "completed"),
            (ui_text('Publish Queue'), self.publish_queue, "publish"),
            (ui_text('Settings'), self.settings, "settings"),
            (ui_text('About'), self.about, "about"),
        ]:
            self.tabs.addTab(page, name, navigation_icon(icon_key))

    def _initialize_status_summary(self) -> None:
        self.task_status_summary = TaskStatusSummaryController(
            self,
            self.statusBar(),
            self.download_service,
        )
        self.task_status_summary.start()
        self.shutdown_controller = ShutdownController(self)

    def _connect_runtime_signals(self) -> None:
        apply_runtime_translation(self)
        self.tabs.refreshNavigationText()
        self.tabs.currentChanged.connect(self._on_tab_changed)
        # Probe local component versions and usable encoders once per process,
        # after the event loop is responsive. Opening Settings and submitting
        # downloads reuse these caches instead of repeatedly launching every
        # runtime and GPU probe.
        QTimer.singleShot(500, self.settings.refresh_local_core_versions)

        # Keep all task-card mutations on the GUI event loop.  In particular,
        # enqueue() can emit task_added synchronously while a button handler is
        # still on the stack; a queued boundary prevents QListWidget re-entry.
        self.download_service.task_added.connect(self.dashboard.add_task, Qt.QueuedConnection)
        self.download_service.tasks_added.connect(self.dashboard.add_tasks, Qt.QueuedConnection)
        self.download_service.task_updated.connect(self.dashboard.update_task, Qt.QueuedConnection)
        self.download_service.task_progress.connect(self.dashboard.update_progress, Qt.QueuedConnection)
        self.download_service.formats_ready.connect(self.dashboard.choose_format, Qt.QueuedConnection)
        self.download_service.playlist_info.connect(self.dashboard.playlist_info, Qt.QueuedConnection)
        self.download_service.task_media_completed.connect(self.dashboard.media_completed, Qt.QueuedConnection)
        self.download_service.task_finished.connect(self.dashboard.finished, Qt.QueuedConnection)
        self.download_service.conversion_finished.connect(
            self.dashboard.conversion_finished,
            Qt.QueuedConnection,
        )
        self.download_service.conversion_failed.connect(
            self.dashboard.conversion_failed,
            Qt.QueuedConnection,
        )
        self.download_service.task_finished.connect(
            self.desktop_notification_controller.download_finished,
            Qt.QueuedConnection,
        )
        self.download_service.task_deleted.connect(self.dashboard.remove_task, Qt.QueuedConnection)
        # Progress updates already repaint the affected task card and the
        # 500 ms timer refreshes aggregate speed. Recomputing the identical
        # status-bar text twice for every progress signal creates avoidable
        # GUI work during concurrent downloads, so only structural/status
        # events request an immediate aggregate refresh.
        for signal in (
            self.download_service.task_added,
            self.download_service.tasks_added,
            self.download_service.task_updated,
            self.download_service.task_finished,
            self.download_service.task_deleted,
        ):
            signal.connect(
                self.task_status_summary.schedule_refresh,
                Qt.QueuedConnection,
            )
        self.publish_service.status.connect(self.publish_ui.status_changed)
        self.desktop_notification_controller.setup()

    def _restore_startup_state(self) -> None:
        # Restore tasks saved in SQLite after a previous application run.
        self.dashboard.begin_task_restore(self.download_service.restore_tasks())
        QTimer.singleShot(0, self.dashboard.resume_collection_probes)
        self.task_status_summary.refresh()
        self.database_lifecycle_controller.start()
        self.application_update_controller.initialize_startup()

    def run_sau_account_action(
        self,
        platform: str,
        account: str,
        action: str,
        *,
        vault_profile_id: str = "",
    ) -> bool:
        """Run the already-vendored publishing core without any bootstrap download."""
        return self.publish_service.run_account_action(
            platform,
            account,
            action,
            vault_profile_id=vault_profile_id,
        )

    def _system_color_scheme_is_dark(self) -> bool:
        """Read Qt's current system scheme without making startup fragile."""
        try:
            return self._style_hints.colorScheme() == Qt.ColorScheme.Dark
        except (AttributeError, RuntimeError):
            return False

    def apply_theme(self, choice: str | None = None) -> None:
        """Apply the selected theme immediately and retain the user choice."""
        requested = normalize_theme(choice if choice is not None else self._theme_choice)
        effective = resolve_theme(requested, self._system_color_scheme_is_dark())
        self._theme_choice = requested
        self._effective_theme = effective
        self.setStyleSheet(build_application_stylesheet(effective))

    def _navigation_collapsed_changed(self, collapsed: bool) -> None:
        self.app_settings.set("navigation_collapsed", "true" if collapsed else "false")
        self.app_settings.sync()

    def _on_system_color_scheme_changed(self, *_args) -> None:
        if self._theme_choice == THEME_SYSTEM:
            self.apply_theme(THEME_SYSTEM)

    def _on_tab_changed(self, index: int) -> None:
        page = self.tabs.widget(index)
        if page is self.completed:
            self.completed.ensure_loaded()
        elif page is self.publish_queue:
            self.publish_queue.ensure_loaded()
        elif page is self.settings:
            self.settings.refresh_runtime_component_status()

    @Slot(object)
    def _persist_github_route_profiles(self, _results: object) -> None:
        profiles = self.update_service.serialized_route_profiles()
        self.app_settings.set("github_route_profiles", profiles)
        self.app_settings.sync()
        self.settings.github_route_profiles = profiles

    def check_application_update(self) -> None:
        self.application_update_controller.check()

    def install_application_update(
        self,
        update,
        *,
        confirmed: bool = False,
    ) -> None:
        self.application_update_controller.install(update, confirmed=confirmed)

    def save_settings(self, page: SettingsPage, scope: str) -> bool:
        return self.settings_save_controller.save(page, scope)

    def open_log_directory(self) -> None:
        path = self.download_service.logs.root
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))
        except OSError as exc:
            QMessageBox.warning(
                self,
                ui_text('Unable to Open Folder'),
                ui_format('Unable to open log folder:\n{path}\n\n{error}', path=path, error=runtime_text(exc)),
            )

    def open_language_pack_directory(self) -> None:
        path = language_pack_directory()
        try:
            os.startfile(str(path))
        except OSError as exc:
            QMessageBox.warning(
                self,
                ui_text('Unable to Open Folder'),
                ui_format('Unable to open language-pack folder:\n{path}\n\n{error}', path=path, error=runtime_text(exc)),
            )

    def check_updates(self) -> None:
        self.runtime_update_dialog_controller.check()

    def settings_status(self, message: str) -> None:
        # Keep the feedback visible without adding another persistent status
        # field to the settings layout.
        self.statusBar().showMessage(runtime_text(message), 5000)

    def closeEvent(self, event) -> None:
        self.shutdown_controller.handle_close_event(event)
