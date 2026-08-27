from __future__ import annotations

import os
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QThread, Qt
from PySide6.QtGui import QColor, QFont, QImage
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QComboBox,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QWidget,
)

from app.core.cover_service import CoverService
from app.core.download_service import DownloadTask
from app.core.log_service import DownloadLogService
from app.core.update_service import UpdateService
from app.storage.models import MediaItem
from app.ui.cover_studio import CoverStudioDialog
from app.ui.download_dialogs import DownloadLogDialog, DownloadReadinessDialog
from app.ui.navigation import SidebarNavigation
from app.ui.path_widgets import (
    CompactPathLineEdit,
    PortablePathLineEdit,
    native_path_display,
)
from app.ui.runtime_components_dialog import UpdateDialog
from app.ui.main_window import MainWindow
from app.ui.settings_save_controller import (
    SettingsSaveController,
    SettingsSavePlan,
)
from app.ui.settings_page import SettingsPage
from app.ui.media_presentation import compact_path_display
from app.ui.runtime import configure_font, configure_high_dpi


class _Settings:
    def __init__(self, **values: str):
        self.values = values
        self.sync_count = 0

    def get(self, key: str) -> str:
        return str(self.values.get(key, ""))

    def get_int(
        self,
        key: str,
        default: int,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        value = int(self.values.get(key, default))
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def get_float(
        self,
        key: str,
        default: float,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float:
        value = float(self.values.get(key, default))
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = str(self.values.get(key, "true" if default else "false")).casefold()
        return value in {"1", "true", "yes", "on"}

    def set(self, key: str, value: str) -> None:
        self.values[key] = str(value)

    def normalize_value(self, _key: str, value: str) -> str:
        return str(value if value is not None else "")

    def set_many(self, values: dict[str, str]) -> dict[str, str]:
        normalized = {
            key: self.normalize_value(key, value)
            for key, value in values.items()
        }
        self.values.update(normalized)
        self.sync()
        return normalized

    def sync(self) -> None:
        self.sync_count += 1


def _settings_save_controller(host) -> SettingsSaveController:
    no_op = lambda *args, **kwargs: None
    secure_store = getattr(host, "secure_store", None) or SimpleNamespace(
        get=lambda _key: None,
        set=no_op,
        delete=no_op,
    )
    host.app_settings = getattr(host, "app_settings", _Settings())
    host.secure_store = secure_store
    host.download_service = getattr(host, "download_service", SimpleNamespace())
    host.update_service = getattr(host, "update_service", SimpleNamespace())
    host.dashboard = getattr(host, "dashboard", SimpleNamespace())
    host.completed = getattr(host, "completed", SimpleNamespace())
    host.application_updates_supported = bool(
        getattr(host, "application_updates_supported", False)
    )
    host.apply_theme = getattr(host, "apply_theme", no_op)
    host.desktop_notification_controller = SimpleNamespace(
        sync_visibility=getattr(host, "_sync_desktop_notification_visibility", no_op)
    )
    host.application_update_controller = getattr(
        host,
        "application_update_controller",
        SimpleNamespace(configure=lambda **_kwargs: None),
    )
    host.application_update_service = getattr(
        host,
        "application_update_service",
        SimpleNamespace(clear_configuration=no_op),
    )
    return SettingsSaveController(host)


class EnglishUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.previous_forced_locale = os.environ.get("HUIFA_UI_LOCALE")
        existing_app = QApplication.instance()
        cls.reused_application = existing_app is not None
        cls.previous_locale = (
            existing_app.property("huifa.ui_locale") if existing_app else None
        )
        cls.previous_translations = (
            existing_app.property("huifa.ui_translations") if existing_app else None
        )
        cls.previous_font = QFont(existing_app.font()) if existing_app else None
        os.environ["HUIFA_UI_LOCALE"] = "en-US"
        if existing_app is None:
            configure_high_dpi()
            cls.app = QApplication([])
        else:
            cls.app = existing_app
        configure_font(cls.app, "en-US")
        cls.app.setProperty("huifa.ui_locale", "en-US")

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.previous_forced_locale is None:
            os.environ.pop("HUIFA_UI_LOCALE", None)
        else:
            os.environ["HUIFA_UI_LOCALE"] = cls.previous_forced_locale
        if cls.reused_application:
            cls.app.setProperty("huifa.ui_locale", cls.previous_locale or "zh-CN")
            cls.app.setProperty("huifa.ui_translations", cls.previous_translations)
            if cls.previous_font is not None:
                cls.app.setFont(cls.previous_font)
        else:
            configure_font(cls.app, "auto")

    def _minimal_settings_page(self, **setting_overrides: str) -> SettingsPage:
        no_op = lambda *args, **kwargs: None
        signal = SimpleNamespace(connect=no_op)
        active_runtimes: set[str] = set()
        update_service = SimpleNamespace(
            result_ready=signal,
            finished=signal,
            failed=signal,
            download_failed=signal,
            install_finished=signal,
            install_failed=signal,
            last_results=[],
            active_runtimes=active_runtimes,
            runtime_active=lambda *kinds: any(
                kind in active_runtimes for kind in kinds
            ),
            set_ffmpeg_build_channel=no_op,
            start_background_route_probe=no_op,
            check=no_op,
            download_asset=no_op,
        )
        settings_values = {"download_options_json": "{}", **setting_overrides}
        publish_service = SimpleNamespace(
            account_status=signal,
            is_account_action_running=lambda _platform, _account: False,
        )
        window = SimpleNamespace(
            app_settings=_Settings(**settings_values),
            update_service=update_service,
            publish_service=publish_service,
            run_sau_account_action=lambda *_args, **_kwargs: False,
            secure_store=SimpleNamespace(get=lambda _key: ""),
            desktop_notifications_available=True,
            application_updates_supported=False,
            application_update_mode="",
            dashboard=SimpleNamespace(
                show_download_readiness=no_op,
                refresh_settings=no_op,
            ),
            open_log_directory=no_op,
            check_application_update=no_op,
            check_updates=no_op,
            save_settings=no_op,
            settings_status=no_op,
        )
        return SettingsPage(window)

    def test_invalid_saved_quality_falls_back_to_best_instead_of_manual(self) -> None:
        page = self._minimal_settings_page(quality="removed-quality-name")
        try:
            self.assertEqual(page.quality.currentData(), "best")
        finally:
            page.close()

    def test_local_core_thread_start_failure_restores_usable_settings_state(self) -> None:
        page = self._minimal_settings_page()
        try:
            with patch(
                "app.ui.local_core_version_controller.QThread.start",
                side_effect=RuntimeError("thread resource exhausted"),
            ):
                page.refresh_local_core_versions(force=True)

            self.assertFalse(page.local_core_version_check_running)
            self.assertIsNone(page.local_core_versions._runtime)
            self.assertTrue(page.transcode_encoder.isEnabled())
            self.assertIn("thread resource exhausted", page.transcode_encoder_status.text())
            self.assertEqual(page.runtime_version_labels["Deno"].text(), "Detection failed")
        finally:
            page.close()

    def test_local_core_wiring_failure_restores_usable_settings_state(self) -> None:
        page = self._minimal_settings_page()
        try:
            with patch(
                "app.ui.local_core_version_controller.LocalCoreVersionWorker.moveToThread",
                side_effect=RuntimeError("signal wiring failed"),
            ), patch(
                "app.ui.local_core_version_controller.delete_unstarted_worker",
            ) as delete_worker, patch(
                "app.ui.local_core_version_controller.QThread.start",
            ) as start_thread:
                page.refresh_local_core_versions(force=True)

            start_thread.assert_not_called()
            delete_worker.assert_called_once()
            self.assertFalse(page.local_core_version_check_running)
            self.assertIsNone(page.local_core_versions._runtime)
            self.assertTrue(page.transcode_encoder.isEnabled())
            self.assertIn("signal wiring failed", page.transcode_encoder_status.text())
            self.assertEqual(page.runtime_version_labels["Deno"].text(), "Detection failed")
        finally:
            page.close()

    def test_local_core_thread_construction_failure_restores_settings_state(self) -> None:
        page = self._minimal_settings_page()
        try:
            with patch(
                "app.ui.local_core_version_controller.QThread",
                side_effect=RuntimeError("thread construction failed"),
            ):
                page.refresh_local_core_versions(force=True)

            self.assertFalse(page.local_core_version_check_running)
            self.assertIsNone(page.local_core_versions._runtime)
            self.assertTrue(page.transcode_encoder.isEnabled())
            self.assertIn("thread construction failed", page.transcode_encoder_status.text())
            self.assertEqual(page.runtime_version_labels["Deno"].text(), "Detection failed")
        finally:
            page.close()

    def test_local_core_cleanup_waits_for_queued_results(self) -> None:
        page = self._minimal_settings_page()
        try:
            thread = QThread(page)
            page.local_core_versions._runtime = (thread, object())
            results = {
                "yt-dlp": ("2026.08.25", "local", "yt-dlp.exe"),
                "yt-dlp-ejs": ("0.9.0", "local", "ejs.whl"),
                "Deno": ("2.5.0", "local", "deno.exe"),
                "FFmpeg": ("8.0", "local", "ffmpeg.exe"),
                "FFprobe": ("8.0", "local", "ffprobe.exe"),
                "__video_encoders__": {"items": ("libx264",), "error": ""},
            }

            page.local_core_versions.defer_finish(thread)
            page.local_core_versions.versions_ready(results)
            self.assertTrue(page.local_core_version_check_running)
            self.app.processEvents()

            self.assertFalse(page.local_core_version_check_running)
            self.assertIsNone(page.local_core_versions._runtime)
            self.assertEqual(page.runtime_version_labels["Deno"].text(), "2.5.0")
            self.assertEqual(page.transcode_encoder.findData("libx264"), 1)
        finally:
            page.close()

    def test_local_core_shutdown_cancels_without_releasing_live_runtime(self) -> None:
        page = self._minimal_settings_page()
        try:
            thread = Mock()
            thread.isRunning.return_value = True
            worker = Mock()
            page.local_core_versions._runtime = (thread, worker)

            page.local_core_versions.request_shutdown()

            worker.cancel.assert_called_once_with()
            thread.requestInterruption.assert_called_once_with()
            thread.quit.assert_called_once_with()
            self.assertEqual(page.local_core_versions._runtime, (thread, worker))

            page.local_core_versions.complete_finish(thread)
            self.assertIsNone(page.local_core_versions._runtime)
            thread.deleteLater.assert_called_once_with()
        finally:
            page.close()

    def test_ffmpeg_install_paths_are_saved_as_one_settings_transaction(self) -> None:
        page = self._minimal_settings_page()
        page.window.download_service = SimpleNamespace(ffprobe_path="")
        page.window.update_service.set_tool_overrides = Mock()
        result = SimpleNamespace(
            component="FFmpeg",
            paths=(
                "D:/Huifa/tools/ffmpeg/x64/ffmpeg.exe",
                "D:/Huifa/tools/ffmpeg/x64/ffprobe.exe",
            ),
        )
        try:
            with patch.object(
                page.window.app_settings,
                "set_many",
                wraps=page.window.app_settings.set_many,
            ) as set_many:
                page._apply_installed_runtime_paths(result)

            set_many.assert_called_once_with({
                "ffmpeg_path": "D:/Huifa/tools/ffmpeg/x64/ffmpeg.exe",
                "ffprobe_path": "D:/Huifa/tools/ffmpeg/x64/ffprobe.exe",
            })
            self.assertEqual(page.ffmpeg.text(), result.paths[0])
            self.assertEqual(page.ffprobe.text(), result.paths[1])
            self.assertEqual(page.window.download_service.ffprobe_path, result.paths[1])
        finally:
            page.close()

    def setUp(self) -> None:
        self.app.setProperty("huifa.ui_locale", "en-US")

    def test_sidebar_navigation_keeps_english_labels_when_collapsed(self) -> None:
        navigation = SidebarNavigation()
        navigation.addTab(QWidget(), "Download Tasks")
        navigation.addTab(QWidget(), "Settings")
        try:
            self.assertEqual(navigation.brand_label.text(), "Huifa")
            self.assertEqual(navigation.collapse_button.toolTip(), "Collapse navigation")
            navigation.setCollapsed(True)
            self.assertEqual(navigation.navigationButton(0).text(), "Download Tasks")
            self.assertEqual(navigation.navigationButton(0).toolTip(), "Download Tasks")
            self.assertEqual(navigation.collapse_button.toolTip(), "Expand navigation")
        finally:
            navigation.deleteLater()

    def test_download_readiness_translates_details_and_tooltips(self) -> None:
        host = QWidget()
        window = SimpleNamespace(
            app_settings=_Settings(download_dir="D:/youtube"),
            tabs=SimpleNamespace(setCurrentWidget=lambda _widget: None),
            settings=object(),
            check_updates=lambda: None,
        )
        rows = [
            {
                "name": "下载核心（yt-dlp）",
                "state": "可用",
                "detail": "版本 2026.08.19 · Python 环境 yt-dlp 模块",
            },
            {
                "name": "JavaScript 运行时（Deno）",
                "state": "可用",
                "detail": "已找到：系统 PATH deno.EXE · C:/Program Files/Deno/deno.EXE",
            },
            {
                "name": "下载 Cookie",
                "state": "未配置",
                "detail": "公开内容通常不需要；私密、限龄或需登录内容请在设置中选择 Netscape Cookie 文件",
            },
        ]
        with patch("app.ui.download_dialogs.download_readiness_report", return_value=(True, rows)):
            dialog = DownloadReadinessDialog(window, host)
            self.app.processEvents()
            try:
                self.assertEqual(dialog.tree.topLevelItem(0).text(0), "下载核心（yt-dlp）")
                details = [dialog.tree.topLevelItem(index).text(2) for index in range(3)]
                tooltips = [dialog.tree.topLevelItem(index).toolTip(2) for index in range(3)]
                self.assertEqual(details[0], "版本 2026.08.19 · Python 环境 yt-dlp 模块")
                self.assertEqual(details[1], "已找到：系统 PATH deno.EXE · C:/Program Files/Deno/deno.EXE")
                self.assertEqual(details[2], "公开内容通常不需要；私密、限龄或需登录内容请在设置中选择 Netscape Cookie 文件")
                self.assertEqual(tooltips, details)
            finally:
                dialog.close()
                host.close()
                self.app.processEvents()

    def test_download_log_preserves_service_diagnosis_and_category_tooltip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            logs = DownloadLogService(Path(directory) / "logs")
            task = DownloadTask(
                "english-log",
                "https://example.com/video",
                directory,
                title="Example",
                status="failed",
                stage="failed",
                stage_text="下载失败",
                error="network error",
            )
            logs.write(task.id, "error", "网络/代理", "Download failed")
            dialog = DownloadLogDialog(task, logs)
            try:
                summary = next(
                    label
                    for label in dialog.findChildren(QLabel)
                    if "Status: Failed" in label.text()
                )
                self.assertIn("Stage: Download failed", summary.text())
                self.assertIn("Diagnosis: 网络/代理", summary.text())
                self.assertEqual(summary.toolTip(), "网络/代理: 1")
            finally:
                dialog.close()
                self.app.processEvents()

    def test_runtime_update_dialog_preserves_service_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = UpdateService(Path(directory) / "updates")
            host = QWidget()
            dialog = UpdateDialog(
                [
                    {
                        "name": "FFmpeg",
                        "current": "n9.0.1",
                        "source": "程序目录 ffmpeg.exe",
                        "runtime_path": "D:/ffmpeg.exe",
                        "latest": "n9.0.1",
                        "assets": [],
                        "auto_install_supported": False,
                        "has_update": False,
                        "url": "https://example.com/releases",
                    }
                ],
                service,
                host,
            )
            try:
                item = dialog.tree.topLevelItem(0)
                self.assertEqual(item.text(2), "程序目录 ffmpeg.exe")
                self.assertEqual(item.text(5), "GitHub Direct")
                self.assertEqual(item.text(6), "Up to date")
                self.assertIn("current available version", item.toolTip(1))
                self.assertIn("程序目录 ffmpeg.exe", item.toolTip(2))
                self.assertIn("Current runtime path", item.toolTip(3))
                self.assertIn("directly from GitHub", item.toolTip(5))
                self.assertEqual(item.toolTip(6), "Up to date")
                self.assertIn("maintained externally", dialog.detail.text())
            finally:
                dialog.close()
                host.close()
                service.shutdown(timeout_ms=0)
                self.app.processEvents()

    def test_runtime_update_dialog_offers_one_click_local_core_setup(self) -> None:
        def result(name: str, asset_name: str, repo: str) -> dict:
            return {
                "name": name,
                "current": "Not installed",
                "source": "",
                "runtime_path": "",
                "latest": "1.0.0",
                "assets": [{
                    "name": asset_name,
                    "size": 1024,
                    "browser_download_url": f"https://github.com/{repo}/releases/download/v1/{asset_name}",
                }],
                "auto_install_supported": True,
                "install_available": True,
                "has_update": False,
                "url": f"https://github.com/{repo}/releases",
            }

        with tempfile.TemporaryDirectory() as directory:
            service = UpdateService(Path(directory) / "updates")
            host = QWidget()
            dialog = UpdateDialog(
                [
                    result("Deno", "deno-x86_64-pc-windows-msvc.zip", "denoland/deno"),
                    result("yt-dlp-ejs", "yt_dlp_ejs-0.8.0-py3-none-any.whl", "yt-dlp/ejs"),
                    result("FFmpeg", "ffmpeg-master-latest-win64-gpl.zip", "yt-dlp/FFmpeg-Builds"),
                    result("yt-dlp", "yt-dlp.exe", "yt-dlp/yt-dlp"),
                ],
                service,
                host,
            )
            try:
                self.assertTrue(dialog.install_all_button.isEnabled())
                self.assertEqual(
                    [item[0]["name"] for item in dialog._bulk_install_candidates()],
                    ["yt-dlp", "FFmpeg", "Deno", "yt-dlp-ejs"],
                )
                self.assertEqual(dialog.install_all_button.text(), "Install/Update All Local Cores")
            finally:
                dialog.close()
                host.close()
                service.shutdown(timeout_ms=0)
                self.app.processEvents()

    def test_cover_studio_translates_preset_and_focus_tooltips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cover_path = Path(directory) / "cover.png"
            image = QImage(320, 180, QImage.Format.Format_ARGB32)
            image.fill(QColor("#336699"))
            self.assertTrue(image.save(str(cover_path), "PNG"))
            service = CoverService()

            class Host(QWidget):
                def __init__(self) -> None:
                    super().__init__()
                    self.cover_service = service
                    self.app_settings = _Settings(
                        cover_preset="landscape_16_9",
                        cover_fit_mode="crop",
                        cover_jpeg_quality="90",
                        cover_focus_x="50",
                        cover_focus_y="50",
                    )

            host = Host()
            dialog = CoverStudioDialog(
                MediaItem(id=1, title="Example cover studio", thumbnail_path=str(cover_path)),
                host,
            )
            try:
                self.assertEqual(dialog.preset.itemText(0), "Landscape 16:9 (1280×720)")
                self.assertEqual(dialog.preset.itemText(1), "Douyin Portrait 9:16 (1080×1920)")
                self.assertEqual(dialog.preset.itemText(2), "WeChat Channels Portrait 3:4 (1080×1440)")
                self.assertEqual(dialog.preset.itemText(3), "WeChat / General Landscape 4:3 (1440×1080)")
                self.assertEqual(dialog.preset.itemText(4), "Square 1:1 (1080×1080)")
                self.assertEqual(dialog.fit.itemText(0), "Smart crop to fill")
                self.assertIn("Douyin", dialog.preset.itemData(1, Qt.ToolTipRole))
                self.assertIn("WeChat Channels", dialog.preset.itemData(2, Qt.ToolTipRole))
                self.assertEqual(dialog.focus_x.toolTip(), "0% left, 50% center, 100% right")
                self.assertEqual(dialog.focus_y.toolTip(), "0% top, 50% center, 100% bottom")
            finally:
                dialog.close()
                host.close()
                service.close()
                self.app.processEvents()

    def test_settings_page_has_no_chinese_chrome_in_english_locale(self) -> None:
        settings = _Settings(
            download_dir="D:/youtube",
            filename_template="%(title)s [%(id)s].%(ext)s",
            quality="best",
            playlist_mode="auto",
            download_performance_mode="smart",
            max_concurrent="3",
            fragment_concurrent="12",
            request_delay="0",
            cover_preset="landscape_16_9",
            cover_fit_mode="crop",
            download_cover_convert_jpeg="true",
            cover_jpeg_quality="90",
            cover_focus_x="50",
            cover_focus_y="50",
            cover_ai_api_url="https://api.example.com/v1",
            desktop_notifications="true",
            appearance_theme="system",
            auto_check_updates="true",
        )
        no_op = lambda *args, **kwargs: None
        signal = SimpleNamespace(connect=no_op)
        downloaded: list[tuple[dict, str]] = []
        selected_ffmpeg_channels: list[str] = []
        saved_settings: list[object] = []
        update_service = SimpleNamespace(
            result_ready=signal,
            finished=signal,
            failed=signal,
            download_failed=signal,
            install_finished=signal,
            install_failed=signal,
            last_results=[],
            runtime_active=lambda *_kinds: False,
            set_ffmpeg_build_channel=selected_ffmpeg_channels.append,
            start_background_route_probe=no_op,
            check=no_op,
            download_asset=lambda asset, component: downloaded.append((asset, component)),
        )
        publish_service = SimpleNamespace(
            account_status=signal,
            is_account_action_running=lambda _platform, _account: False,
        )
        window = SimpleNamespace(
            app_settings=settings,
            update_service=update_service,
            publish_service=publish_service,
            run_sau_account_action=lambda *_args, **_kwargs: False,
            secure_store=SimpleNamespace(get=lambda _key: ""),
            desktop_notifications_available=True,
            application_updates_supported=False,
            application_update_mode="",
            dashboard=SimpleNamespace(
                show_download_readiness=no_op,
                refresh_settings=no_op,
            ),
            open_log_directory=no_op,
            check_application_update=no_op,
            check_updates=no_op,
            save_settings=lambda page, scope: saved_settings.append((page, scope)),
            settings_status=no_op,
        )
        page = SettingsPage(window)
        try:
            self.assertTrue(page.cover_convert_jpeg.isChecked())
            self.assertEqual(
                [
                    page.download_content_mode.itemText(index)
                    for index in range(page.download_content_mode.count())
                ],
                ["Manual", "Video", "Audio"],
            )
            self.assertEqual(
                [
                    page.download_container.itemText(index)
                    for index in range(page.download_container.count())
                ],
                ["Automatic", "MP4", "MKV"],
            )
            page.download_container.setCurrentIndex(
                page.download_container.findData("mkv")
            )
            page.download_content_mode.setCurrentIndex(
                page.download_content_mode.findData("audio")
            )
            self.assertFalse(page.download_container.isEnabled())
            self.assertEqual(page.download_container.currentData(), "mkv")
            page.download_content_mode.setCurrentIndex(
                page.download_content_mode.findData("video")
            )
            self.assertTrue(page.download_container.isEnabled())
            self.assertEqual(page.download_container.currentData(), "mkv")
            group_save_buttons = [
                button
                for button in page.findChildren(QAbstractButton)
                if button.property("settingsGroupSave")
            ]
            self.assertEqual(len(group_save_buttons), 7)
            self.assertEqual(
                {button.property("settingsScope") for button in group_save_buttons},
                {"download", "network", "experience", "appearance", "tools", "cover", "updates"},
            )
            self.assertFalse(any(button.text() == "Folder Settings" for button in page.findChildren(QAbstractButton)))
            visible_texts: list[tuple[str, str]] = []
            for widget in [page, *page.findChildren(QWidget)]:
                if isinstance(widget, QGroupBox):
                    visible_texts.append(("group title", widget.title()))
                if isinstance(widget, (QAbstractButton, QLabel)):
                    visible_texts.append(("text", widget.text()))
                if isinstance(widget, QLineEdit):
                    visible_texts.append(("placeholder", widget.placeholderText()))
                if isinstance(widget, QComboBox):
                    if widget is not page.ui_language:
                        visible_texts.extend(
                            ("combo item", widget.itemText(index))
                            for index in range(widget.count())
                        )
                visible_texts.append(("tooltip", widget.toolTip()))
            chinese = [
                (kind, value)
                for kind, value in visible_texts
                if value and re.search(r"[\u3400-\u9fff]", value)
            ]
            self.assertEqual(chinese, [])
            self.assertEqual(page.performance_mode.itemText(0), "Smart (recommended)")
            self.assertEqual(page.performance_mode.itemText(1), "Manual")
            labels = {label.text() for label in page.findChildren(QLabel)}
            self.assertEqual(page.cover_ai_api_url.text(), "https://api.example.com/v1")
            self.assertEqual(page.cover_ai_api_url.placeholderText(), "Leave blank for OpenAI; enter a compatible service /v1 URL")
            self.assertIn("yt-dlp-ejs Source", labels)
            self.assertIn("Deno Path", labels)
            self.assertIn("FFmpeg Path", labels)
            self.assertIn("FFprobe Path", labels)
            page.local_core_versions.versions_ready(
                {
                    "yt-dlp": ("2026.08.25", "local", "yt-dlp.exe"),
                    "yt-dlp-ejs": ("0.9.0", "local", "ejs.whl"),
                    "Deno": ("2.5.0", "local", "D:/code/yt-release/tools/deno/x64/deno.exe"),
                    "FFmpeg": ("8.0", "local", "D:/code/yt-release/tools/ffmpeg/x64/ffmpeg.exe"),
                    "FFprobe": ("8.0", "local", "D:/code/yt-release/tools/ffmpeg/x64/ffprobe.exe"),
                    "__video_encoders__": {
                        "items": ("libx264", "h264_nvenc", "hevc_nvenc"),
                        "error": "",
                    },
                }
            )
            self.assertEqual(page.runtime_version_labels["yt-dlp"].text(), "2026.08.25")
            self.assertEqual(page.runtime_version_labels["yt-dlp-ejs"].text(), "0.9.0")
            self.assertEqual(page.runtime_version_labels["Deno"].text(), "2.5.0")
            self.assertEqual(page.runtime_version_labels["FFmpeg"].text(), "8.0")
            self.assertEqual(page.runtime_version_labels["FFprobe"].text(), "8.0")
            self.assertNotIn("D:/code/yt-release", page.runtime_version_labels["FFmpeg"].toolTip())
            self.assertIn("tools\\ffmpeg\\x64\\ffmpeg.exe", page.runtime_version_labels["FFmpeg"].toolTip())
            self.assertEqual(
                [page.transcode_encoder.itemText(index) for index in range(page.transcode_encoder.count())],
                [
                    "Keep original encoding (no conversion)",
                    "x264 (H.264 / AVC, CPU)",
                    "NVIDIA NVENC H.264 (GPU)",
                    "NVIDIA NVENC HEVC (GPU)",
                ],
            )
            self.assertTrue(page.transcode_encoder.isEnabled())
            self.assertEqual(
                page.transcode_encoder_status.text(),
                "Found 3 encoders in the current FFmpeg; GPU entries are explicitly marked.",
            )
            self.assertEqual(page.runtime_update_buttons["Deno"].text(), "Check Update")
            self.assertNotIn("Browse", [button.text() for button in page.runtime_update_buttons.values()])
            deno_asset = {
                "name": "deno-x86_64-pc-windows-msvc.zip",
                "browser_download_url": "https://github.com/denoland/deno/releases/download/v2.6.0/deno-x86_64-pc-windows-msvc.zip",
            }
            ffmpeg_asset = {
                "name": "ffmpeg-master-latest-win64-gpl.zip",
                "browser_download_url": "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
            }
            page.runtime_component_updates.remote_versions_ready([
                {
                    "name": "Deno", "current": "2.5.0", "latest": "2.6.0",
                    "has_update": True, "auto_install_supported": True,
                    "assets": [deno_asset],
                },
                {
                    "name": "FFmpeg", "current": "8.0", "latest": "滚动版 2026-08-25",
                    "has_update": True, "auto_install_supported": True,
                    "assets": [ffmpeg_asset],
                },
            ])
            self.assertIn('<a href="update"', page.runtime_version_labels["Deno"].text())
            self.assertEqual(page.runtime_update_buttons["Deno"].text(), "Update")
            self.assertIn("downloadable update", page.runtime_version_labels["Deno"].toolTip())
            self.assertIn('<a href="update"', page.runtime_version_labels["FFprobe"].text())
            self.assertEqual(page.runtime_update_buttons["FFprobe"].text(), "Update")
            with patch("app.ui.main_window.QMessageBox.warning", return_value=QMessageBox.Yes):
                page.request_runtime_component_update("Deno")
            self.assertEqual(downloaded, [(deno_asset, "Deno")])
            self.assertEqual(page.ytdlp_ejs_source.currentData(), "auto")
            self.assertEqual(page.ytdlp_ejs_source.itemData(1), "npm")
            self.assertEqual(page.ytdlp_ejs_source.itemData(2), "github")
            self.assertEqual(page.ytdlp_ejs_source.itemData(3), "local")
            self.assertEqual(page.ffmpeg_build_channel.itemData(0), "latest")
            self.assertEqual(page.ffmpeg_build_channel.itemData(1), "nvenc_13_0")
            self.assertEqual(page.ffmpeg_build_channel.itemText(0), "Latest build (recommended)")
            self.assertIn("NVENC API 13.0", page.ffmpeg_build_channel.itemText(1))
            self.assertIn("2026-05-31", page.ffmpeg_build_channel.toolTip())
            group_save_buttons = [
                button
                for button in page.findChildren(QAbstractButton)
                if button.property("settingsGroupSave") is True
            ]
            self.assertEqual(len(group_save_buttons), 7)
            group_save_buttons[0].click()
            self.assertEqual(
                saved_settings,
                [(page, group_save_buttons[0].property("settingsScope"))],
            )
            with patch.object(page.runtime_component_updates, "refresh") as refresh_status:
                page.ffmpeg_build_channel.setCurrentIndex(
                    page.ffmpeg_build_channel.findData("nvenc_13_0")
                )
                self.app.processEvents()
            self.assertEqual(selected_ffmpeg_channels[-1], "nvenc_13_0")
            refresh_status.assert_called_once_with(force_remote=True)
            page.runtime_component_updates.remote_version_ready({
                "name": "FFmpeg",
                "ffmpeg_build_channel": "latest",
                "latest": "Rolling build 2026-08-25",
                "has_update": False,
            })
            self.assertNotIn("FFmpeg", page.runtime_component_updates.results)
            page.runtime_component_updates.remote_versions_ready([
                {
                    "name": "FFmpeg",
                    "ffmpeg_build_channel": "nvenc_13_0",
                    "current": "n9.0.1-6-g9d4ca21220-20260822",
                    "latest": "N-124716-g054dffd133-20260531",
                    "has_update": True,
                    "channel_switch_required": True,
                    "auto_install_supported": True,
                    "assets": [ffmpeg_asset],
                },
            ])
            self.assertEqual(page.runtime_update_buttons["FFmpeg"].text(), "Switch")
            self.assertEqual(page.runtime_update_buttons["FFprobe"].text(), "Switch")
            self.assertIn("does not match the selected build", page.runtime_version_labels["FFmpeg"].toolTip())
            self.assertFalse(page.max_concurrent.isEnabled())
            self.assertIn("logical processors", page.performance_summary.text())
            page.performance_mode.setCurrentIndex(page.performance_mode.findData("manual"))
            self.app.processEvents()
            self.assertTrue(page.max_concurrent.isEnabled())
            self.assertEqual(page.manual_download_performance_values(), (3, 12, 0.0))
        finally:
            page.close()
            self.app.processEvents()

    def test_runtime_component_titles_fit_the_windows_form_label_column(self) -> None:
        page = self._minimal_settings_page()
        try:
            page.resize(820, 700)
            page.show()
            self.app.processEvents()
            title = next(
                label
                for label in page.findChildren(QLabel)
                if label.text() == "yt-dlp Download Core"
            )
            available = title.parentWidget().width() - 4
            required = title.fontMetrics().horizontalAdvance(title.text())
            self.assertTrue(
                available >= required or title.wordWrap(),
                "runtime component title must fit or wrap instead of clipping",
            )
        finally:
            page.close()

    def test_runtime_batch_results_ignore_unnamed_stale_and_duplicate_rows(self) -> None:
        page = self._minimal_settings_page()
        try:
            page.runtime_component_updates.remote_versions_ready([
                {"error": "missing component name"},
                {"name": "Deno", "error": "temporary failure"},
                {
                    "name": "Deno",
                    "latest": "2.6.0",
                    "has_update": False,
                    "auto_install_supported": False,
                },
                {
                    "name": "FFmpeg",
                    "ffmpeg_build_channel": "nvenc_13_0",
                    "latest": "legacy",
                },
            ])

            self.assertEqual(set(page.runtime_component_updates.results), {"Deno"})
            self.assertIn("1 results, 0 failed", page.update_status.text())
        finally:
            page.close()

    def test_runtime_force_refresh_coalesces_while_check_is_running(self) -> None:
        page = self._minimal_settings_page()
        try:
            page.window.update_service.active_runtimes.add("check")
            with patch.object(page.local_core_versions, "refresh"):
                page.refresh_runtime_component_status(force_remote=True)
                page.refresh_runtime_component_status(force_remote=True)

            self.assertTrue(page.runtime_component_updates.remote_recheck_pending)
            self.assertTrue(page.runtime_component_updates.remote_checking)

            page.window.update_service.active_runtimes.discard("check")
            with patch.object(page.runtime_component_updates, "refresh") as refresh:
                page.runtime_component_updates._start_pending_recheck()

            self.assertFalse(page.runtime_component_updates.remote_recheck_pending)
            refresh.assert_called_once_with(force_remote=True)
        finally:
            page.close()
            self.app.processEvents()

    def test_download_group_save_does_not_validate_or_save_other_groups(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = _Settings(proxy="unchanged-proxy", cover_ai_model="unchanged-model")
            download_service = SimpleNamespace(
                max_concurrent=0,
                fragment_concurrent=0,
                request_delay=0.0,
                start_calls=0,
            )
            download_service._start_next = lambda: setattr(
                download_service,
                "start_calls",
                download_service.start_calls + 1,
            )
            def configure_performance(**values) -> None:
                download_service.max_concurrent = values["max_concurrent"]
                download_service.fragment_concurrent = values["fragment_concurrent"]
                download_service.request_delay = values["request_delay"]
                download_service.start_calls += 1
            download_service.configure_performance = configure_performance
            dashboard = SimpleNamespace(refresh_calls=0)
            dashboard.refresh_settings = lambda: setattr(
                dashboard,
                "refresh_calls",
                dashboard.refresh_calls + 1,
            )
            host = SimpleNamespace(
                app_settings=settings,
                download_service=download_service,
                dashboard=dashboard,
            )
            value = lambda item: SimpleNamespace(currentData=lambda: item)
            text = lambda item: SimpleNamespace(text=lambda: item)
            page = SimpleNamespace(
                download_dir=text(directory),
                processing_temp_dir=text(""),
                template=text("%(title)s.%(ext)s"),
                organize_task_folder=SimpleNamespace(isChecked=lambda: False),
                quality=value("best"),
                download_content_mode=value("audio"),
                download_container=value("mkv"),
                download_video_fps=value("best"),
                download_source_codec=value("auto"),
                download_vr_mode=value("any"),
                download_compatibility_target=value("auto"),
                download_audio_track=value("default"),
                download_options_json={
                    "content_mode": "video",
                    "container": "auto",
                    "audio_format": "flac",
                },
                transcode_encoder=value("original"),
                subtitle_language=value("none"),
                playlist_mode=value("auto"),
                performance_mode=value("manual"),
                manual_download_performance_values=lambda: (2, 6, 0.5),
                effective_download_performance_values=lambda: (2, 6, 0.5),
            )

            with patch(
                "app.ui.settings_save_controller.QMessageBox.information"
            ) as information:
                _settings_save_controller(host).save(page, "download")

            self.assertEqual(settings.get("proxy"), "unchanged-proxy")
            self.assertEqual(settings.get("cover_ai_model"), "unchanged-model")
            self.assertEqual(settings.get("max_concurrent"), "2")
            self.assertEqual(settings.get("fragment_concurrent"), "6")
            saved_options = json.loads(settings.get("download_options_json"))
            self.assertEqual(saved_options["content_mode"], "audio")
            self.assertEqual(saved_options["container"], "mkv")
            self.assertEqual(saved_options["audio_format"], "flac")
            self.assertEqual(settings.sync_count, 1)
            self.assertEqual(download_service.start_calls, 1)
            self.assertEqual(dashboard.refresh_calls, 1)
            self.assertEqual(information.call_args.args[2], "Download settings were saved.")

    def test_invalid_download_template_does_not_create_folder_or_write_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "should-not-be-created"
            settings = _Settings(filename_template="old-template")
            host = SimpleNamespace(app_settings=settings)
            page = SimpleNamespace(
                download_dir=SimpleNamespace(text=lambda: str(target)),
                template=SimpleNamespace(text=lambda: "../%(title)s.%(ext)s"),
            )

            with patch(
                "app.ui.settings_save_controller.QMessageBox.warning"
            ) as warning, patch(
                "app.ui.settings_save_controller.QMessageBox.information",
            ) as information:
                _settings_save_controller(host).save(page, "download")

            self.assertFalse(target.exists())
            self.assertEqual(settings.get("filename_template"), "old-template")
            self.assertEqual(settings.sync_count, 0)
            warning.assert_called_once()
            information.assert_not_called()

    def test_settings_directory_requires_a_successful_write_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "app.ui.settings_save_controller.tempfile.mkstemp",
            side_effect=PermissionError("access denied"),
        ), patch(
            "app.ui.settings_save_controller.QMessageBox.warning"
        ) as warning:
            path = _settings_save_controller(SimpleNamespace()).validated_directory(
                directory,
                error_template='The download folder cannot be used:\n{path}\n\n{error}',
            )

        self.assertIsNone(path)
        warning.assert_called_once()
        self.assertIn("access denied", warning.call_args.args[2])

    def test_failed_settings_batch_restores_secret_and_skips_runtime_apply(self) -> None:
        class FailingSettings(_Settings):
            def set_many(self, _values: dict[str, str]) -> dict[str, str]:
                raise OSError("simulated settings disk failure")

        class MemorySecureStore:
            def __init__(self) -> None:
                self.values = {"openai_api_key": "old-secret"}

            def get(self, key: str) -> str | None:
                return self.values.get(key)

            def set(self, key: str, value: str) -> None:
                self.values[key] = value

            def delete(self, key: str) -> None:
                self.values.pop(key, None)

        secure_store = MemorySecureStore()
        applied: list[bool] = []
        host = SimpleNamespace(
            app_settings=FailingSettings(cover_ai_model="old-model"),
            secure_store=secure_store,
        )
        plan = SettingsSavePlan(
            values={"cover_ai_model": "new-model"},
            success_message="saved",
            secret_updates=(("openai_api_key", "new-secret"),),
            after_commit=lambda: applied.append(True),
        )

        with patch(
            "app.ui.settings_save_controller.QMessageBox.warning"
        ) as warning, patch(
            "app.ui.settings_save_controller.QMessageBox.information",
        ) as information:
            saved = _settings_save_controller(host).commit(plan)

        self.assertFalse(saved)
        self.assertEqual(secure_store.get("openai_api_key"), "old-secret")
        self.assertEqual(applied, [])
        warning.assert_called_once()
        information.assert_not_called()

    def test_cover_settings_commit_secret_and_normalized_api_endpoint_together(self) -> None:
        class MemorySecureStore:
            def __init__(self) -> None:
                self.values: dict[str, str] = {}

            def get(self, key: str) -> str | None:
                return self.values.get(key)

            def set(self, key: str, value: str) -> None:
                self.values[key] = value

            def delete(self, key: str) -> None:
                self.values.pop(key, None)

        class SecretField:
            def __init__(self, value: str) -> None:
                self.value = value
                self.placeholder = ""

            def text(self) -> str:
                return self.value

            def clear(self) -> None:
                self.value = ""

            def setPlaceholderText(self, value: str) -> None:
                self.placeholder = value

        value = lambda item: SimpleNamespace(currentData=lambda: item)
        number = lambda item: SimpleNamespace(value=lambda: item)
        checked = lambda item: SimpleNamespace(isChecked=lambda: item)
        secret = SecretField("new-secret")
        page = SimpleNamespace(
            cover_ai_model=SimpleNamespace(text=lambda: "gpt-image-test"),
            cover_ai_api_url=SimpleNamespace(text=lambda: "http://127.0.0.1:9000/v1"),
            openai_key=secret,
            cover_preset=value("portrait_9_16"),
            cover_fit=value("crop"),
            cover_focus_x=number(35),
            cover_focus_y=number(65),
            cover_convert_jpeg=checked(True),
            cover_quality=number(88),
            prepend_cover_enabled=checked(True),
            prepend_cover_frames=number(4),
        )
        settings = _Settings()
        secure_store = MemorySecureStore()
        download_service = SimpleNamespace(
            cover_convert_jpeg=False,
            cover_jpeg_quality=0,
        )
        completed = SimpleNamespace(dirty=0)
        completed.mark_dirty = lambda: setattr(completed, "dirty", completed.dirty + 1)
        host = SimpleNamespace(
            app_settings=settings,
            secure_store=secure_store,
            download_service=download_service,
            completed=completed,
        )

        with patch("app.ui.settings_save_controller.QMessageBox.information"):
            _settings_save_controller(host).save(page, "cover")

        self.assertEqual(secure_store.get("openai_api_key"), "new-secret")
        self.assertEqual(
            settings.get("cover_ai_api_url"),
            "http://127.0.0.1:9000/v1/images/edits",
        )
        self.assertEqual(settings.get("cover_jpeg_quality"), "88")
        self.assertTrue(download_service.cover_convert_jpeg)
        self.assertEqual(download_service.cover_jpeg_quality, 88)
        self.assertEqual(completed.dirty, 1)
        self.assertEqual(secret.value, "")
        self.assertTrue(secret.placeholder)

    def test_update_settings_store_canonical_repository_before_applying_routes(self) -> None:
        value = lambda item: SimpleNamespace(currentData=lambda: item)
        checked = lambda item: SimpleNamespace(isChecked=lambda: item)
        page = SimpleNamespace(
            update_repo=SimpleNamespace(text=lambda: "yt-dlp/yt-dlp.git"),
            auto_check_updates=checked(True),
            update_prerelease=checked(False),
            github_download_route=value("auto"),
            github_mirror_urls="",
            github_route_profiles="{}",
        )
        calls: list[tuple[str, str, str]] = []
        update_service = SimpleNamespace(
            set_download_routes=lambda route, mirrors, profiles: calls.append(
                (route, mirrors, profiles)
            ),
        )
        settings = _Settings()
        host = SimpleNamespace(
            app_settings=settings,
            update_service=update_service,
            application_updates_supported=False,
        )

        with patch("app.ui.settings_save_controller.QMessageBox.information"):
            _settings_save_controller(host).save(page, "updates")

        self.assertEqual(
            settings.get("update_repo"),
            "https://github.com/yt-dlp/yt-dlp",
        )
        self.assertEqual(calls, [("auto", "", "{}")])
        self.assertEqual(settings.sync_count, 1)

    def test_clearing_update_repository_detaches_previous_runtime_updater(self) -> None:
        value = lambda item: SimpleNamespace(currentData=lambda: item)
        checked = lambda item: SimpleNamespace(isChecked=lambda: item)
        page = SimpleNamespace(
            update_repo=SimpleNamespace(text=lambda: ""),
            auto_check_updates=checked(True),
            update_prerelease=checked(False),
            github_download_route=value("auto"),
            github_mirror_urls="",
            github_route_profiles="{}",
        )
        cleared: list[bool] = []
        host = SimpleNamespace(
            app_settings=_Settings(update_repo="https://github.com/old/project"),
            update_service=SimpleNamespace(
                set_download_routes=lambda *_args: None,
            ),
            application_update_service=SimpleNamespace(
                clear_configuration=lambda: cleared.append(True),
            ),
            application_updates_supported=True,
        )

        with patch("app.ui.settings_save_controller.QMessageBox.information"):
            saved = _settings_save_controller(host).save(page, "updates")

        self.assertTrue(saved)
        self.assertEqual(host.app_settings.get("update_repo"), "")
        self.assertEqual(cleared, [True])

    def test_compact_runtime_path_field_preserves_real_path_without_displaying_it(self) -> None:
        full_path = "D:/code/yt-release/tools/ffmpeg/x64/ffmpeg.exe"
        field = CompactPathLineEdit(full_path)
        other = QWidget()
        try:
            self.assertEqual(field.text(), full_path)
            self.assertEqual(field.displayText(), "tools\\ffmpeg\\x64\\ffmpeg.exe")
            self.assertEqual(compact_path_display("D:/custom/runtime/deno.exe"), "…\\runtime\\deno.exe")
            field.show()
            field.setFocus()
            self.app.processEvents()
            self.assertEqual(
                field.displayText(),
                native_path_display(full_path),
            )
            self.assertEqual(field.text(), full_path)
        finally:
            field.close()
            other.close()

    def test_portable_path_field_uses_native_separators_without_changing_storage(self) -> None:
        stored = "data/browser/sau-cookies"
        field = PortablePathLineEdit(stored)
        try:
            self.assertEqual(field.text(), stored)
            self.assertEqual(field.displayText(), native_path_display(stored))
            field.setText("tools/ffmpeg/x64/ffprobe.exe")
            self.assertEqual(field.text(), "tools/ffmpeg/x64/ffprobe.exe")
            self.assertEqual(
                field.displayText(),
                native_path_display("tools/ffmpeg/x64/ffprobe.exe"),
            )
        finally:
            field.close()


if __name__ == "__main__":
    unittest.main()
