from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, QThread, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.core.cookie_sources import (
    COOKIE_SOURCE_EMBEDDED,
    COOKIE_SOURCE_NONE,
    EMBEDDED_DOWNLOAD_PROFILE,
)
from app.core.collection_service import CollectionProbeRequest
from app.core.download_links import extract_download_links, normalize_download_link
from app.core.download_options import DownloadOptions
from app.core.download_performance import (
    effective_download_performance,
    smart_download_performance,
)
from app.core.download_service import DownloadService, DownloadTask
from app.storage.database import Database
from app.storage.models import MediaItem
from app.ui.download_control_presentation import DOWNLOAD_QUALITY_VALUES
from app.ui.download_cookie_controller import DownloadCookieController
from app.ui.download_dialogs import FormatSelectionDialog
from app.ui.task_card import (
    DownloadTaskCard,
    INDETERMINATE_TASK_STAGES,
    TASK_CARD_HEIGHT,
)
from app.ui.navigation import (
    SidebarNavigation,
    configure_main_navigation,
    navigation_icon_key,
)
from app.ui.dashboard_page import DashboardPage, reveal_file_or_folder
from app.ui.i18n import text as ui_text
from app.ui.widget_behavior import ExplicitWheelFocusGuard
from app.ui.task_context_menu import task_menu_capabilities
from app.ui.task_list import ordered_top_level_tasks


class _Settings:
    def __init__(self, **overrides: str) -> None:
        self.values = {
            "download_dir": "D:/downloads",
            "quality": "best",
            "playlist_mode": "auto",
            "download_performance_mode": "manual",
            "max_concurrent": "3",
            "fragment_concurrent": "12",
            "request_delay": "0",
            "download_cookie_source": COOKIE_SOURCE_NONE,
        }
        self.values.update(overrides)
        self.sync_count = 0

    def get(self, key: str) -> str:
        return self.values.get(key, "")

    def set(self, key: str, value: str) -> None:
        self.values[key] = str(value)

    def set_many(self, values: dict[str, str]) -> dict[str, str]:
        normalized = {str(key): str(value) for key, value in values.items()}
        self.values.update(normalized)
        self.sync_count += 1
        return normalized

    def get_resolved_path(self, key: str) -> str:
        return self.values.get(key, "")

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = str(self.values.get(key, "")).strip().casefold()
        return value in {"1", "true", "yes", "on"} if value else default

    def get_int(
        self,
        key: str,
        default: int,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        try:
            value = int(self.values.get(key, default))
        except (TypeError, ValueError):
            value = default
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def sync(self) -> None:
        self.sync_count += 1


class _QuickSettingsPage:
    def __init__(self) -> None:
        self.download_content_mode = QComboBox()
        self.quality = QComboBox()
        self.download_container = QComboBox()
        self.download_audio_track = QComboBox()
        self.download_video_fps = QComboBox()
        self.download_source_codec = QComboBox()
        self.download_vr_mode = QComboBox()
        self.subtitle_language = QComboBox()
        self.download_options_json: dict[str, object] = {}

    def update_download_format_controls(self) -> None:
        pass


class _DownloadService:
    def __init__(self) -> None:
        self.tasks: dict[str, DownloadTask] = {}
        self.workers: dict[str, object] = {}
        self.ytdlp_core_mode = "auto"
        self.deno_path = ""
        self.ytdlp_ejs_source = "auto"
        self.enqueued: list[dict[str, object]] = []
        self.resumed: list[str] = []
        self.retried: list[str] = []
        self.redownloaded: list[tuple[str, str | None]] = []
        self.converted: list[dict[str, str]] = []
        self.probe_updates: list[dict[str, object]] = []

    find_active_duplicate = DownloadService.find_active_duplicate

    def task_statistics(self, *, top_level_only: bool = False) -> dict[str, int]:
        tasks = [
            task for task in self.tasks.values()
            if not top_level_only or not task.parent_task_id
        ]
        resumable_paused = sum(task.status == "paused" for task in tasks)
        paused = resumable_paused + sum(
            task.status == "暂停中" for task in tasks
        )
        failed = sum(
            task.status in {"failed", "partial_failed", "canceled"}
            for task in tasks
        )
        completed = sum(task.status == "completed" for task in tasks)
        return {
            "total": len(tasks),
            "active": sum(task.status in {"downloading", "parsing_collection", "canceling"} for task in tasks),
            "queued": sum(task.status == "queued" for task in tasks),
            "paused": paused,
            "processing": sum(task.status == "processing" for task in tasks),
            "completed": completed,
            "failed": failed,
            "pausable": sum(task.status in {"downloading", "queued"} for task in tasks),
            "resumable": resumable_paused + failed,
            "cleanable": completed,
        }

    def enqueue(self, url: str, output_dir: str, proxy: str = "", cookie_file: str = "", **options) -> str:
        task_id = f"new-{len(self.enqueued) + 1}"
        self.enqueued.append({
            "id": task_id,
            "url": url,
            "output_dir": output_dir,
            "proxy": proxy,
            "cookie_file": cookie_file,
            **options,
        })
        return task_id

    def create_collection(self, url: str, output_dir: str, **options) -> str:
        task_id = f"collection-{len(self.tasks) + 1}"
        task = DownloadTask(
            task_id,
            url,
            output_dir,
            task_kind="collection",
            parent_task_id=str(options.get("parent_task_id") or ""),
            root_task_id=str(options.get("root_task_id") or task_id),
            collection_index=int(options.get("collection_index") or 0),
            title=str(options.get("title") or "正在解析合集"),
            status="parsing_collection",
            stage="parsing_collection",
            stage_text="正在解析合集",
        )
        self.tasks[task_id] = task
        return task_id

    def cancel(self, _task_id: str) -> None:
        pass

    def delete_task(self, task_id: str, _delete_files: bool = False) -> bool:
        return self.tasks.pop(task_id, None) is not None

    def pause(self, _task_id: str) -> None:
        pass

    def resume(self, task_id: str) -> None:
        self.resumed.append(task_id)

    def retry(self, task_id: str) -> None:
        self.retried.append(task_id)

    def redownload(
        self,
        task_id: str,
        quality_override: str | None = None,
    ) -> str:
        self.redownloaded.append((task_id, quality_override))
        return f"redownload-{task_id}"

    def collection_children(self, task_id: str) -> list[DownloadTask]:
        return [task for task in self.tasks.values() if task.parent_task_id == task_id]

    def update_collection_probe(self, task_id: str, **values) -> None:
        task = self.tasks[task_id]
        task.status = "waiting_selection" if values.get("finished") else "parsing_collection"
        task.stage = task.status
        task.stage_text = "等待选择下载项目" if values.get("finished") else "正在解析合集"
        self.probe_updates.append({"task_id": task_id, **values})

    def fail_collection_probe(self, task_id: str, error: str) -> bool:
        task = self.tasks.get(task_id)
        if task is None or task.task_kind != "collection":
            return False
        task.status = "failed"
        task.error = error
        task.stage = "failed"
        task.stage_text = error
        self.probe_updates.append({
            "task_id": task_id,
            "failed": True,
            "error": error,
        })
        return True

    def start_task(self, _task_id: str) -> None:
        pass

    def convert_completed_task(
        self,
        task_id: str,
        encoder: str,
        *,
        ffmpeg_path: str = "",
        ffprobe_path: str = "",
    ) -> bool:
        self.converted.append({
            "task_id": task_id,
            "encoder": encoder,
            "ffmpeg_path": ffmpeg_path,
            "ffprobe_path": ffprobe_path,
        })
        return True


class DashboardOverviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        existing = QCoreApplication.instance()
        if existing is not None and not isinstance(existing, QApplication):
            raise unittest.SkipTest("a non-GUI QCoreApplication already exists")
        cls.app = existing or QApplication([])

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.service = _DownloadService()
        self.window = SimpleNamespace(
            app_settings=_Settings(),
            download_service=self.service,
            db=SimpleNamespace(path=Path(self.temp_dir.name) / "app.db"),
            tabs=SimpleNamespace(setCurrentWidget=lambda _widget: None),
            settings=_QuickSettingsPage(),
        )
        self.page = DashboardPage(self.window)
        self.page.resize(1280, 760)
        self.page.show()
        self.addCleanup(self._close_page)

    def _close_page(self) -> None:
        self.page.close()
        self.app.processEvents()
        # QWidget.deleteLater() posts DeferredDelete events that processEvents
        # alone does not guarantee to drain. Leaving dozens of task cards from
        # each test queued until a later threaded Qt test made the combined
        # suite intermittently crash inside unrelated event processing.
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.app.processEvents()

    def test_metric_cards_show_live_counts_and_filter_with_one_click(self) -> None:
        statuses = (
            ("active", "downloading"),
            ("queued", "queued"),
            ("paused", "paused"),
            ("done", "completed"),
            ("failed", "failed"),
            ("cancelled", "canceled"),
        )
        for task_id, status in statuses:
            task = DownloadTask(task_id, f"https://example.com/{task_id}", "D:/downloads", title=task_id, status=status)
            self.service.tasks[task_id] = task
            self.page.add_task(task)
        self.page.task_restore.set_loaded()
        self.app.processEvents()

        expected = {
            "全部": "6",
            "下载中": "1",
            "排队中": "1",
            "已暂停": "1",
            "已完成": "1",
            "失败": "2",
        }
        self.assertEqual(
            {name: card.value.text() for name, card in self.page.metric_cards.items()},
            expected,
        )
        self.assertEqual(self.page.count_label.text(), "共 6 个任务")

        QTest.mouseClick(self.page.metric_cards["失败"], Qt.LeftButton)
        self.app.processEvents()

        self.assertEqual(self.page.filter_box.currentText(), "失败")
        self.assertTrue(bool(self.page.metric_cards["失败"].property("active")))
        visible_ids = {
            task_id
            for task_id, item in self.page.items.items()
            if not item.isHidden()
        }
        self.assertEqual(visible_ids, {"failed", "cancelled"})

    def test_dashboard_has_only_persistent_quick_download_selectors(self) -> None:
        self.assertFalse(hasattr(self.page, "_smart_settings_button"))
        self.assertFalse(hasattr(self.page, "_smart_options_button"))
        self.assertEqual(self.page.task_content_mode.findData(""), -1)
        self.assertEqual(self.page.task_quality.findData(""), -1)
        self.assertEqual(self.page.task_container.findData(""), -1)
        self.assertEqual(self.page.task_subtitle_language.findData(""), -1)
        self.assertEqual(self.page.task_audio_track.findData(""), -1)
        self.assertEqual(
            [
                self.page.task_content_mode.itemData(index)
                for index in range(self.page.task_content_mode.count())
            ],
            ["manual", "video", "audio"],
        )
        self.assertGreaterEqual(self.page.task_subtitle_language.findData("all"), 0)
        self.assertGreaterEqual(self.page.task_audio_track.findData("original"), 0)
        self.assertGreaterEqual(self.page.task_audio_track.findData("all"), 0)

    def test_task_list_uses_smooth_fixed_height_scrolling(self) -> None:
        self.assertEqual(
            self.page.task_list.verticalScrollMode(),
            QAbstractItemView.ScrollPerPixel,
        )
        self.assertTrue(self.page.task_list.uniformItemSizes())

    def test_dashboard_toolbars_reflow_without_overlapping_controls(self) -> None:
        for width, height in ((680, 570), (900, 620), (1280, 760), (1536, 760)):
            self.page.resize(width, height)
            self.app.processEvents()
            controls = [
                self.page._input_sites_button,
                self.page._input_paste_button,
                self.page._input_add_button,
                self.page.task_download_menu,
                self.page.task_quality_menu,
                self.page.task_container,
                self.page.search_box,
                self.page.sort_box,
                self.page.filter_box,
                self.page._open_download_dir_button,
                self.page.pause_all_button,
                self.page.resume_all_button,
                self.page.log_button,
                self.page.cleanup_button,
            ]
            for index, first in enumerate(controls):
                if not first.isVisible():
                    continue
                for second in controls[index + 1:]:
                    if second.isVisible() and first.parentWidget() is second.parentWidget():
                        self.assertFalse(
                            first.geometry().intersects(second.geometry()),
                            msg=f"overlap at width {width}: {first.objectName()} / {second.objectName()}",
                        )

            smart_children = (
                self.page.smart_mode_badge,
                self.page.smart_mode_summary,
                self.page._smart_content_label,
                self.page.task_download_menu,
                self.page._smart_quality_label,
                self.page.task_quality_menu,
                self.page._smart_format_label,
                self.page.task_container,
            )
            bar_rect = self.page._smart_mode_bar.contentsRect()
            for child in smart_children:
                self.assertTrue(
                    bar_rect.contains(child.geometry()),
                    msg=f"smart control clipped at {width}x{height}: {child.objectName()}",
                )
            if width >= 1040:
                self.assertGreaterEqual(
                    self.page.smart_mode_summary.width(),
                    180,
                    msg=f"smart summary collapsed at width {width}",
                )

    def test_resize_only_reflows_cached_labels_without_reloading_settings(self) -> None:
        controller = self.page.quick_download_settings
        with (
            patch.object(
                self.window.app_settings,
                "get",
                wraps=self.window.app_settings.get,
            ) as get_setting,
            patch.object(controller, "refresh", wraps=controller.refresh) as refresh,
            patch.object(
                controller,
                "refresh_elided_text",
                wraps=controller.refresh_elided_text,
            ) as refresh_elided_text,
        ):
            self.page.resize(900, 620)
            self.app.processEvents()

        refresh.assert_not_called()
        get_setting.assert_not_called()
        self.assertGreaterEqual(refresh_elided_text.call_count, 1)

    def test_empty_state_replaces_the_blank_task_list_panel(self) -> None:
        self.page.task_restore.set_loaded()
        self.app.processEvents()

        self.assertIs(self.page.task_content_stack.currentWidget(), self.page.empty_label)
        self.assertFalse(self.page.task_list.isVisible())

    def test_quick_download_selectors_have_distinct_compact_icons(self) -> None:
        self.assertEqual(self.page.task_download_menu.compactIconKey(), "content")
        self.assertEqual(self.page.task_quality_menu.compactIconKey(), "quality")
        self.assertEqual(self.page.task_container.compactIconKey(), "format")

    def test_compact_download_menu_contains_content_subtitle_and_audio_track_choices(self) -> None:
        self.assertTrue(self.page.task_content_mode.isHidden())
        self.assertTrue(self.page.task_subtitle_language.isHidden())
        self.assertTrue(self.page.task_audio_track.isHidden())
        self.assertEqual(
            set(self.page._download_content_actions),
            {'manual', 'video', 'audio'},
        )
        self.assertIn('all', self.page._download_subtitle_actions)
        self.assertIn('original', self.page._download_audio_track_actions)
        self.assertIn('all', self.page._download_audio_track_actions)

        self.page._download_subtitle_actions['ja'].trigger()
        self.page._download_audio_track_actions['original'].trigger()
        self.page._download_content_actions['audio'].trigger()

        self.assertEqual(self.page.task_subtitle_language.currentData(), 'ja')
        self.assertEqual(self.page.task_audio_track.currentData(), 'original')
        self.assertEqual(self.page.task_content_mode.currentData(), 'audio')
        self.assertEqual(self.page.task_download_menu.text(), '音频')
        self.assertTrue(self.page._download_subtitle_actions['ja'].isChecked())
        self.assertTrue(self.page._download_audio_track_actions['original'].isChecked())

    def test_quality_menu_contains_frame_rate_codec_and_vr_submenus(self) -> None:
        self.assertTrue(self.page.task_quality.isHidden())
        self.assertTrue(self.page.task_video_fps.isHidden())
        self.assertTrue(self.page.task_source_codec.isHidden())
        self.assertTrue(self.page.task_vr_mode.isHidden())
        self.assertEqual(
            list(self.page._quality_fps_actions),
            ['best', '240', '120', '60', '50', '30', '25', '24'],
        )
        self.assertEqual(
            set(self.page._quality_codec_actions),
            {'auto', 'h264', 'h265', 'av1', 'vp9'},
        )
        self.assertEqual(
            set(self.page._quality_vr_actions),
            {'any', '2d360', '3d180', '3d360', 'none'},
        )

        self.page._quality_actions['4k'].trigger()
        self.page._quality_fps_actions['120'].trigger()
        self.page._quality_codec_actions['av1'].trigger()
        self.page._quality_vr_actions['3d180'].trigger()

        self.assertEqual(self.page.task_quality.currentData(), '4k')
        self.assertEqual(self.page.task_video_fps.currentData(), '120')
        self.assertEqual(self.page.task_source_codec.currentData(), 'av1')
        self.assertEqual(self.page.task_vr_mode.currentData(), '3d180')
        self.assertEqual(self.page.task_quality_menu.text(), '4K (2160p)')

    def test_task_controls_keep_the_requested_two_row_layout_at_minimum_width(self) -> None:
        self.page.resize(680, 620)
        self.app.processEvents()

        self.assertLessEqual(
            abs(self.page._input_layout.geometry().right() - self.page._input_add_button.geometry().right()),
            12,
        )
        filter_row_top = self.page._filter_tasks_label.geometry().top()
        for control in (
            self.page.search_box,
            self.page._filter_sort_label,
            self.page.sort_box,
            self.page.filter_box,
        ):
            self.assertEqual(control.geometry().top(), filter_row_top)

        action_row_top = self.page.download_dir_hint.geometry().top()
        for control in (
            self.page._open_download_dir_button,
            self.page.pause_all_button,
            self.page.resume_all_button,
            self.page.log_button,
            self.page.cleanup_button,
        ):
            self.assertEqual(control.geometry().top(), action_row_top)
        self.assertGreater(action_row_top, filter_row_top)

    def test_progress_update_only_repaints_the_changed_task_card(self) -> None:
        task = DownloadTask(
            "progress-only",
            "https://example.com/progress-only",
            "D:/downloads",
            title="Progress only",
            status="downloading",
            progress=10.0,
        )
        self.service.tasks[task.id] = task
        self.page.add_task(task)
        self.page.task_restore.set_loaded()

        task.progress = 20.0
        with patch.object(
            self.service,
            "task_statistics",
            wraps=self.service.task_statistics,
        ) as statistics:
            self.page.update_progress(task.id, {"downloaded_bytes": 20})

        statistics.assert_not_called()
        self.assertEqual(self.page.cards[task.id]._task.progress, 20.0)

    def test_card_task_list_pages_large_history_without_materializing_every_row(self) -> None:
        tasks = [
            DownloadTask(
                f"task-{index:05d}",
                f"https://example.com/{index}",
                "D:/downloads",
                title=f"Video {index:05d}",
                status="completed" if index % 3 == 0 else "queued",
                created_at=f"2026-08-25T{index // 3600 % 24:02d}:{index // 60 % 60:02d}:{index % 60:02d}",
            )
            for index in range(10_000)
        ]
        self.service.tasks = {task.id: task for task in tasks}
        with patch(
            "app.ui.task_list.TaskListPagingState.materialized_row",
            side_effect=AssertionError("canonical restore must append rows"),
        ), patch(
            "app.ui.task_list_restore.TaskListRestoreController.remaining_count",
            side_effect=AssertionError("filtering already updates the load button"),
        ), patch.object(
            self.page.task_presentation,
            "filter_values",
            wraps=self.page.task_presentation.filter_values,
        ) as filter_values:
            self.page.begin_task_restore(tasks)
            while self.page._task_render_timer.isActive():
                self.app.processEvents()
        self.assertEqual(filter_values.call_count, 2)

        self.assertEqual(len(self.service.tasks), 10_000)
        self.assertEqual(self.page.task_list.count(), 50)
        self.assertEqual(len(self.page.cards), 50)
        self.assertEqual(len(self.page.task_list.findChildren(DownloadTaskCard)), 50)
        self.assertTrue(all(
            self.page.task_list.item(index).sizeHint().height() == TASK_CARD_HEIGHT
            for index in range(self.page.task_list.count())
        ))
        self.assertTrue(self.page.load_more_button.isVisible())

        self.page.search_box.setText("task-05000")
        QTest.qWait(320)
        for _ in range(50):
            self.app.processEvents()
            if "task-05000" in self.page.items and not self.page._task_render_timer.isActive():
                break
            QTest.qWait(10)
        self.assertIn("task-05000", self.page.items)
        self.assertFalse(self.page.items["task-05000"].isHidden())
        self.assertEqual(
            [task_id for task_id, item in self.page.items.items() if not item.isHidden()],
            ["task-05000"],
        )
        self.page.search_box.clear()
        self.page.filter_box.setCurrentIndex(self.page.filter_box.findData("已完成"))
        self.app.processEvents()
        self.assertTrue(all(
            self.service.tasks[task_id].status == "completed"
            for task_id, item in self.page.items.items()
            if not item.isHidden()
        ))

        self.page.filter_box.setCurrentIndex(self.page.filter_box.findData("全部"))
        self.page.sort_box.setCurrentIndex(self.page.sort_box.findData("oldest"))
        self.app.processEvents()
        self.assertEqual(self.page.task_list.item(0).data(Qt.UserRole), "task-00000")
        before = self.page.task_list.count()
        with patch(
            "app.ui.task_list.TaskListPagingState.materialized_row",
            side_effect=AssertionError("canonical page must append rows"),
        ):
            self.page.task_restore.load_more()
            while self.page._task_render_timer.isActive():
                self.app.processEvents()
        self.assertGreater(self.page.task_list.count(), before)
        self.assertLessEqual(self.page.task_list.count(), before + 50)
        self.page.remove_task("task-05000")
        self.assertNotIn("task-05000", self.page.items)
        self.assertNotIn("task-05000", self.page.task_paging.ordered_ids)

    def test_new_task_during_partial_restore_keeps_canonical_visual_order(self) -> None:
        tasks = [
            DownloadTask(
                f"restore-{index:02d}",
                f"https://example.com/restore/{index}",
                "D:/downloads",
                title=f"Restore {index:02d}",
                status="completed",
                created_at=f"{index:04d}",
            )
            for index in range(60)
        ]
        self.service.tasks = {task.id: task for task in tasks}
        self.page.sort_box.setCurrentIndex(self.page.sort_box.findData("oldest"))
        self.page.begin_task_restore(tasks)
        self.page._task_render_timer.stop()
        self.page.task_restore.render_batch()
        self.assertEqual(self.page.task_list.count(), 8)

        inserted = DownloadTask(
            "restore-inserted",
            "https://example.com/restore/inserted",
            "D:/downloads",
            title="Restore inserted",
            status="queued",
            created_at="0030.5",
        )
        self.service.tasks[inserted.id] = inserted
        self.page.add_task(inserted)

        while self.page.task_paging.loading:
            self.page.task_restore.render_batch()

        materialized = [
            self.page.task_list.item(index).data(Qt.UserRole)
            for index in range(self.page.task_list.count())
        ]
        self.assertEqual(
            materialized,
            self.page.task_paging.ordered_ids[:self.page.task_rows.page_size],
        )

    def test_card_task_list_selection_action_context_and_double_click_map_correct_task(self) -> None:
        tasks = [
            DownloadTask("queued", "https://example.com/q", "D:/downloads", title="Queued", status="queued"),
            DownloadTask("done", "https://example.com/d", "D:/downloads", title="Done", status="completed"),
            DownloadTask(
                "collection", "https://example.com/list", "D:/downloads",
                title="Collection", status="completed", task_kind="collection",
            ),
        ]
        self.service.tasks = {task.id: task for task in tasks}
        self.service.collection_children = lambda _task_id: [object()]
        self.page.begin_task_restore(tasks)
        self.app.processEvents()

        queued_item = self.page.items["queued"]
        done_item = self.page.items["done"]
        queued_item.setSelected(True)
        done_item.setSelected(True)
        self.assertEqual(set(self.page.selected_task_ids()), {"queued", "done"})

        with patch.object(self.page, "cancel_task") as cancel:
            QTest.mouseClick(self.page.cards["queued"].action, Qt.LeftButton)
            self.app.processEvents()
            cancel.assert_called_once_with("queued")

        with patch.object(self.page, "show_task_menu") as show_menu:
            self.page.task_context_menu(self.page.task_list.visualItemRect(done_item).center())
            show_menu.assert_called_once()
            self.assertEqual(show_menu.call_args.args[0], "done")

        collection_item = self.page.items["collection"]
        with patch.object(self.page, "_open_collection_detail") as open_detail:
            self.page._task_double_clicked(collection_item)
            open_detail.assert_called_once_with("collection")

    def test_metric_cards_are_keyboard_accessible(self) -> None:
        card = self.page.metric_cards["已完成"]
        card.setFocus()
        QTest.keyClick(card, Qt.Key_Space)
        self.app.processEvents()

        self.assertEqual(self.page.filter_box.currentText(), "已完成")
        self.assertIn("点击筛选", card.accessibleDescription())

    def test_task_presentation_refresh_uses_one_statistics_snapshot(self) -> None:
        task = DownloadTask(
            "single-snapshot",
            "https://example.com/single-snapshot",
            "D:/downloads",
            status="queued",
        )
        self.service.tasks[task.id] = task
        self.page.add_task(task)
        self.page.task_restore.set_loaded()

        with patch.object(
            self.service,
            "task_statistics",
            wraps=self.service.task_statistics,
        ) as statistics:
            self.page.apply_filter()

        statistics.assert_called_once_with(top_level_only=True)
        self.assertTrue(self.page.pause_all_button.isEnabled())
        self.assertFalse(self.page.resume_all_button.isEnabled())
        self.assertFalse(self.page.cleanup_button.isEnabled())

    def test_filter_deselects_hidden_task_and_disables_log_action(self) -> None:
        queued = DownloadTask(
            "selected-queued",
            "https://example.com/selected-queued",
            "D:/downloads",
            status="queued",
        )
        completed = DownloadTask(
            "visible-completed",
            "https://example.com/visible-completed",
            "D:/downloads",
            status="completed",
        )
        self.service.tasks = {
            queued.id: queued,
            completed.id: completed,
        }
        with patch(
            "app.ui.task_rows.ordered_top_level_tasks",
            wraps=ordered_top_level_tasks,
        ) as order_tasks:
            self.page.add_tasks([queued, completed])
        order_tasks.assert_called_once()
        self.page.task_restore.set_loaded()
        self.page.items[queued.id].setSelected(True)
        self.page.sync_selection()
        self.assertTrue(self.page.log_button.isEnabled())

        self.page.filter_box.setCurrentIndex(
            self.page.filter_box.findData("已完成")
        )
        self.page.apply_filter()

        self.assertEqual(self.page.selected_task_ids(), [])
        self.assertFalse(self.page.log_button.isEnabled())
        self.assertFalse(self.page.cards[queued.id].property("selected"))
        self.assertTrue(self.page.items[queued.id].isHidden())
        self.assertFalse(self.page.items[completed.id].isHidden())

    def test_filter_without_metric_card_leaves_all_metrics_inactive(self) -> None:
        for filter_name in ("处理中", "文件已删除"):
            with self.subTest(filter_name=filter_name):
                self.page.filter_box.setCurrentIndex(
                    self.page.filter_box.findData(filter_name)
                )
                self.page.apply_filter()
                self.assertFalse(any(
                    bool(metric.property("active"))
                    for metric in self.page.metric_cards.values()
                ))

    def test_pausing_task_is_counted_as_paused_but_not_yet_resumable(self) -> None:
        task = DownloadTask(
            "pausing-only",
            "https://example.com/pausing-only",
            "D:/downloads",
            status="暂停中",
        )
        self.service.tasks[task.id] = task
        self.page.add_task(task)
        self.page.task_restore.set_loaded()

        self.assertEqual(self.page.metric_cards["已暂停"].value.text(), "1")
        self.assertFalse(self.page.resume_all_button.isEnabled())

    def test_download_link_extraction_normalizes_clipboard_wrappers_and_deduplicates(self) -> None:
        text = (
            "[视频一](https://example.com/watch?v=1)\n"
            "<https://example.com/watch?v=2>；\n"
            "重复：https://example.com/watch?v=1。\n"
            "带括号：https://example.com/title_(demo)"
        )

        self.assertEqual(
            extract_download_links(text),
            [
                "https://example.com/watch?v=1",
                "https://example.com/watch?v=2",
                "https://example.com/title_(demo)",
            ],
        )
        self.assertEqual(
            normalize_download_link("[标题](https://example.com/video)"),
            "https://example.com/video",
        )
        self.assertEqual(normalize_download_link("不是链接"), "")

    def test_smart_mode_summary_is_read_only_and_explains_current_settings(self) -> None:
        self.page.refresh_settings()

        tooltip = self.page.smart_mode_summary.toolTip()
        self.assertIn("最高画质", tooltip)
        self.assertIn("自动识别列表", tooltip)
        self.assertIn("3 个任务并行", tooltip)
        self.assertIn("单任务 12 路分片", tooltip)
        self.assertIn("手动性能参数", tooltip)
        self.assertEqual(self.page.url.accessibleName(), "视频或播放列表链接")

    def test_invalid_saved_quality_falls_back_to_best_instead_of_manual(self) -> None:
        self.window.app_settings.values["quality"] = "not-a-quality"

        self.page.refresh_settings()

        self.assertEqual(self.page.task_quality.currentData(), "best")
        self.assertEqual(
            self.page.task_quality_menu.text(),
            self.page.task_quality.currentText(),
        )

    def test_summary_reports_embedded_cookie_source_without_cookie_file(self) -> None:
        self.window.app_settings.values.update({
            "download_cookie_source": COOKIE_SOURCE_EMBEDDED,
            "download_cookie_file": "",
        })

        self.page.refresh_settings()

        self.assertIn(
            ui_text("Cookie configured"),
            self.page.smart_mode_summary.toolTip(),
        )

    def test_quick_download_choices_persist_and_remain_after_submit(self) -> None:
        self.window.app_settings.values["playlist_mode"] = "single"
        self.window.app_settings.values["download_options_json"] = (
            '{"content_mode":"audio","container":"auto","audio_format":"flac"}'
        )
        self.page.refresh_settings()
        self.page.task_content_mode.setCurrentIndex(
            self.page.task_content_mode.findData("video")
        )
        self.page.task_container.setCurrentIndex(
            self.page.task_container.findData("mkv")
        )
        self.page.task_quality.setCurrentIndex(
            self.page.task_quality.findData("custom")
        )
        self.page.task_subtitle_language.setCurrentIndex(
            self.page.task_subtitle_language.findData("ja")
        )
        self.page.task_audio_track.setCurrentIndex(
            self.page.task_audio_track.findData("en")
        )
        self.page.task_video_fps.setCurrentIndex(
            self.page.task_video_fps.findData("120")
        )
        self.page.task_source_codec.setCurrentIndex(
            self.page.task_source_codec.findData("av1")
        )
        self.page.task_vr_mode.setCurrentIndex(
            self.page.task_vr_mode.findData("3d180")
        )

        options = self.page.quick_download_settings.global_options()
        self.assertEqual(options["content_mode"], "video")
        self.assertEqual(options["container"], "mkv")
        self.assertEqual(options["audio_format"], "flac")
        self.assertEqual(options["audio_track"], "en")
        self.assertEqual(options["video_fps"], "120")
        self.assertEqual(options["source_video_codec"], "av1")
        self.assertEqual(options["vr_mode"], "3d180")
        self.assertEqual(self.window.app_settings.get("subtitle_language"), "ja")
        self.assertEqual(self.window.app_settings.get("quality"), "custom")
        self.assertGreaterEqual(self.window.app_settings.sync_count, 3)

        self.page.url.setText("https://example.com/video")
        self.page.start()

        self.assertEqual(len(self.service.enqueued), 1)
        submitted = self.service.enqueued[0]["options_json"]
        self.assertEqual(submitted["content_mode"], "video")
        self.assertEqual(submitted["container"], "mkv")
        self.assertEqual(submitted["audio_track"], "en")
        self.assertEqual(submitted["video_fps"], "120")
        self.assertEqual(submitted["source_video_codec"], "av1")
        self.assertEqual(submitted["vr_mode"], "3d180")
        self.assertEqual(self.service.enqueued[0]["subtitle_language"], "ja")
        self.assertEqual(self.service.enqueued[0]["quality"], "custom")
        self.assertEqual(self.page.task_content_mode.currentData(), "video")
        self.assertEqual(self.page.task_container.currentData(), "mkv")
        self.assertEqual(self.page.task_quality.currentData(), "custom")
        self.assertEqual(self.page.task_subtitle_language.currentData(), "ja")
        self.assertEqual(self.page.task_audio_track.currentData(), "en")

    def test_quick_download_save_failure_restores_durable_values(self) -> None:
        settings = self.window.app_settings
        with patch.object(
            settings,
            "set_many",
            side_effect=OSError("settings disk unavailable"),
        ), patch(
            "app.ui.quick_download_settings.QMessageBox.warning",
        ) as warning:
            self.page.task_quality.setCurrentIndex(
                self.page.task_quality.findData("720p")
            )

        self.assertEqual(settings.get("quality"), "best")
        self.assertEqual(self.page.task_quality.currentData(), "best")
        warning.assert_called_once()
        self.assertIn("settings disk unavailable", warning.call_args.args[2])

    def test_quality_priority_is_manual_then_best_then_resolutions(self) -> None:
        values = [
            self.page.task_quality.itemData(index)
            for index in range(self.page.task_quality.count())
        ]
        self.assertEqual(values[:4], ["custom", "best", "8k", "4k"])

    def test_quick_selectors_synchronize_an_existing_settings_page(self) -> None:
        settings_content = QComboBox()
        settings_content.addItem("Manual", "manual")
        settings_content.addItem("Video", "video")
        settings_content.addItem("Audio", "audio")
        settings_quality = QComboBox()
        for value in DOWNLOAD_QUALITY_VALUES:
            settings_quality.addItem(value, value)
        settings_container = QComboBox()
        for value in ("auto", "mp4", "mkv"):
            settings_container.addItem(value, value)
        settings_audio_track = QComboBox()
        for value in ("default", "original", "all", "en"):
            settings_audio_track.addItem(value, value)
        settings_subtitle = QComboBox()
        for value in ("none", "all", "en"):
            settings_subtitle.addItem(value, value)
        settings_fps = QComboBox()
        for value in ("best", "120"):
            settings_fps.addItem(value, value)
        settings_codec = QComboBox()
        for value in ("auto", "av1"):
            settings_codec.addItem(value, value)
        settings_vr = QComboBox()
        for value in ("any", "3d360"):
            settings_vr.addItem(value, value)
        settings_page = SimpleNamespace(
            download_content_mode=settings_content,
            quality=settings_quality,
            download_container=settings_container,
            download_audio_track=settings_audio_track,
            subtitle_language=settings_subtitle,
            download_video_fps=settings_fps,
            download_source_codec=settings_codec,
            download_vr_mode=settings_vr,
            download_options_json={},
            update_download_format_controls=lambda: None,
        )
        self.window.settings = settings_page

        self.page.task_content_mode.setCurrentIndex(
            self.page.task_content_mode.findData("audio")
        )
        self.page.task_quality.setCurrentIndex(
            self.page.task_quality.findData("720p")
        )
        self.page.task_audio_track.setCurrentIndex(
            self.page.task_audio_track.findData("en")
        )
        self.page.task_subtitle_language.setCurrentIndex(
            self.page.task_subtitle_language.findData("all")
        )
        self.page.task_video_fps.setCurrentIndex(
            self.page.task_video_fps.findData("120")
        )
        self.page.task_source_codec.setCurrentIndex(
            self.page.task_source_codec.findData("av1")
        )
        self.page.task_vr_mode.setCurrentIndex(
            self.page.task_vr_mode.findData("3d360")
        )

        self.assertEqual(settings_content.currentData(), "audio")
        self.assertEqual(settings_quality.currentData(), "720p")
        self.assertEqual(settings_audio_track.currentData(), "en")
        self.assertEqual(settings_subtitle.currentData(), "all")
        self.assertEqual(settings_fps.currentData(), "120")
        self.assertEqual(settings_codec.currentData(), "av1")
        self.assertEqual(settings_vr.currentData(), "3d360")
        self.assertEqual(settings_page.download_options_json["content_mode"], "audio")
        self.assertEqual(settings_page.download_options_json["audio_track"], "en")
        self.assertEqual(settings_page.download_options_json["vr_mode"], "3d360")

    def test_audio_only_quick_choice_disables_video_container_without_losing_selection(self) -> None:
        self.page.task_container.setCurrentIndex(
            self.page.task_container.findData("mp4")
        )
        self.page.task_content_mode.setCurrentIndex(
            self.page.task_content_mode.findData("audio")
        )
        self.assertFalse(self.page.task_container.isEnabled())
        self.assertEqual(self.page.task_container.currentData(), "mp4")

        self.page.task_content_mode.setCurrentIndex(
            self.page.task_content_mode.findData("video")
        )
        self.assertTrue(self.page.task_container.isEnabled())
        self.assertEqual(self.page.task_container.currentData(), "mp4")

    def test_open_task_folder_selects_existing_completed_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            media_path = Path(directory) / "finished video.mp4"
            media_path.write_bytes(b"video")
            task = DownloadTask(
                "completed-file",
                "https://example.com/video",
                directory,
                status="completed",
                media_path=str(media_path),
            )
            self.service.tasks[task.id] = task

            with patch("app.ui.dashboard_page.sys.platform", "win32"), patch(
                "app.ui.dashboard_page.subprocess.Popen"
            ) as popen, patch("app.ui.dashboard_page.os.startfile") as startfile:
                self.page.open_task_folder(task.id)

            popen.assert_called_once_with(["explorer.exe", "/select,", str(media_path)])
            startfile.assert_not_called()

    def test_reveal_file_or_folder_falls_back_to_task_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "downloads"
            missing_file = output_dir / "not-created.mp4"

            with patch("app.ui.dashboard_page.sys.platform", "win32"), patch(
                "app.ui.dashboard_page.subprocess.Popen"
            ) as popen, patch("app.ui.dashboard_page.os.startfile") as startfile:
                reveal_file_or_folder(missing_file, output_dir)

            self.assertTrue(output_dir.is_dir())
            popen.assert_not_called()
            startfile.assert_called_once_with(str(output_dir))

    def test_settings_selector_wheel_requires_click_and_otherwise_scrolls_page(self) -> None:
        scroll = QScrollArea()
        scroll.resize(320, 180)
        content = QWidget()
        layout = QVBoxLayout(content)
        combo = QComboBox()
        combo.addItems(["一", "二", "三"])
        layout.addWidget(combo)
        spacer = QLabel("滚动内容")
        spacer.setMinimumHeight(1200)
        layout.addWidget(spacer)
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        guard = ExplicitWheelFocusGuard(scroll, scroll)
        guard.watch(combo)
        scroll.show()
        self.app.processEvents()

        def send_wheel(delta: int) -> None:
            global_pos = QPointF(combo.mapToGlobal(combo.rect().center()))
            event = QWheelEvent(
                QPointF(combo.rect().center()),
                global_pos,
                QPoint(),
                QPoint(0, delta),
                Qt.NoButton,
                Qt.NoModifier,
                Qt.NoScrollPhase,
                False,
            )
            QApplication.sendEvent(combo, event)
            self.app.processEvents()

        combo.setCurrentIndex(0)
        scroll.verticalScrollBar().setValue(0)
        scroll.setFocus()
        send_wheel(-120)
        self.assertEqual(combo.currentIndex(), 0)
        self.assertGreater(scroll.verticalScrollBar().value(), 0)

        scroll.verticalScrollBar().setValue(0)
        self.app.processEvents()
        combo.setFocus()
        self.app.processEvents()
        click = QMouseEvent(
            QMouseEvent.MouseButtonPress,
            QPointF(combo.rect().center()),
            QPointF(combo.mapToGlobal(combo.rect().center())),
            Qt.LeftButton,
            Qt.LeftButton,
            Qt.NoModifier,
        )
        guard.eventFilter(combo, click)
        send_wheel(-120)
        self.assertEqual(combo.currentIndex(), 1)
        self.assertEqual(scroll.verticalScrollBar().value(), 0)
        scroll.close()

    def test_smart_download_performance_is_balanced_and_manual_values_are_preserved(self) -> None:
        self.assertEqual(smart_download_performance(1), (1, 6, 0.5))
        self.assertEqual(smart_download_performance(4), (2, 8, 0.0))
        self.assertEqual(smart_download_performance(8), (3, 8, 0.0))
        self.assertEqual(smart_download_performance(64), (4, 8, 0.0))

        manual_values = {
            "download_performance_mode": "manual",
            "max_concurrent": "7",
            "fragment_concurrent": "24",
            "request_delay": "1.5",
        }
        manual_settings = SimpleNamespace(get=lambda key: manual_values.get(key, ""))
        self.assertEqual(
            effective_download_performance(manual_settings, logical_processors=64),
            (7, 24, 1.5),
        )
        smart_values = {"download_performance_mode": "smart"}
        smart_settings = SimpleNamespace(get=lambda key: smart_values.get(key, ""))
        self.assertEqual(
            effective_download_performance(smart_settings, logical_processors=4),
            (2, 8, 0.0),
        )

    def test_batch_add_skips_equivalent_unfinished_task(self) -> None:
        existing = DownloadTask(
            "existing",
            "https://example.com/already",
            "D:/downloads",
            quality="best",
            playlist_mode="single",
            status="paused",
        )
        self.service.tasks[existing.id] = existing
        context = {
            "output_dir": "D:/downloads",
            "proxy": "",
            "cookie_file": "",
            "quality": "best",
            "filename_template": "%(title)s.%(ext)s",
            "ffmpeg_path": "",
            "download_album": False,
            "playlist_mode": "single",
        }

        with patch.object(
            self.page.submission_workflow,
            "request_context",
            return_value=context,
        ):
            created, skipped = self.page.submission_workflow.enqueue_links([
                "https://example.com/already",
                "https://example.com/new",
            ])

        self.assertEqual((created, skipped), (1, 1))
        self.assertEqual([entry["url"] for entry in self.service.enqueued], ["https://example.com/new"])
        self.assertIn("跳过 1 个", self.page.status.text())

    def test_single_collection_override_uses_one_consistent_task_snapshot(self) -> None:
        context = {
            "output_dir": "D:/downloads",
            "proxy": "",
            "cookie_file": "",
            "quality": "best",
            "filename_template": "%(title)s.%(ext)s",
            "ffmpeg_path": "",
            "download_album": True,
            "playlist_mode": "playlist",
            "options_json": {"collection_mode": "single"},
        }

        with patch.object(
            self.page.submission_workflow,
            "request_context",
            return_value=context,
        ):
            created, skipped = self.page.submission_workflow.enqueue_links([
                "https://example.com/watch?v=single",
            ])

        self.assertEqual((created, skipped), (1, 0))
        task = self.service.enqueued[0]
        self.assertEqual(task["playlist_mode"], "single")
        self.assertFalse(task["download_album"])

    def test_single_collection_override_deduplicates_against_single_task(self) -> None:
        existing = DownloadTask(
            "existing-single",
            "https://example.com/watch?v=duplicate",
            "D:/downloads",
            quality="best",
            playlist_mode="single",
            status="queued",
        )
        self.service.tasks[existing.id] = existing
        context = {
            "output_dir": "D:/downloads",
            "proxy": "",
            "cookie_file": "",
            "quality": "best",
            "filename_template": "%(title)s.%(ext)s",
            "ffmpeg_path": "",
            "download_album": True,
            "playlist_mode": "playlist",
            "options_json": {"collection_mode": "single"},
        }

        with patch.object(
            self.page.submission_workflow,
            "request_context",
            return_value=context,
        ), patch("app.ui.download_submission_workflow.QMessageBox.information"):
            created, skipped = self.page.submission_workflow.enqueue_links([
                "https://example.com/watch?v=duplicate",
            ])

        self.assertEqual((created, skipped), (0, 1))
        self.assertFalse(self.service.enqueued)

    def test_rapid_repeat_submission_is_debounced_before_a_second_enqueue(self) -> None:
        context = {
            "output_dir": "D:/downloads",
            "proxy": "",
            "cookie_file": "",
            "quality": "best",
            "filename_template": "%(title)s.%(ext)s",
            "ffmpeg_path": "",
            "download_album": False,
            "playlist_mode": "single",
        }
        links = ["https://example.com/rapid"]

        with patch.object(
            self.page.submission_workflow,
            "request_context",
            return_value=context,
        ):
            first = self.page.submission_workflow.enqueue_links(links)
            second = self.page.submission_workflow.enqueue_links(links)

        self.assertEqual(first, (1, 0))
        self.assertEqual(second, (0, 1))
        self.assertEqual(len(self.service.enqueued), 1)
        self.assertFalse(self.page.add_download_button.isEnabled())
        self.assertFalse(self.page.paste_download_button.isEnabled())
        self.assertIn("忽略重复点击", self.page.status.text())

    def test_batch_submission_parses_advanced_options_once(self) -> None:
        context = {
            "output_dir": "D:/downloads",
            "proxy": "",
            "cookie_file": "",
            "quality": "best",
            "filename_template": "%(title)s.%(ext)s",
            "ffmpeg_path": "",
            "playlist_mode": "single",
            "options_json": {"collection_mode": "single"},
        }

        with patch.object(
            self.page.submission_workflow,
            "request_context",
            return_value=context,
        ), patch(
            "app.ui.download_submission_workflow.DownloadOptions.from_mapping",
            wraps=DownloadOptions.from_mapping,
        ) as parse_options:
            created, skipped = self.page.submission_workflow.enqueue_links([
                "https://example.com/one",
                "https://example.com/two",
            ])

        self.assertEqual((created, skipped), (2, 0))
        parse_options.assert_called_once_with(context["options_json"])

    def test_rejected_collection_probe_is_not_reported_as_created(self) -> None:
        self.service.ytdlp_core_mode = "auto"
        context = {
            "output_dir": "D:/downloads",
            "proxy": "",
            "cookie_file": "",
            "quality": "best",
            "filename_template": "%(title)s.%(ext)s",
            "ffmpeg_path": "",
            "playlist_mode": "auto",
            "options_json": {},
        }

        with patch.object(
            self.page.submission_workflow,
            "request_context",
            return_value=context,
        ), patch.object(
            self.page.submission_workflow,
            "start_collection",
            return_value=False,
        ), patch(
            "app.ui.download_submission_workflow.QMessageBox.critical",
        ) as critical:
            created, skipped = self.page.submission_workflow.enqueue_links([
                "https://example.com/playlist",
            ])

        self.assertEqual((created, skipped), (0, 0))
        critical.assert_called_once()
        self.assertIn("聚合解析器正在退出", critical.call_args.args[2])

    def test_submission_failure_does_not_claim_unwritten_diagnostic_log(self) -> None:
        context = {
            "output_dir": "D:/downloads",
            "proxy": "",
            "cookie_file": "",
            "quality": "best",
            "filename_template": "%(title)s.%(ext)s",
            "ffmpeg_path": "",
            "playlist_mode": "single",
            "options_json": {"collection_mode": "single"},
        }

        with patch.object(
            self.page.submission_workflow,
            "request_context",
            return_value=context,
        ), patch.object(
            self.service,
            "enqueue",
            side_effect=RuntimeError("publication failed"),
        ), patch(
            "app.ui.download_submission_workflow.QMessageBox.critical",
        ) as critical, patch(
            "app.ui.download_submission_workflow.Path.open",
            side_effect=OSError("read-only log directory"),
        ):
            created, skipped = self.page.submission_workflow.enqueue_links([
                "https://example.com/failure",
            ])

        self.assertEqual((created, skipped), (0, 0))
        critical.assert_called_once()
        message = critical.call_args.args[2]
        self.assertIn("无法写入诊断日志", message)
        self.assertNotIn("详细信息已写入", message)

    def test_submit_context_does_not_probe_components_or_touch_download_folder(self) -> None:
        with patch("app.core.download_readiness.runtime_component_presence") as component_probe, patch.object(
            self.page, "_ensure_download_dir"
        ) as ensure_directory:
            context = self.page.submission_workflow.request_context()

        self.assertIsNotNone(context)
        self.assertEqual(
            os.path.normcase(str(context["output_dir"])),
            os.path.normcase("D:\\downloads"),
        )
        component_probe.assert_not_called()
        ensure_directory.assert_not_called()

    def test_collection_probe_creates_parent_and_nested_probe_reuses_dedupe_snapshot(self) -> None:
        identity_calls: list[bool] = []

        def completed_media_identities():
            identity_calls.append(True)
            return (
                {"generic:completed"},
                {"https://example.com/completed"},
                {"Completed Video"},
            )

        self.window.db = SimpleNamespace(
            completed_media_identities=completed_media_identities,
            collection_probe_entry_count=lambda _task_id: 0,
        )
        self.service.ytdlp_core_mode = "auto"
        context = {
            "output_dir": "D:/downloads",
            "proxy": "",
            "cookie_file": "",
            "cookie_source": "none",
            "cookie_browser": "chrome",
            "cookie_profile": "",
            "cookie_keyring": "",
            "cookie_container": "",
            "quality": "best",
            "filename_template": "%(title)s.%(ext)s",
            "ffmpeg_path": "",
            "download_album": False,
            "playlist_mode": "auto",
            "transcode_encoder": "original",
            "transcode_codec": "original",
            "transcode_device": "auto",
            "subtitle_language": "none",
            "options_json": {},
        }

        with patch.object(
            self.page.collection_workflow.coordinator,
            "start_pending",
        ) as start_workers:
            started = self.page.collection_workflow.start_probe(
                "https://example.com/list",
                context,
            )
            request_id, state = next(
                iter(self.page.collection_workflow.coordinator.states.items())
            )
            self.page.collection_workflow.active_request_id = request_id
            self.page.collection_workflow.parse_nested({
                "url": "https://example.com/nested",
                "index": 2,
            })

        self.assertEqual(start_workers.call_count, 2)
        self.assertEqual(len(identity_calls), 1)
        self.assertTrue(started)
        self.assertEqual(len(self.service.tasks), 2)
        parent = self.service.tasks[state["parent_id"]]
        self.assertEqual(parent.task_kind, "collection")
        self.assertEqual(parent.status, "parsing_collection")
        self.assertEqual(state["parent_id"], parent.id)
        nested_state = next(
            item
            for key, item in self.page.collection_workflow.coordinator.states.items()
            if key != request_id
        )
        self.assertEqual(
            nested_state["request"].completed_source_keys,
            state["request"].completed_source_keys,
        )
        self.assertEqual(
            nested_state["request"].completed_urls,
            state["request"].completed_urls,
        )
        self.assertEqual(
            nested_state["request"].completed_titles,
            state["request"].completed_titles,
        )

    def test_collection_probe_enqueue_rejection_removes_new_parent(self) -> None:
        self.window.db = SimpleNamespace(
            completed_media_identities=lambda: (set(), set(), set()),
            collection_probe_entry_count=lambda _task_id: 0,
        )
        self.service.ytdlp_core_mode = "auto"
        context = {
            "output_dir": "D:/downloads",
            "proxy": "",
            "cookie_file": "",
            "cookie_source": "none",
            "cookie_browser": "chrome",
            "cookie_profile": "",
            "cookie_keyring": "",
            "cookie_container": "",
            "quality": "best",
            "filename_template": "%(title)s.%(ext)s",
            "ffmpeg_path": "",
            "download_album": False,
            "playlist_mode": "auto",
            "transcode_encoder": "original",
            "transcode_codec": "original",
            "transcode_device": "auto",
            "subtitle_language": "none",
            "options_json": {},
        }

        with patch.object(
            self.page.collection_workflow.coordinator,
            "enqueue",
            return_value=False,
        ):
            started = self.page.collection_workflow.start_probe(
                "https://example.com/list",
                context,
            )

        self.assertFalse(started)
        self.assertFalse(self.service.tasks)

    def test_collection_probe_setup_failure_rolls_back_new_parent(self) -> None:
        self.window.db = SimpleNamespace(
            completed_media_identities=lambda: (set(), set(), set()),
            collection_probe_entry_count=lambda _task_id: (_ for _ in ()).throw(
                OSError("collection cache unavailable")
            ),
        )
        self.service.ytdlp_core_mode = "auto"
        context = {
            "output_dir": "D:/downloads",
            "proxy": "",
            "cookie_file": "",
            "cookie_source": "none",
            "cookie_browser": "chrome",
            "cookie_profile": "",
            "cookie_keyring": "",
            "cookie_container": "",
            "quality": "best",
            "filename_template": "%(title)s.%(ext)s",
            "ffmpeg_path": "",
            "download_album": False,
            "playlist_mode": "auto",
            "transcode_encoder": "original",
            "transcode_codec": "original",
            "transcode_device": "auto",
            "subtitle_language": "none",
            "options_json": {},
        }

        with self.assertRaisesRegex(OSError, "collection cache unavailable"):
            self.page.collection_workflow.start_probe(
                "https://example.com/list",
                context,
            )

        self.assertFalse(self.service.tasks)
        self.assertFalse(self.page.collection_workflow.coordinator.states)

    def test_collection_resume_ignores_confirmed_stale_probe_state(self) -> None:
        task = DownloadTask(
            "resume-stale-probe",
            "https://example.com/playlist",
            "D:/downloads",
            task_kind="collection",
            status="parsing_collection",
            stage="parsing_collection",
            options_json={},
        )
        self.service.tasks[task.id] = task
        self.window.db = SimpleNamespace(
            collection_probe_entry_count=lambda _task_id: 0,
        )
        workflow = self.page.collection_workflow
        workflow.coordinator.states["confirmed-stale"] = {
            "parent_id": task.id,
            "confirmed": True,
        }

        with patch.object(workflow, "start_probe", return_value=True) as start_probe:
            workflow.resume()

        start_probe.assert_called_once()
        self.assertEqual(start_probe.call_args.kwargs["existing_parent_id"], task.id)

    def test_collection_probe_entries_use_database_collection_api(self) -> None:
        database = Database(Path(self.temp_dir.name) / "collection-probe.db")
        self.window.db = database
        parent = DownloadTask(
            "collection-probe-parent",
            "https://example.com/playlist",
            "D:/downloads",
            task_kind="collection",
            status="parsing_collection",
            stage="parsing_collection",
        )
        self.service.tasks[parent.id] = parent
        request_id = "collection-probe-entries"
        workflow = self.page.collection_workflow
        workflow.coordinator.states[request_id] = {
            "parent_id": parent.id,
            "context": {"options_json": {"collection_mode": "select"}},
            "metadata": {"title": "Playlist", "source_key": "Example:playlist"},
            "entry_count": 0,
            "confirmed": False,
        }

        workflow._on_entries(request_id, [{
            "index": 1,
            "url": "https://example.com/video/1",
            "title": "Episode 1",
            "entry_kind": "video",
            "downloadable": True,
            "selected": True,
        }])

        self.assertEqual(database.collection_probe_entry_count(parent.id), 1)
        self.assertEqual(workflow.selection_view.model.source_count(), 1)
        self.assertEqual(
            database.list_collection_probe_entries(parent.id)[0]["title"],
            "Episode 1",
        )

        workflow._on_entries(request_id, [{
            "index": 2,
            "url": "https://example.com/video/2",
            "title": "Episode 2",
            "entry_kind": "video",
            "downloadable": True,
            "selected": True,
        }])

        self.assertEqual(database.collection_probe_entry_count(parent.id), 2)
        self.assertEqual(workflow.selection_view.model.source_count(), 2)

        workflow.request_shutdown()
        self.assertEqual(workflow.selection_view.model.source_count(), 0)
        database.close()

    def test_collection_resume_waits_for_confirmed_running_probe_to_exit(self) -> None:
        task = DownloadTask(
            "resume-running-probe",
            "https://example.com/playlist",
            "D:/downloads",
            task_kind="collection",
            status="parsing_collection",
            stage="parsing_collection",
        )
        self.service.tasks[task.id] = task
        workflow = self.page.collection_workflow
        thread = QThread(self.page)
        workflow.coordinator.states["confirmed-running"] = {
            "parent_id": task.id,
            "confirmed": True,
            "thread": thread,
        }

        with patch.object(QThread, "isRunning", return_value=True), patch.object(
            workflow,
            "start_probe",
        ) as start_probe:
            workflow.resume()

        start_probe.assert_not_called()
        thread.deleteLater()

    def test_waiting_collection_resume_snapshots_options_without_duplication(self) -> None:
        options = {
            "collection_mode": "select",
            "quality": "best",
        }
        task = DownloadTask(
            "resume-waiting-selection",
            "https://example.com/playlist",
            "D:/downloads",
            title="Playlist",
            task_kind="collection",
            status="waiting_selection",
            stage="waiting_selection",
            source_key="Example:playlist",
            options_json=options,
        )
        self.service.tasks[task.id] = task
        persisted_options = dict(task.options_json)
        self.window.db = SimpleNamespace(
            collection_probe_entry_count=lambda _task_id: 3,
            completed_media_identities=lambda: (set(), set(), set()),
        )
        workflow = self.page.collection_workflow

        with patch.object(workflow, "show_selection") as show_selection, patch.object(
            workflow.selection_view,
            "set_finished",
        ) as set_finished:
            workflow.resume()
            workflow.resume()

        self.assertEqual(len(workflow.coordinator.states), 1)
        state = next(iter(workflow.coordinator.states.values()))
        snapshot = state["context"]["options_json"]
        self.assertEqual(snapshot, persisted_options)
        self.assertIsNot(snapshot, task.options_json)
        task.options_json["collection_mode"] = "all"
        self.assertEqual(
            DownloadOptions.from_mapping(snapshot).collection_mode,
            "select",
        )
        show_selection.assert_called_once()
        set_finished.assert_called_once()

    def test_collection_probe_thread_start_failure_marks_parent_failed_and_releases_slot(self) -> None:
        request_id = "probe-start-failure"
        parent = DownloadTask(
            "collection-start-failure",
            "https://example.com/list",
            "D:/downloads",
            task_kind="collection",
            status="parsing_collection",
            stage="parsing_collection",
        )
        self.service.tasks[parent.id] = parent
        self.service._persist = lambda _task: None  # type: ignore[attr-defined]
        self.service.task_updated = SimpleNamespace(emit=lambda _task: None)
        self.page.collection_workflow.coordinator.states[request_id] = {
            "request": CollectionProbeRequest(request_id, parent.url),
            "parent_id": parent.id,
            "thread": None,
            "worker": None,
            "confirmed": False,
        }
        self.page.collection_workflow.coordinator.queue.append(request_id)

        with patch(
            "app.ui.collection_probe_coordinator.QThread.start",
            side_effect=RuntimeError("thread resource exhausted"),
        ):
            self.page.collection_workflow.coordinator.start_pending()

        self.assertEqual(parent.status, "failed")
        self.assertEqual(parent.stage, "failed")
        self.assertIn("thread resource exhausted", parent.error)
        self.assertNotIn(request_id, self.page.collection_workflow.coordinator.states)
        self.assertFalse(self.page.collection_probe_running)

    def test_collection_probe_wiring_failure_marks_parent_failed_without_leaking_slot(self) -> None:
        request_id = "probe-wiring-failure"
        parent = DownloadTask(
            "collection-wiring-failure",
            "https://example.com/list",
            "D:/downloads",
            task_kind="collection",
            status="parsing_collection",
            stage="parsing_collection",
        )
        self.service.tasks[parent.id] = parent
        self.service._persist = lambda _task: None  # type: ignore[attr-defined]
        self.service.task_updated = SimpleNamespace(emit=lambda _task: None)
        self.page.collection_workflow.coordinator.states[request_id] = {
            "request": CollectionProbeRequest(request_id, parent.url),
            "parent_id": parent.id,
            "thread": None,
            "worker": None,
            "confirmed": False,
        }
        self.page.collection_workflow.coordinator.queue.append(request_id)

        with patch(
            "app.ui.collection_probe_coordinator.CollectionProbeWorker.moveToThread",
            side_effect=RuntimeError("signal wiring failed"),
        ), patch(
            "app.ui.collection_probe_coordinator.delete_unstarted_worker",
        ) as delete_worker, patch(
            "app.ui.collection_probe_coordinator.QThread.start",
        ) as start_thread:
            self.page.collection_workflow.coordinator.start_pending()

        start_thread.assert_not_called()
        delete_worker.assert_called_once()
        self.assertEqual(parent.status, "failed")
        self.assertEqual(parent.stage, "failed")
        self.assertIn("signal wiring failed", parent.error)
        self.assertNotIn(request_id, self.page.collection_workflow.coordinator.states)
        self.assertFalse(self.page.collection_probe_running)

    def test_collection_probe_thread_construction_failure_releases_queue_slot(self) -> None:
        request_id = "probe-construction-failure"
        parent = DownloadTask(
            "collection-construction-failure",
            "https://example.com/list",
            "D:/downloads",
            task_kind="collection",
            status="parsing_collection",
            stage="parsing_collection",
        )
        self.service.tasks[parent.id] = parent
        self.service._persist = lambda _task: None  # type: ignore[attr-defined]
        self.service.task_updated = SimpleNamespace(emit=lambda _task: None)
        self.page.collection_workflow.coordinator.states[request_id] = {
            "request": CollectionProbeRequest(request_id, parent.url),
            "parent_id": parent.id,
            "thread": None,
            "worker": None,
            "confirmed": False,
        }
        self.page.collection_workflow.coordinator.queue.append(request_id)

        class FailingThread(QThread):
            def __init__(self, *_args, **_kwargs):
                raise RuntimeError("thread construction failed")

        with patch("app.ui.collection_probe_coordinator.QThread", FailingThread):
            self.page.collection_workflow.coordinator.start_pending()

        self.assertEqual(parent.status, "failed")
        self.assertIn("thread construction failed", parent.error)
        self.assertNotIn(request_id, self.page.collection_workflow.coordinator.states)
        self.assertFalse(self.page.collection_probe_running)

    def test_collection_probe_cleanup_waits_for_queued_failure_result(self) -> None:
        request_id = "probe-deferred-failure"
        parent = DownloadTask(
            "collection-deferred-failure",
            "https://example.com/list",
            "D:/downloads",
            task_kind="collection",
            status="parsing_collection",
            stage="parsing_collection",
        )
        self.service.tasks[parent.id] = parent
        self.service._persist = lambda _task: None  # type: ignore[attr-defined]
        self.service.task_updated = SimpleNamespace(emit=lambda _task: None)
        thread = QThread(self.page)
        self.page.collection_workflow.coordinator.states[request_id] = {
            "parent_id": parent.id,
            "thread": thread,
            "worker": object(),
            "confirmed": False,
        }

        with patch.object(
            self.page.collection_workflow.coordinator,
            "start_pending",
        ) as start_next:
            self.page.collection_workflow.coordinator.defer_thread_finish(
                request_id,
                thread,
            )
            self.page.collection_workflow._on_failed(
                request_id,
                "late parser failure",
            )
            self.app.processEvents()

        self.assertEqual(parent.status, "failed")
        self.assertEqual(parent.error, "late parser failure")
        self.assertNotIn(request_id, self.page.collection_workflow.coordinator.states)
        self.assertFalse(self.page.collection_probe_running)
        start_next.assert_called_once_with()

    def test_canceled_running_collection_ignores_queued_worker_results(self) -> None:
        request_id = "probe-canceled-with-late-results"
        parent = DownloadTask(
            "collection-canceled-with-late-results",
            "https://example.com/list",
            "D:/downloads",
            task_kind="collection",
            status="parsing_collection",
            stage="parsing_collection",
        )
        self.service.tasks[parent.id] = parent
        thread = QThread(self.page)
        workflow = self.page.collection_workflow
        workflow.coordinator.states[request_id] = {
            "parent_id": parent.id,
            "thread": thread,
            "worker": None,
            "confirmed": False,
        }
        self.window.db = SimpleNamespace(
            upsert_collection_probe_entries=lambda *_args, **_kwargs: self.fail(
                "late entries must not be persisted"
            ),
            collection_probe_entry_count=lambda *_args, **_kwargs: self.fail(
                "late entries must not be counted"
            ),
        )

        with patch.object(QThread, "isRunning", return_value=True):
            self.assertTrue(workflow.coordinator.cancel(request_id))
        self.assertTrue(workflow.coordinator.states[request_id]["confirmed"])
        self.service.delete_task(parent.id, False)

        with patch(
            "app.ui.collection_workflow.QMessageBox.warning",
        ) as warning:
            workflow._on_metadata(request_id, {"title": "late title"})
            workflow._on_entries(request_id, [{"index": 1}])
            workflow._on_single(request_id, {"title": "late video"})
            workflow._on_failed(request_id, "late parser failure")
            workflow._on_finished(request_id, True, 1)

        warning.assert_not_called()
        self.assertNotIn(parent.id, self.service.tasks)
        workflow.coordinator.complete_thread_finish(request_id, thread)
        self.assertNotIn(request_id, workflow.coordinator.states)

    def test_task_card_caches_missing_thumbnail_probe_during_progress_updates(self) -> None:
        task = DownloadTask(
            "thumbnail-cache",
            "https://example.com/video",
            "D:/downloads",
            thumbnail_path="D:/downloads/not-ready.jpg",
            status="downloading",
        )
        with patch("app.ui.main_window.Path.is_file", return_value=False) as probe:
            card = DownloadTaskCard(task)
            card.update_task(task)
            card.update_task(task)
            self.assertEqual(probe.call_count, 1)
        card.deleteLater()

    def test_adding_new_task_keeps_existing_card_and_loaded_thumbnail(self) -> None:
        thumbnail = Path(self.temp_dir.name) / "existing-cover.jpg"
        image = QImage(64, 36, QImage.Format.Format_RGB32)
        image.fill(QColor(30, 80, 140))
        self.assertTrue(image.save(str(thumbnail), "JPG"))
        existing = DownloadTask(
            "existing-thumbnail",
            "https://example.com/existing",
            "D:/downloads",
            thumbnail_path=str(thumbnail),
            status="downloading",
            created_at="2026-08-25T08:00:00",
        )
        self.service.tasks[existing.id] = existing
        self.page.add_task(existing)
        original_card = self.page.cards[existing.id]
        self.assertFalse(original_card.thumbnail.pixmap().isNull())

        added = DownloadTask(
            "new-task",
            "https://example.com/new",
            "D:/downloads",
            status="queued",
            created_at="2026-08-25T08:01:00",
        )
        self.service.tasks[added.id] = added
        self.page.add_task(added)

        self.assertIs(self.page.cards[existing.id], original_card)
        self.assertFalse(original_card.thumbnail.pixmap().isNull())
        self.assertEqual(self.page.task_list.item(0).data(Qt.UserRole), added.id)

    def test_missing_card_index_is_repaired_without_leaving_a_blank_row(self) -> None:
        task = DownloadTask(
            "repair-missing-card-index",
            "https://example.com/repair",
            "D:/downloads",
            status="queued",
        )
        self.service.tasks[task.id] = task
        self.page.add_task(task)
        item = self.page.items[task.id]
        original_card = self.page.cards.pop(task.id)
        self.assertIs(self.page.task_list.itemWidget(item), original_card)

        self.page.add_task(task)
        self.app.processEvents()

        replacement = self.page.cards[task.id]
        self.assertIsNot(replacement, original_card)
        self.assertIs(self.page.task_list.itemWidget(self.page.items[task.id]), replacement)
        self.assertEqual(self.page.task_list.count(), 1)

    def test_card_factory_failure_does_not_leave_an_orphan_list_item(self) -> None:
        task = DownloadTask(
            "card-construction-failure",
            "https://example.com/failure",
            "D:/downloads",
            status="queued",
        )
        self.service.tasks[task.id] = task

        with patch.object(
            self.page.task_rows,
            "card_factory",
            side_effect=RuntimeError("card failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "card failed"):
                self.page.add_task(task)

        self.assertNotIn(task.id, self.page.items)
        self.assertNotIn(task.id, self.page.cards)
        self.assertEqual(self.page.task_list.count(), 0)

    def test_task_pipeline_uses_the_localized_task_context_menu(self) -> None:
        task = DownloadTask(
            "pipeline-menu",
            "https://example.com/video",
            "D:/downloads",
            status="downloading",
            stage="downloading",
        )
        card = DownloadTaskCard(task)
        requests: list[tuple[str, object]] = []
        card.context_requested.connect(lambda task_id, position: requests.append((task_id, position)))

        self.assertEqual(card.pipeline.contextMenuPolicy(), Qt.CustomContextMenu)
        card.pipeline.customContextMenuRequested.emit(QPoint(4, 5))
        self.assertEqual([task_id for task_id, _position in requests], [task.id])
        card.deleteLater()

    def test_task_menu_capabilities_cover_transitional_and_terminal_states(self) -> None:
        media_path = Path(self.temp_dir.name) / "completed.mp4"
        media_path.write_bytes(b"media")

        cases = (
            ("video", "downloading", "", "pause_download", True, False, False),
            ("video", "canceling", "", "", False, False, False),
            ("video", "paused", "", "resume_download", False, True, False),
            ("video", "completed", str(media_path), "", False, True, True),
            ("video", "completed", str(media_path.with_name("missing.mp4")), "", False, True, False),
            ("collection", "queued", "", "pause_collection", True, False, False),
            ("collection", "paused", "", "resume_collection", False, True, False),
            ("collection", "partial_failed", "", "", False, True, False),
        )
        for task_kind, status, path, pause_mode, can_cancel, can_retry, can_convert in cases:
            with self.subTest(task_kind=task_kind, status=status, path=path):
                task = DownloadTask(
                    f"{task_kind}-{status}-{bool(path)}",
                    "https://example.com/item",
                    self.temp_dir.name,
                    task_kind=task_kind,
                    status=status,
                    media_path=path,
                )
                capabilities = task_menu_capabilities(task)
                self.assertEqual(capabilities.pause_mode, pause_mode)
                self.assertEqual(capabilities.can_cancel, can_cancel)
                self.assertEqual(capabilities.can_retry, can_retry)
                self.assertEqual(capabilities.can_convert, can_convert)

    def test_collection_context_menu_uses_collection_specific_labels(self) -> None:
        task = DownloadTask(
            "collection-menu-labels",
            "https://example.com/playlist",
            self.temp_dir.name,
            task_kind="collection",
            status="paused",
        )

        menu, actions = self.page.task_menu_controller.build(task)

        self.assertEqual(actions.copy_link.text(), "复制合集链接")
        self.assertEqual(actions.copy_folder.text(), "复制合集文件夹路径")
        self.assertEqual(actions.pause_or_resume.text(), "恢复合集")
        self.assertIsNone(actions.custom_redownload)
        self.assertIsNone(actions.convert)
        menu.deleteLater()

    def test_missing_completed_media_does_not_offer_conversion(self) -> None:
        task = DownloadTask(
            "missing-completed-media",
            "https://example.com/video",
            self.temp_dir.name,
            task_kind="video",
            status="completed",
            media_path=str(Path(self.temp_dir.name) / "missing.mp4"),
        )

        menu, actions = self.page.task_menu_controller.build(task)

        self.assertIsNone(actions.convert)
        menu.deleteLater()

    def test_completed_task_convert_action_uses_saved_encoder_without_second_picker(self) -> None:
        media_path = Path(self.temp_dir.name) / "completed.webm"
        media_path.write_bytes(b"media")
        task = DownloadTask(
            "convert-completed",
            "https://example.com/video",
            str(media_path.parent),
            title="completed",
            status="completed",
            media_path=str(media_path),
        )
        self.window.app_settings.values.update({
            "transcode_encoder": "h264_nvenc",
            "ffmpeg_path": "tools/ffmpeg/ffmpeg.exe",
            "ffprobe_path": "tools/ffmpeg/ffprobe.exe",
        })
        self.service.tasks[task.id] = task
        self.page.add_task(task)
        self.page.task_restore.set_loaded()

        class FakeMenu:
            def __init__(self, _parent=None):
                self.actions = []

            def addAction(self, label):
                action = SimpleNamespace(label=label)
                self.actions.append(action)
                return action

            def addSeparator(self):
                return None

            def exec(self, _position):
                return next(action for action in self.actions if action.label == "转换格式")

        with patch.object(self.page, "_database_task_ids", return_value={task.id}), patch(
            "app.ui.task_context_menu.QMenu",
            FakeMenu,
        ), patch("PySide6.QtWidgets.QInputDialog.getItem") as picker:
            self.page.show_task_menu(task.id, QPoint(10, 10))

        picker.assert_not_called()
        self.assertEqual(self.service.converted, [{
            "task_id": task.id,
            "encoder": "h264_nvenc",
            "ffmpeg_path": "tools/ffmpeg/ffmpeg.exe",
            "ffprobe_path": "tools/ffmpeg/ffprobe.exe",
        }])

    def test_task_menu_database_read_failure_does_not_clear_live_tasks(self) -> None:
        task = DownloadTask(
            "database-read-failure",
            "https://example.com/video",
            self.temp_dir.name,
            status="queued",
        )
        self.service.tasks[task.id] = task
        self.page.add_task(task)
        self.page.task_restore.set_loaded()

        with patch.object(
            self.page,
            "_database_task_ids",
            return_value=None,
        ), patch.object(
            self.page,
            "_drop_all_stale_tasks",
        ) as drop_all, patch.object(
            self.page,
            "_drop_stale_task",
        ) as drop_one, patch.object(
            self.page.task_menu_controller,
            "show",
        ) as show_menu:
            self.page.show_task_menu(task.id, QPoint(10, 10))

        drop_all.assert_not_called()
        drop_one.assert_not_called()
        show_menu.assert_not_called()
        self.assertIn(task.id, self.service.tasks)
        self.assertIn(task.id, self.page.items)
        self.assertIn("未执行任何操作", self.page.status.text())

    def test_task_card_keeps_percentage_visible_when_bytes_are_not_available(self) -> None:
        task = DownloadTask(
            "percentage-only",
            "https://example.com/video",
            "D:/downloads",
            status="downloading",
            progress=34.0,
            downloaded_bytes=0,
            total_bytes=0,
        )
        card = DownloadTaskCard(task)
        self.assertIn("进度 34%", card.details.text())
        self.assertNotIn("0.0 B / 未知", card.details.text())
        task.size = "1.9 GiB"
        card.update_task(task)
        self.assertIn("进度 34%", card.details.text())
        self.assertIn("1.9 GiB", card.details.text())
        card.deleteLater()

    def test_task_card_shows_actual_selected_video_quality(self) -> None:
        task = DownloadTask(
            "selected-quality",
            "https://example.com/video",
            "D:/downloads",
            status="downloading",
            stage="downloading_video",
            stage_text="正在下载视频",
            selected_quality="12K · 11520×6480 · 240 FPS · AV1 · HDR10",
        )

        card = DownloadTaskCard(task)
        self.assertFalse(card.quality_badge.isHidden())
        self.assertIn("当前画质", card.quality_badge.text())
        self.assertIn("12K", card.quality_badge.text())
        self.assertIn("视频", card.quality_badge.text())
        self.assertIn("自动", card.quality_badge.text())
        self.assertIn("11520×6480", card.quality_badge.toolTip())
        card.deleteLater()

    def test_task_card_shows_audio_only_output_choice_before_download_starts(self) -> None:
        task = DownloadTask(
            "audio-output",
            "https://example.com/audio",
            "D:/downloads",
            status="queued",
            options_json={"content_mode": "audio", "audio_format": "flac"},
        )

        card = DownloadTaskCard(task)
        self.assertFalse(card.quality_badge.isHidden())
        self.assertIn("音频", card.quality_badge.text())
        self.assertIn("FLAC", card.quality_badge.text())
        self.assertIn("任务选项", card.quality_badge.toolTip())
        card.deleteLater()

    def test_task_card_rebuild_restores_task_level_visible_progress_snapshot(self) -> None:
        task = DownloadTask(
            "rebuilt-progress",
            "https://example.com/video",
            "D:/downloads",
            status="downloading",
            progress=0.0,
            downloaded_bytes=0,
            total_bytes=0,
            size="",
            speed="",
            eta="",
            visible_progress=42.0,
            visible_downloaded_bytes=420 * 1024 * 1024,
            visible_total_bytes=1000 * 1024 * 1024,
            visible_size="1000 MiB",
            visible_speed="12.5 MiB/s",
            visible_eta="00:46",
        )

        # Dashboard rows may be recreated after filtering or Qt widget
        # invalidation. A new card must recover the stable in-memory snapshot
        # instead of rendering the latest transitional zero-valued callback.
        card = DownloadTaskCard(task)

        self.assertEqual(card.progress.value(), 42)
        self.assertIn("420.0 MiB", card.details.text())
        self.assertIn("1000.0 MiB", card.details.text())
        self.assertIn("12.5 MiB/s", card.details.text())
        self.assertIn("00:46", card.details.text())
        self.assertNotIn("0.0 B / 未知", card.details.text())
        card.deleteLater()

    def test_postprocessing_hides_stale_download_size_estimate(self) -> None:
        task = DownloadTask(
            "merge-size-estimate",
            "https://example.com/video",
            "D:/downloads",
            status="downloading",
            stage="merging",
            stage_text="正在合并视频和音频",
            progress=100.0,
            downloaded_bytes=354 * 1024 * 1024,
            total_bytes=839 * 1024 * 1024,
            speed="51.74 MiB/s",
            eta="0s",
            video_progress=100.0,
            audio_progress=100.0,
            stage_elapsed_seconds=7.0,
        )

        card = DownloadTaskCard(task)

        self.assertEqual(card.status.text(), "处理中")
        self.assertEqual((card.progress.minimum(), card.progress.maximum()), (0, 0))
        self.assertIn("视频 100% / 音频 100%", card.details.text())
        self.assertIn("正在本地合并", card.details.text())
        self.assertIn("当前阶段已用时 7s", card.details.text())
        self.assertNotIn("839.0 MiB", card.details.text())
        self.assertNotIn("51.74 MiB/s", card.details.text())
        with patch(
            "app.ui.task_card.time.monotonic",
            return_value=card._stage_activity_started_at + 5.0,
        ):
            card._refresh_processing_activity()
        self.assertIn("当前阶段已用时 12s", card.details.text())
        card.deleteLater()

    def test_every_task_stage_has_determinate_progress_or_live_activity_feedback(self) -> None:
        cases = (
            # status, stage, overall progress, stage progress, total bytes,
            # expected busy indicator, expected progress value
            ("queued", "queued", 0.0, 0.0, 0, False, 0),
            ("downloading", "parsing", 0.0, 0.0, 0, True, None),
            ("downloading", "formats", 0.0, 0.0, 0, True, None),
            ("waiting_selection", "waiting_selection", 0.0, 0.0, 0, False, 0),
            ("downloading", "waiting_disk", 0.0, 0.0, 0, True, None),
            ("downloading", "downloading", 0.0, 0.0, 0, True, None),
            ("downloading", "downloading", 35.0, 0.0, 1000, False, 35),
            ("downloading", "downloading_video", 45.0, 45.0, 1000, False, 45),
            ("downloading", "downloading_audio", 65.0, 65.0, 1000, False, 65),
            ("downloading", "reconnecting", 0.0, 0.0, 0, True, None),
            ("downloading", "reconnecting", 42.0, 0.0, 1000, False, 42),
            ("downloading", "merging", 100.0, 0.0, 1000, True, None),
            ("downloading", "transcoding", 100.0, 0.0, 1000, True, None),
            ("downloading", "transcoding", 100.0, 42.0, 1000, False, 42),
            ("downloading", "thumbnail", 100.0, 0.0, 1000, True, None),
            ("downloading", "metadata", 100.0, 0.0, 1000, True, None),
            ("downloading", "verifying", 100.0, 0.0, 1000, True, None),
            ("parsing_collection", "parsing_collection", 0.0, 0.0, 0, True, None),
            ("canceling", "canceled", 51.0, 0.0, 1000, True, None),
            ("暂停中", "paused", 51.0, 0.0, 1000, True, None),
            ("paused", "paused", 51.0, 0.0, 1000, False, 51),
            ("completed", "completed", 0.0, 100.0, 1000, False, 100),
            ("failed", "failed", 51.0, 0.0, 1000, False, 51),
            ("partial_failed", "partial_failed", 51.0, 0.0, 1000, False, 51),
            ("canceled", "canceled", 51.0, 0.0, 1000, False, 51),
        )

        for index, (status, stage, progress, stage_progress, total, busy, value) in enumerate(cases):
            with self.subTest(status=status, stage=stage, stage_progress=stage_progress):
                task = DownloadTask(
                    f"feedback-{index}",
                    f"https://example.com/feedback/{index}",
                    "D:/downloads",
                    status=status,
                    stage=stage,
                    stage_text="",
                    progress=progress,
                    stage_progress=stage_progress,
                    downloaded_bytes=int(total * progress / 100) if total else 0,
                    total_bytes=total,
                    speed="12.5 MiB/s",
                    eta="00:30",
                    stage_elapsed_seconds=3.0,
                    current_transcode_encoder="h264_nvenc" if stage == "transcoding" else "",
                )
                card = DownloadTaskCard(task)

                self.assertTrue(card.stage.text().strip())
                self.assertTrue(card.details.text().strip())
                self.assertEqual((card.progress.minimum(), card.progress.maximum()) == (0, 0), busy)
                if value is not None:
                    self.assertEqual(card.progress.value(), value)
                if busy:
                    self.assertTrue(card._activity_timer.isActive())
                    self.assertIn("当前阶段已用时", card.details.text())
                if stage in {"merging", "transcoding", "thumbnail", "metadata", "verifying"}:
                    self.assertNotIn("12.5 MiB/s", card.details.text())
                    self.assertNotIn("00:30", card.details.text())
                    self.assertNotIn("已下载", card.details.text())
                if stage == "transcoding" and stage_progress > 0:
                    self.assertIn("转换进度 42%", card.details.text())
                    self.assertIn("h264_nvenc", card.details.text())
                card.deleteLater()

        self.assertEqual(
            INDETERMINATE_TASK_STAGES,
            frozenset({
                "parsing", "formats", "waiting_disk", "merging", "thumbnail",
                "metadata", "verifying", "parsing_collection",
            }),
        )

    def test_audio_stream_progress_does_not_reuse_completed_video_percentage(self) -> None:
        task = DownloadTask(
            "separate-stream-progress",
            "https://example.com/separate-streams",
            "D:/downloads",
            status="downloading",
            stage="downloading_audio",
            progress=100.0,
            stage_progress=20.0,
            video_progress=100.0,
            audio_progress=20.0,
            downloaded_bytes=20,
            total_bytes=100,
        )

        card = DownloadTaskCard(task)

        self.assertEqual((card.progress.minimum(), card.progress.maximum()), (0, 100))
        self.assertEqual(card.progress.value(), 20)
        self.assertIn("视频 100% / 音频 20%", card.details.text())
        card.deleteLater()

    def test_retry_refreshes_cookie_source_from_current_settings(self) -> None:
        task = DownloadTask(
            "cookie-retry",
            "https://www.douyin.com/video/123",
            "D:/downloads",
            status="failed",
            cookie_source=COOKIE_SOURCE_NONE,
        )
        self.service.tasks[task.id] = task
        self.window.app_settings.set("download_cookie_source", COOKIE_SOURCE_EMBEDDED)

        self.page.retry_task_with_current_auth(task.id)

        self.assertEqual(task.cookie_source, COOKIE_SOURCE_EMBEDDED)
        self.assertEqual(self.service.retried, [task.id])

    def test_collection_resume_refreshes_auth_for_all_descendants(self) -> None:
        parent = DownloadTask(
            "auth-parent",
            "https://example.com/parent",
            "D:/downloads",
            task_kind="collection",
            status="paused",
            cookie_source=COOKIE_SOURCE_NONE,
        )
        nested = DownloadTask(
            "auth-nested",
            "https://example.com/nested",
            "D:/downloads",
            task_kind="collection",
            parent_task_id=parent.id,
            root_task_id=parent.id,
            status="paused",
            cookie_source=COOKIE_SOURCE_NONE,
        )
        child = DownloadTask(
            "auth-child",
            "https://example.com/child",
            "D:/downloads",
            parent_task_id=nested.id,
            root_task_id=parent.id,
            status="paused",
            cookie_source=COOKIE_SOURCE_NONE,
        )
        self.service.tasks = {
            task.id: task for task in (parent, nested, child)
        }
        self.window.app_settings.set(
            "download_cookie_source",
            COOKIE_SOURCE_EMBEDDED,
        )

        self.page.resume_task_with_current_auth(parent.id)

        self.assertEqual(self.service.resumed, [parent.id])
        self.assertTrue(all(
            task.cookie_source == COOKIE_SOURCE_EMBEDDED
            for task in (parent, nested, child)
        ))

    def test_context_menu_resume_uses_current_authentication(self) -> None:
        task = DownloadTask(
            "context-auth-resume",
            "https://example.com/context-auth-resume",
            "D:/downloads",
            status="paused",
            cookie_source=COOKIE_SOURCE_NONE,
        )
        self.service.tasks[task.id] = task
        self.window.app_settings.set(
            "download_cookie_source",
            COOKIE_SOURCE_EMBEDDED,
        )
        menu, actions = self.page.task_menu_controller.build(task)

        self.page.task_menu_controller.execute(
            task,
            actions,
            actions.pause_or_resume,
        )

        self.assertEqual(task.cookie_source, COOKIE_SOURCE_EMBEDDED)
        self.assertEqual(self.service.resumed, [task.id])
        menu.deleteLater()

    def test_redownload_refreshes_auth_before_cloning_task(self) -> None:
        task = DownloadTask(
            "context-auth-redownload",
            "https://example.com/context-auth-redownload",
            "D:/downloads",
            status="completed",
            cookie_source=COOKIE_SOURCE_NONE,
        )
        self.service.tasks[task.id] = task
        self.window.app_settings.set(
            "download_cookie_source",
            COOKIE_SOURCE_EMBEDDED,
        )

        result = self.page.redownload_task_with_current_auth(
            task.id,
            "custom",
        )

        self.assertEqual(task.cookie_source, COOKIE_SOURCE_EMBEDDED)
        self.assertEqual(
            self.service.redownloaded,
            [(task.id, "custom")],
        )
        self.assertEqual(result, f"redownload-{task.id}")

    def test_retry_collection_that_failed_before_children_restarts_probe(self) -> None:
        task = DownloadTask(
            "collection-probe-retry",
            "https://example.com/playlist",
            "D:/downloads",
            title="Playlist",
            task_kind="collection",
            status="failed",
            stage="failed",
            error="temporary parser error",
        )
        self.service.tasks[task.id] = task
        self.window.db = SimpleNamespace(collection_probe_entry_count=lambda _task_id: 7)

        with patch.object(
            self.page.collection_workflow,
            "resume",
        ) as resume_probes:
            self.page.retry_task_with_current_auth(task.id)

        self.assertEqual(task.status, "parsing_collection")
        self.assertEqual(task.error, "")
        self.assertEqual(self.service.probe_updates[0]["parsed_count"], 7)
        self.assertEqual(self.service.retried, [])
        resume_probes.assert_called_once_with()

    def test_context_menu_retries_childless_failed_collection_via_probe(self) -> None:
        task = DownloadTask(
            "context-collection-probe-retry",
            "https://example.com/playlist",
            "D:/downloads",
            title="Playlist",
            task_kind="collection",
            status="failed",
            stage="failed",
            error="temporary parser error",
        )
        self.service.tasks[task.id] = task
        self.window.db = SimpleNamespace(
            collection_probe_entry_count=lambda _task_id: 3,
        )
        menu, actions = self.page.task_menu_controller.build(task)

        with patch.object(
            self.page.collection_workflow,
            "resume",
        ) as resume_probes:
            self.page.task_menu_controller.execute(
                task,
                actions,
                actions.retry,
            )

        self.assertEqual(task.status, "parsing_collection")
        self.assertEqual(self.service.probe_updates[0]["parsed_count"], 3)
        self.assertEqual(self.service.retried, [])
        resume_probes.assert_called_once_with()
        menu.deleteLater()

    def test_embedded_login_persists_cookie_source_after_success(self) -> None:
        combo = QComboBox()
        combo.addItem("None", COOKIE_SOURCE_NONE)
        combo.addItem("Embedded", COOKIE_SOURCE_EMBEDDED)
        settings = _Settings(download_cookie_source=COOKIE_SOURCE_NONE)
        refreshed: list[bool] = []
        statuses: list[str] = []

        class PublishService:
            def __init__(self) -> None:
                self.calls: list[tuple[tuple, dict]] = []

            def run_account_action(self, *args, **kwargs) -> bool:
                self.calls.append((args, kwargs))
                return True

        publish_service = PublishService()
        parent = QWidget()
        self.addCleanup(parent.deleteLater)
        login_button = QPushButton("Open login page")
        window = SimpleNamespace(
            app_settings=settings,
            dashboard=SimpleNamespace(refresh_settings=lambda: refreshed.append(True)),
            publish_service=publish_service,
            run_sau_account_action=publish_service.run_account_action,
            settings_status=lambda message: statuses.append(message),
        )
        parent.window = window
        parent.download_cookie_source = combo
        parent.download_cookie_browser = QComboBox()
        parent.download_cookie_profile = QLineEdit()
        parent.download_cookie_keyring = QLineEdit()
        parent.download_cookie_container = QLineEdit()
        parent.download_cookie_file = QLineEdit()
        parent.open_cookie_login_button = login_button
        controller = DownloadCookieController(parent)

        controller.open_login()

        self.assertEqual(combo.currentData(), COOKIE_SOURCE_NONE)
        self.assertEqual(
            publish_service.calls[0][0],
            ("browser", "download", "login"),
        )
        self.assertEqual(
            publish_service.calls[0][1]["vault_profile_id"],
            EMBEDDED_DOWNLOAD_PROFILE,
        )
        self.assertFalse(login_button.isEnabled())
        self.assertEqual(settings.get("download_cookie_source"), COOKIE_SOURCE_NONE)
        opening_statuses = list(statuses)

        controller.login_result(
            "browser",
            "download",
            "login",
            True,
            "cookies saved",
        )

        self.assertEqual(combo.currentData(), COOKIE_SOURCE_EMBEDDED)
        self.assertEqual(settings.get("download_cookie_source"), COOKIE_SOURCE_EMBEDDED)
        self.assertEqual(settings.sync_count, 1)
        self.assertEqual(refreshed, [True])
        self.assertTrue(login_button.isEnabled())
        self.assertEqual(statuses, opening_statuses)

    def test_main_navigation_switches_between_text_and_icon_only_sidebar(self) -> None:
        navigation = SidebarNavigation()
        configure_main_navigation(navigation)
        first = QWidget()
        second = QWidget()
        navigation.addTab(first, "下载任务")
        navigation.addTab(second, "设置")

        self.assertEqual(navigation.objectName(), "mainNavigation")
        self.assertEqual(navigation.accessibleName(), "主导航")
        self.assertFalse(navigation.isCollapsed())
        self.assertEqual(navigation.sidebar.minimumWidth(), navigation.EXPANDED_WIDTH)
        self.assertEqual(
            navigation.navigationButton(0).toolButtonStyle(),
            Qt.ToolButtonTextBesideIcon,
        )
        navigation.navigationButton(1).click()
        self.assertIs(navigation.currentWidget(), second)

        navigation.collapse_button.click()
        self.assertTrue(navigation.isCollapsed())
        self.assertEqual(navigation.sidebar.maximumWidth(), navigation.COLLAPSED_WIDTH)
        self.assertEqual(
            navigation.navigationButton(0).toolButtonStyle(),
            Qt.ToolButtonIconOnly,
        )
        self.assertEqual(navigation.navigationButton(0).text(), "下载任务")
        self.assertEqual(navigation.navigationButton(0).toolTip(), "下载任务")
        self.assertEqual(navigation.collapse_button.accessibleName(), "展开导航")
        navigation.deleteLater()

    def test_navigation_icons_resolve_from_translated_or_source_labels(self) -> None:
        self.assertEqual(navigation_icon_key("Download Tasks"), "download")
        self.assertEqual(navigation_icon_key("下载任务"), "download")
        self.assertEqual(navigation_icon_key("设置"), "settings")

    def test_task_card_state_matrix_is_stable_and_actionable(self) -> None:
        cases = (
            ("queued", "queued", 0.0, "取消", True, 0),
            ("downloading", "parsing", 0.0, "暂停", True, None),
            ("downloading", "formats", 0.0, "暂停", True, None),
            ("waiting_selection", "waiting_selection", 0.0, "取消", True, 0),
            ("downloading", "waiting_disk", 0.0, "暂停", True, None),
            ("downloading", "downloading_video", 35.0, "暂停", True, 35),
            ("downloading", "reconnecting", 42.0, "暂停", True, 42),
            ("processing", "transcoding", 42.0, "取消", True, None),
            ("暂停中", "paused", 42.0, "取消", False, None),
            ("paused", "paused", 42.0, "继续", True, 42),
            ("canceling", "canceled", 42.0, "取消", False, None),
            ("failed", "failed", 42.0, "重试", True, 42),
            ("partial_failed", "partial_failed", 42.0, "重试", True, 42),
            ("canceled", "canceled", 42.0, "重试", True, 42),
            ("deleted", "completed", 100.0, "重新下载", True, 100),
            # Legacy/restored rows can lack the final percentage even though
            # the durable task status is already complete.
            ("completed", "completed", 0.0, "打开文件夹", True, 100),
        )
        for index, (status, stage, progress, action, enabled, expected_progress) in enumerate(cases):
            with self.subTest(status=status, stage=stage):
                task = DownloadTask(
                    f"matrix-{index}",
                    f"https://example.com/{index}",
                    "D:/downloads",
                    title=f"状态矩阵 {status}/{stage}",
                    status=status,
                    stage=stage,
                    stage_text="",
                    progress=progress,
                    stage_progress=(progress if stage in {"downloading_video", "downloading_audio"} else 0.0),
                    error="network timeout" if status == "failed" else "",
                )
                card = DownloadTaskCard(task)
                self.assertEqual(card.height(), TASK_CARD_HEIGHT)
                self.assertEqual(card.action.text(), action)
                self.assertEqual(card.action.isEnabled(), enabled)
                if expected_progress is None:
                    self.assertEqual((card.progress.minimum(), card.progress.maximum()), (0, 0))
                else:
                    self.assertEqual((card.progress.minimum(), card.progress.maximum()), (0, 100))
                    self.assertEqual(card.progress.value(), expected_progress)
                self.assertTrue(card.status.text().strip())
                self.assertTrue(card.stage.text().strip())
                self.assertTrue(card.pipeline.text().strip())
                self.assertTrue(card.details.text().strip())
                if status == "deleted":
                    self.assertIn("文件已不在保存位置", card.stage.text())
                    self.assertIn("重新下载", card.details.text())
                    self.assertNotIn("速度", card.details.text())
                elif status == "failed":
                    self.assertIn("network timeout", card.details.text())
                elif status == "partial_failed":
                    self.assertIn("部分合集项目下载失败", card.pipeline.text())
                    self.assertNotIn("● 解析", card.pipeline.text())
                elif status == "canceling":
                    self.assertEqual(card.stage.text(), "取消中")
                    self.assertIn("● 取消中", card.pipeline.text())
                elif status == "暂停中":
                    self.assertEqual(card.stage.text(), "暂停中")
                    self.assertIn("● 暂停中", card.pipeline.text())
                card.deleteLater()

    def test_task_card_long_text_elides_without_losing_full_tooltip(self) -> None:
        title = "非常长的视频标题" * 30
        url = "https://example.com/watch?v=" + "x" * 300
        task = DownloadTask(
            "long-text",
            url,
            "D:/downloads",
            title=title,
            status="downloading",
            stage="reconnecting",
            stage_text="网络中断，正在重连，服务器将在数秒后重新建立连接" * 4,
            progress=66.0,
            retry_count=2,
            retry_total=5,
        )
        card = DownloadTaskCard(task)
        card.resize(620, TASK_CARD_HEIGHT)
        card.show()
        self.app.processEvents()

        self.assertEqual(card.title.toolTip(), title)
        self.assertEqual(card.url.toolTip(), url)
        self.assertNotEqual(card.title.text(), title)
        self.assertNotEqual(card.url.text(), url)
        self.assertLessEqual(card.height(), TASK_CARD_HEIGHT)
        card.close()
        card.deleteLater()

    def test_task_card_media_identity_survives_resize_elision(self) -> None:
        task = DownloadTask(
            "media-identity",
            "https://example.com/original",
            "D:/downloads",
            title="解析阶段标题",
            status="completed",
        )
        card = DownloadTaskCard(task)
        media_title = "下载完成后的正式标题" * 12
        media_url = "https://example.com/watch?v=finished-media"
        card.update_media(MediaItem(
            title=media_title,
            source_url=media_url,
            uploader="示例上传者",
        ))

        card.resize(620, TASK_CARD_HEIGHT)
        card.show()
        self.app.processEvents()
        card.resize(760, TASK_CARD_HEIGHT)
        self.app.processEvents()

        self.assertEqual(card.title.toolTip(), media_title)
        self.assertIn(media_url, card.url.toolTip())
        self.assertIn("示例上传者", card.url.toolTip())
        self.assertEqual(card._title_text, media_title)
        self.assertEqual(card._url_text, media_url)
        card.close()
        card.deleteLater()

    def test_task_card_reuses_platform_presentation_until_url_changes(self) -> None:
        task = DownloadTask(
            "platform-cache",
            "https://example.com/first",
            "D:/downloads",
            title="平台缓存",
            status="downloading",
        )
        with patch("app.ui.task_card.detect_platform", return_value="other") as detect:
            card = DownloadTaskCard(task)
            for progress in (10.0, 20.0, 30.0):
                task.progress = progress
                card.update_task(task)
            task.url = "https://example.com/second"
            card.update_task(task)

        self.assertEqual(detect.call_count, 2)
        card.close()
        card.deleteLater()

    def test_task_card_survives_rapid_full_lifecycle_transitions(self) -> None:
        task = DownloadTask(
            "lifecycle",
            "https://example.com/lifecycle",
            "D:/downloads",
            title="完整生命周期模拟",
        )
        card = DownloadTaskCard(task)
        transitions = (
            ("queued", "queued", 0.0),
            ("downloading", "parsing", 0.0),
            ("downloading", "formats", 0.0),
            ("waiting_selection", "waiting_selection", 0.0),
            ("downloading", "waiting_disk", 0.0),
            ("downloading", "downloading_video", 28.0),
            ("downloading", "downloading_audio", 63.0),
            ("downloading", "reconnecting", 63.0),
            ("暂停中", "paused", 63.0),
            ("paused", "paused", 63.0),
            ("downloading", "downloading", 63.0),
            ("downloading", "merging", 0.0),
            ("downloading", "thumbnail", 0.0),
            ("downloading", "metadata", 0.0),
            ("downloading", "verifying", 0.0),
            ("failed", "failed", 63.0),
            # Retrying is the only lifecycle edge that should reset the
            # monotonic presentation snapshot.
            ("queued", "queued", 0.0),
            ("downloading", "downloading", 91.0),
            ("completed", "completed", 0.0),
            ("deleted", "completed", 100.0),
        )
        for status, stage, progress in transitions:
            task.status = status
            task.stage = stage
            task.stage_text = ""
            task.progress = progress
            task.downloaded_bytes = int(progress * 1024 * 1024)
            task.total_bytes = 100 * 1024 * 1024 if progress else 0
            card.update_task(task)
            self.assertTrue(card.status.text().strip())
            self.assertTrue(card.pipeline.text().strip())
            if (
                status in {"canceling", "暂停中"}
                or (status == "downloading" and stage in {"merging", "thumbnail", "metadata", "verifying"})
            ):
                self.assertEqual((card.progress.minimum(), card.progress.maximum()), (0, 0))
            else:
                self.assertGreaterEqual(card.progress.value(), 0)
                self.assertLessEqual(card.progress.value(), 100)

        self.assertEqual(card.progress.value(), 100)
        self.assertEqual(card.action.text(), "重新下载")
        self.assertIn("文件已不在保存位置", card.stage.text())
        card.deleteLater()

    def test_task_card_sanitizes_malformed_runtime_counters(self) -> None:
        task = DownloadTask(
            "malformed-runtime-counters",
            "https://example.com/malformed-runtime-counters",
            "D:/downloads",
            title="Malformed runtime counters",
            status="processing",
            stage="transcoding",
            options_json={
                "_storage_preview": {
                    "known": True,
                    "temporary_bytes": "not-a-number",
                    "final_bytes": float('inf'),
                    "temporary_dir": "D:/temp",
                    "final_dir": "D:/downloads",
                },
            },
        )
        task.progress = float('nan')
        task.stage_progress = "not-a-number"
        task.downloaded_bytes = "not-a-number"
        task.total_bytes = float('inf')
        task.visible_progress = float('inf')
        task.visible_downloaded_bytes = []
        task.visible_total_bytes = {}
        task.video_progress = "not-a-number"
        task.audio_progress = float('inf')
        task.retry_count = "not-a-number"
        task.elapsed_seconds = float('nan')
        task.stage_elapsed_seconds = float('inf')

        card = DownloadTaskCard(task)

        self.assertEqual((card.progress.minimum(), card.progress.maximum()), (0, 0))
        self.assertTrue(card.details.text())
        self.assertEqual(card.action.text(), ui_text('Cancel'))
        self.assertTrue(card.action.isEnabled())
        card.deleteLater()

    def test_collection_task_card_sanitizes_malformed_summary_counts(self) -> None:
        task = DownloadTask(
            "malformed-collection-summary",
            "https://example.com/collection",
            "D:/downloads",
            task_kind="collection",
            status="parsing_collection",
            stage="parsing_collection",
            options_json={
                "_collection": {
                    "parsed": "not-a-number",
                    "selected": float('inf'),
                    "completed": [],
                    "failed": {},
                    "queued": -9,
                    "skipped": None,
                },
            },
        )

        card = DownloadTaskCard(task)

        self.assertIn("0", card.stage.text())
        self.assertEqual(card.action.text(), ui_text('Cancel'))
        card.deleteLater()

    def test_all_task_filters_cover_processing_missing_and_terminal_states(self) -> None:
        statuses = {
            "downloading": "downloading",
            "canceling": "canceling",
            "queued": "queued",
            "paused": "paused",
            "pausing": "暂停中",
            "selection": "waiting_selection",
            "processing": "processing",
            "collection_parsing": "parsing_collection",
            "completed": "completed",
            "deleted": "deleted",
            "failed": "failed",
            "partial_failed": "partial_failed",
            "canceled": "canceled",
        }
        for task_id, status in statuses.items():
            task = DownloadTask(
                task_id,
                f"https://example.com/{task_id}",
                "D:/downloads",
                title=task_id,
                status=status,
                stage=status if status not in {"暂停中", "waiting_selection"} else (
                    "paused" if status == "暂停中" else "waiting_selection"
                ),
            )
            self.service.tasks[task_id] = task
            self.page.add_task(task)
        self.page.task_restore.set_loaded()

        expected = {
            "全部": set(statuses),
            "下载中": {"downloading", "canceling", "collection_parsing"},
            "排队中": {"queued"},
            "已暂停": {"paused", "pausing"},
            "处理中": {
                "canceling", "pausing", "selection", "processing", "collection_parsing",
            },
            "已完成": {"completed"},
            "文件已删除": {"deleted"},
            "失败": {"failed", "partial_failed", "canceled"},
        }
        for filter_name, expected_ids in expected.items():
            with self.subTest(filter_name=filter_name):
                self.page.filter_box.setCurrentText(filter_name)
                self.page.apply_filter()
                visible_ids = {
                    task_id
                    for task_id, item in self.page.items.items()
                    if not item.isHidden()
                }
                self.assertEqual(visible_ids, expected_ids)

    def test_status_sort_keeps_live_processing_before_terminal_history(self) -> None:
        tasks = [
            DownloadTask("deleted", "https://example.com/deleted", "D:/downloads", status="deleted"),
            DownloadTask("processing", "https://example.com/processing", "D:/downloads", status="processing"),
            DownloadTask("completed", "https://example.com/completed", "D:/downloads", status="completed"),
            DownloadTask("canceling", "https://example.com/canceling", "D:/downloads", status="canceling"),
            DownloadTask("downloading", "https://example.com/downloading", "D:/downloads", status="downloading"),
        ]
        self.page.sort_box.setCurrentIndex(self.page.sort_box.findData("status"))

        ordered = [
            task.id
            for task in ordered_top_level_tasks(tasks, "status")
        ]

        terminal_start = min(ordered.index("completed"), ordered.index("deleted"))
        self.assertLess(ordered.index("processing"), terminal_start)
        self.assertLess(ordered.index("canceling"), terminal_start)
        self.assertLess(ordered.index("downloading"), terminal_start)

    def test_resume_all_includes_partially_failed_collection(self) -> None:
        parent = DownloadTask(
            "partial-parent",
            "https://example.com/playlist",
            "D:/downloads",
            task_kind="collection",
            status="partial_failed",
            stage="partial_failed",
        )
        child = DownloadTask(
            "partial-child",
            "https://example.com/video",
            "D:/downloads",
            status="failed",
            parent_task_id=parent.id,
            root_task_id=parent.id,
        )
        self.service.tasks[parent.id] = parent
        self.service.tasks[child.id] = child

        self.page.add_tasks([parent, child])
        self.assertTrue(self.page.resume_all_button.isEnabled())
        self.page.resume_all()

        self.assertEqual(self.service.retried, [parent.id])

    def test_format_dialog_elides_long_title_but_keeps_tooltip(self) -> None:
        title = "超长视频标题 " * 80
        dialog = FormatSelectionDialog(
            title,
            "",
            [{"height": 1080, "ext": "mp4", "fps": "30", "format_note": "高清", "selector": "137"}],
        )
        heading = next(
            label
            for label in dialog.findChildren(__import__("PySide6.QtWidgets", fromlist=["QLabel"]).QLabel)
            if label.toolTip() == title
        )
        self.assertEqual(heading.toolTip(), title)
        self.assertIn("…", heading.text())
        dialog.close()

    def test_format_dialog_shows_structured_vertical_hdr_format_details(self) -> None:
        original_label = "1080p source · webm · vp9 · HDR"
        dialog = FormatSelectionDialog(
            "Vertical HDR video",
            "",
            [{
                "kind": "video",
                "label": original_label,
                "selector": "313+bestaudio/best",
                "height": 1080,
                "width": 1080,
                "source_height": 1920,
                "ext": "webm",
                "fps": "60",
                "codec": "vp9",
                "hdr": True,
                "has_audio": True,
                "language": "ja",
                "format_note": "Premium HDR",
            }],
        )

        row = dialog.list.itemWidget(dialog.list.item(0))
        label = row.findChild(QLabel)
        self.assertIn("1080p (1080×1920)", label.text())
        self.assertIn("60 " + ui_text('fps'), label.text())
        self.assertIn("HDR", label.text())
        self.assertEqual(label.text().count("HDR"), 1)
        self.assertIn(ui_text('Video + audio'), label.text())
        self.assertIn("ja", label.text())
        self.assertIn("Premium HDR", label.text())
        self.assertEqual(label.toolTip(), original_label)
        dialog.close()

    def test_format_dialog_tolerates_malformed_dimensions(self) -> None:
        dialog = FormatSelectionDialog(
            "Malformed extractor metadata",
            "",
            [{
                "kind": "video",
                "label": "Fallback format",
                "selector": "broken",
                "height": "not-a-number",
                "width": float('inf'),
                "source_height": [],
                "ext": "mp4",
            }],
        )

        row = dialog.list.itemWidget(dialog.list.item(0))
        label = row.findChild(QLabel)
        self.assertIn("Fallback format", label.text())
        self.assertEqual(label.toolTip(), "Fallback format")
        dialog.close()

    def test_format_dialog_disables_unavailable_content_modes(self) -> None:
        dialog = FormatSelectionDialog(
            "Video only",
            "",
            [{
                "kind": "video",
                "label": "1080p",
                "selector": "137+bestaudio/best",
                "height": 1080,
                "ext": "mp4",
            }],
        )

        audio_index = dialog.content_mode.findData('audio')
        audio_item = dialog.content_mode.model().item(audio_index)
        self.assertFalse(audio_item.isEnabled())
        self.assertTrue(dialog._ok_button.isEnabled())

        dialog._choices = []
        dialog._populate_choices()
        self.assertFalse(dialog._ok_button.isEnabled())
        self.assertIsNone(dialog.selected_choice())
        dialog.close()


if __name__ == "__main__":
    unittest.main()
