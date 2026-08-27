from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QObject, Qt, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton

from app.storage.models import MediaItem
from app.ui.distribution_plan import (
    distribution_platform_states,
    distribution_preselected_platforms,
    distribution_target_platforms,
    serialize_distribution_target_platforms,
)
from app.ui.completed_page import (
    FILTER_ALL,
    FILTER_COMPLETE,
    FILTER_NEEDS_DISTRIBUTION,
    FILTER_PUBLISHED,
    FILTER_QUEUED,
    FILTER_RETRY_NEEDED,
    CompletedPage,
)
from app.ui.completed_media_card import CompletedMediaCard, resolved_media_platform
from app.ui.publish_editor import PublishPage
from app.ui.publish_queue import PublishQueuePage


class _Settings:
    def get(self, key: str) -> str:
        return "default" if key.startswith("publish_account/") else ""


class _PublishService(QObject):
    account_status = Signal(str, str, str, bool, str)

    def __init__(self) -> None:
        super().__init__()
        self.run_calls: list[int] = []
        self.retry_calls: list[int] = []

    def account_state(self, _platform: str, _account: str):
        return None

    def run_task(self, task_id: int) -> None:
        self.run_calls.append(int(task_id))

    def retry_task(self, task_id: int) -> None:
        self.retry_calls.append(int(task_id))


class _Window:
    def __init__(self) -> None:
        self.app_settings = _Settings()
        self.publish_service = _PublishService()
        self.account_hub = SimpleNamespace(platform_account_fields={})


class _EditorSettings:
    def __init__(self, error: Exception | None = None) -> None:
        self.values: dict[str, str] = {}
        self.sync_count = 0
        self.error = error

    def set_many(self, values: dict[str, str]) -> dict[str, str]:
        self.sync_count += 1
        if self.error is not None:
            raise self.error
        self.values.update(values)
        return dict(values)


class _EditorPublishService(_PublishService):
    def __init__(self, *, error: Exception | None = None) -> None:
        super().__init__()
        self.error = error
        self.create_calls = 0
        self.create_args: tuple[object, ...] | None = None
        self.last_created_count = 1
        self.last_existing_count = 0

    def create_tasks(self, *args) -> None:
        self.create_calls += 1
        self.create_args = args
        if self.error is not None:
            raise self.error


class _EditorWindow:
    def __init__(
        self,
        account_platforms: tuple[str, ...],
        *,
        error: Exception | None = None,
        settings_error: Exception | None = None,
    ) -> None:
        self.app_settings = _EditorSettings(settings_error)
        self.publish_service = _EditorPublishService(error=error)
        self.account_hub = SimpleNamespace(
            platform_account_fields={
                platform: QLineEdit("main")
                for platform in account_platforms
            }
        )
        self.publish_queue = SimpleNamespace(mark_dirty=lambda: None)
        self.completed = SimpleNamespace(mark_dirty=lambda: None)
        self.tabs = SimpleNamespace(setCurrentWidget=lambda _widget: None)
        self.completed_editors: list[object] = []
        self.publish_ui = SimpleNamespace(
            complete_editor=self.completed_editors.append,
        )


class _QueueDatabase:
    def __init__(self) -> None:
        self.rows = [
            {
                "id": 11, "media_id": 1, "platform": "douyin", "account": "main",
                "status": "failed", "title": "first", "result": "cookie expired",
            },
            {
                "id": 12, "media_id": 2, "platform": "youtube", "account": "main",
                "status": "pending", "title": "second", "result": "",
            },
        ]

    def count_publish_tasks(self, media_id=None) -> int:
        return sum(
            media_id is None or int(row["media_id"]) == int(media_id)
            for row in self.rows
        )

    def list_publish_tasks(self, limit=None, offset=0, media_id=None):
        rows = [
            row
            for row in self.rows
            if media_id is None or int(row["media_id"]) == int(media_id)
        ]
        return list(rows[offset:] if limit is None else rows[offset:offset + limit])

    def get_publish_task(self, task_id: int):
        return next((row for row in self.rows if int(row["id"]) == int(task_id)), None)


class _PagedQueueDatabase(_QueueDatabase):
    def __init__(self) -> None:
        self.rows = [
            {
                "id": index,
                "media_id": index,
                "platform": "youtube",
                "account": "main",
                "status": "pending" if index > 20 else "failed",
                "title": f"Task {index}",
                "result": "" if index > 20 else "cookie expired",
            }
            for index in range(120, 0, -1)
        ]

class _QueueWindow:
    def __init__(self) -> None:
        self.db = _QueueDatabase()
        self.publish_service = _PublishService()


class _CompletedSettings:
    def get(self, key: str) -> str:
        return "douyin,bilibili" if key == "publish_target_platforms" else ""


class _CompletedDatabase:
    def __init__(self) -> None:
        self.media = [
            MediaItem(id=index, title=f"Media {index}", uploader="Uploader", video_path=f"D:/missing-{index}.mp4")
            for index in range(1, 7)
        ]
        self.statuses = {
            2: {"douyin": "success"},
            3: {"douyin": "uploading"},
            4: {"douyin": "failed"},
            5: {"douyin": "success", "bilibili": "success"},
            6: {"douyin": "success", "bilibili": "uploading"},
        }

    def count_media(self):
        return len(self.media)

    def list_media(self, limit=None, offset=0):
        return list(self.media[offset:] if limit is None else self.media[offset:offset + limit])

    def get_media(self, media_id: int):
        return next(
            (media for media in self.media if media.id == int(media_id)),
            None,
        )

    def publish_statuses_by_media(self):
        return {media_id: dict(states) for media_id, states in self.statuses.items()}

    def publish_statuses_for_media(self, media_id: int):
        return dict(self.statuses.get(int(media_id), {}))

    def publish_statuses_for_media_ids(self, media_ids):
        return {
            int(media_id): dict(self.statuses.get(int(media_id), {}))
            for media_id in media_ids
            if int(media_id) in self.statuses
        }

    def media_distribution_counts(self, _platforms):
        total = len(self.media)
        published = 0
        active = 0
        failed = 0
        complete = 0
        for media in self.media:
            states = self.statuses.get(media.id, {})
            published += int(any(state == "success" for state in states.values()))
            active += int(any(state in {"pending", "uploading"} for state in states.values()))
            failed += int(any(state == "failed" for state in states.values()))
            complete += int(all(states.get(platform) == "success" for platform in ("douyin", "bilibili")))
        return {
            FILTER_ALL: total,
            FILTER_NEEDS_DISTRIBUTION: total - complete,
            FILTER_PUBLISHED: published,
            FILTER_QUEUED: active,
            FILTER_RETRY_NEEDED: failed,
            FILTER_COMPLETE: complete,
        }


class _CompletedWindow:
    def __init__(self) -> None:
        self.app_settings = _CompletedSettings()
        self.db = _CompletedDatabase()


class DistributionPlanUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        existing = QCoreApplication.instance()
        if existing is not None and not isinstance(existing, QApplication):
            raise unittest.SkipTest("a non-GUI QCoreApplication already exists")
        cls.app = existing or QApplication([])

    def test_empty_setting_means_all_and_unknown_keys_are_filtered(self) -> None:
        supported = ("douyin", "bilibili", "youtube")
        self.assertEqual(distribution_target_platforms("", supported), supported)
        self.assertEqual(
            distribution_target_platforms("unknown,youtube,douyin", supported),
            ("douyin", "youtube"),
        )
        self.assertEqual(distribution_target_platforms("unknown", supported), supported)
        self.assertEqual(serialize_distribution_target_platforms(supported, supported), "")
        self.assertEqual(
            serialize_distribution_target_platforms({"youtube", "douyin", "unknown"}, supported),
            "douyin,youtube",
        )
        with self.assertRaises(ValueError):
            serialize_distribution_target_platforms(set(), supported)

    def test_coverage_and_preselection_use_only_target_platforms(self) -> None:
        targets = ("douyin", "bilibili", "youtube")
        states = {
            "douyin": "success",
            "bilibili": "failed",
            "youtube": "uploading",
            "removed-platform": "success",
        }
        self.assertEqual(
            distribution_platform_states(states, targets),
            {"douyin": "success", "bilibili": "failed", "youtube": "uploading"},
        )
        self.assertEqual(distribution_preselected_platforms(states, targets), ())

        card = CompletedMediaCard(MediaItem(id=1, title="demo"), states, targets)
        self.assertIn("平台覆盖 1/3", card.distribution.text())
        self.assertNotIn("removed-platform", card.distribution.toolTip())
        self.assertEqual(card.publish_button.text(), "处理失败任务（1）")
        queued = []
        card.queue_requested.connect(queued.append)
        card.publish_button.click()
        self.assertEqual(queued, [1])
        card.close()

    def test_completed_card_falls_back_to_stored_platform_for_generic_urls(self) -> None:
        media = MediaItem(
            id=9,
            title="fallback",
            source_url="https://example.test/watch/9",
            source_platform="youtube",
        )

        self.assertEqual(resolved_media_platform(media), "youtube")
        card = CompletedMediaCard(media, {}, ("youtube",))
        self.assertIn("YouTube", card.source_icon.toolTip())
        card.close()

    def test_new_targets_and_failed_tasks_have_separate_actions(self) -> None:
        targets = ("douyin", "bilibili")
        states = {"douyin": "failed"}
        self.assertEqual(distribution_preselected_platforms(states, targets), ("bilibili",))
        card = CompletedMediaCard(MediaItem(id=7, title="demo"), states, targets)
        self.assertEqual(card.publish_button.text(), "继续分发（1）")
        queued = []
        card.queue_requested.connect(queued.append)
        failed_button = next(
            button for button in card.findChildren(QPushButton)
            if button.text() == "处理失败任务（1）"
        )
        failed_button.click()
        self.assertEqual(queued, [7])
        card.close()

    def test_completed_targets_disable_primary_action_and_queue_scope_filters_media(self) -> None:
        card = CompletedMediaCard(
            MediaItem(id=1, title="done"),
            {"douyin": "success", "bilibili": "success"},
            ("douyin", "bilibili"),
        )
        self.assertEqual(card.publish_button.text(), "目标平台已完成")
        self.assertFalse(card.publish_button.isEnabled())
        card.close()

        page = PublishQueuePage(_QueueWindow())
        page.focus_media(MediaItem(id=2, title="second"))
        while page._queue_render_timer.isActive():
            self.app.processEvents()
        self.assertEqual(set(page.items), {12})
        self.assertFalse(page.items[12].isHidden())
        page.clear_media_filter()
        while page._queue_render_timer.isActive():
            self.app.processEvents()
        self.assertEqual(set(page.items), {11, 12})
        self.assertTrue(all(not item.isHidden() for item in page.items.values()))
        page.close()

    def test_publish_queue_actions_stay_in_top_toolbar_at_minimum_window_width(self) -> None:
        page = PublishQueuePage(_QueueWindow())
        page.resize(704, 560)
        page.show()
        page.focus_media(MediaItem(
            id=2,
            title="A long scoped video title used to exercise the compact toolbar layout",
        ))
        self.app.processEvents()

        for button in (page.refresh_button, page.run_button, page.retry_button):
            self.assertLessEqual(button.geometry().bottom(), page.queue_stack.geometry().top())
            self.assertLessEqual(button.geometry().right(), page.rect().right())
        self.assertLess(page.search_box.geometry().right(), page.refresh_button.geometry().left())
        self.assertFalse(page.tree.rootIsDecorated())
        self.assertEqual(page.tree.indentation(), 0)
        page.close()

    def test_publish_page_checks_only_requested_supported_platforms(self) -> None:
        page = PublishPage(
            _Window(),
            MediaItem(id=1, title="demo"),
            ("bilibili", "youtube", "removed-platform"),
        )
        checked = {
            page.platforms.item(index).data(Qt.UserRole)
            for index in range(page.platforms.count())
            if page.platforms.item(index).checkState() == Qt.Checked
        }
        self.assertEqual(checked, {"bilibili", "youtube"})
        page.close()

    def test_publish_editor_reports_partially_missing_account_fields(self) -> None:
        window = _EditorWindow(("youtube",))
        page = PublishPage(
            window,
            MediaItem(id=1, title="demo"),
            ("youtube", "bilibili"),
        )
        with patch("app.ui.publish_editor.QMessageBox.warning") as warning:
            page.submit()

        self.assertEqual(window.publish_service.create_calls, 0)
        warning.assert_called_once()
        self.assertIn("哔哩哔哩", str(warning.call_args.args[2]))
        page.close()

    def test_publish_editor_restores_submit_button_after_create_failure(self) -> None:
        window = _EditorWindow(("youtube",), error=RuntimeError("database unavailable"))
        page = PublishPage(
            window,
            MediaItem(id=1, title="demo"),
            ("youtube",),
        )
        with patch("app.ui.publish_editor.QMessageBox.warning") as warning:
            page.submit()

        self.assertEqual(window.publish_service.create_calls, 1)
        self.assertTrue(page.submit_button.isEnabled())
        self.assertEqual(window.app_settings.values, {})
        warning.assert_called_once()
        self.assertIn("database unavailable", str(warning.call_args.args[2]))
        page.close()

    def test_publish_editor_keeps_created_tasks_when_preference_sync_fails(self) -> None:
        window = _EditorWindow(
            ("youtube",),
            settings_error=RuntimeError("settings file is read-only"),
        )
        page = PublishPage(
            window,
            MediaItem(id=1, title="demo"),
            ("youtube",),
        )
        with (
            patch("app.ui.publish_editor.QMessageBox.warning") as warning,
            patch("app.ui.publish_editor.QMessageBox.information") as information,
        ):
            page.submit()

        self.assertEqual(window.publish_service.create_calls, 1)
        self.assertTrue(page.submit_button.isEnabled())
        warning.assert_called_once()
        information.assert_not_called()
        self.assertIn("发布任务已经创建", str(warning.call_args.args[2]))
        self.assertIn("settings file is read-only", str(warning.call_args.args[2]))
        self.assertEqual(window.completed_editors, [page])
        page.close()

    def test_publish_editor_does_not_submit_not_recorded_as_source_ip(self) -> None:
        window = _EditorWindow(("douyin",))
        page = PublishPage(
            window,
            MediaItem(id=1, title="demo", source_ip=""),
            ("douyin",),
        )
        try:
            with patch("app.ui.publish_editor.QMessageBox.information"):
                page.submit()

            self.assertEqual(page.source_ip.text(), "")
            self.assertTrue(page.source_ip.placeholderText())
            settings = window.publish_service.create_args[3]
            self.assertEqual(settings["douyin"]["source_ip"], "")
        finally:
            page.close()

    def test_publish_editor_applies_schedule_only_to_supported_platforms(self) -> None:
        window = _EditorWindow(("douyin", "youtube"))
        page = PublishPage(
            window,
            MediaItem(id=1, title="demo"),
            ("douyin", "youtube"),
        )
        page.schedule.setText("2026-08-27 09:30")
        try:
            with patch("app.ui.publish_editor.QMessageBox.information"):
                page.submit()

            settings = window.publish_service.create_args[3]
            self.assertEqual(settings["douyin"]["schedule"], "2026-08-27 09:30")
            self.assertNotIn("schedule", settings["youtube"])
            self.assertIn("visibility", settings["youtube"])
            self.assertNotIn("visibility", settings["douyin"])
        finally:
            page.close()

    def test_publish_editor_rejects_invalid_schedule_before_task_creation(self) -> None:
        window = _EditorWindow(("douyin",))
        page = PublishPage(
            window,
            MediaItem(id=1, title="demo"),
            ("douyin",),
        )
        page.schedule.setText("2026-02-30 09:30")
        try:
            with patch("app.ui.publish_editor.QMessageBox.warning") as warning:
                page.submit()

            self.assertEqual(window.publish_service.create_calls, 0)
            warning.assert_called_once()
            self.assertIn("YYYY-MM-DD HH:MM", str(warning.call_args.args[2]))
        finally:
            page.close()

    def test_publish_editor_ignores_hidden_schedule_after_platform_switch(self) -> None:
        window = _EditorWindow(("douyin", "youtube"))
        page = PublishPage(
            window,
            MediaItem(id=1, title="demo"),
            ("douyin",),
        )
        page.schedule.setText("not-a-date")
        for index in range(page.platforms.count()):
            item = page.platforms.item(index)
            platform = item.data(Qt.UserRole)
            item.setCheckState(Qt.Checked if platform == "youtube" else Qt.Unchecked)
        try:
            with patch("app.ui.publish_editor.QMessageBox.information"):
                page.submit()

            self.assertEqual(window.publish_service.create_calls, 1)
            settings = window.publish_service.create_args[3]
            self.assertNotIn("schedule", settings["youtube"])
        finally:
            page.close()

    def test_completed_page_metrics_filter_real_distribution_states(self) -> None:
        page = CompletedPage(_CompletedWindow())
        page.resize(1280, 760)
        page.show()
        page.refresh()
        while page._media_render_timer.isActive():
            self.app.processEvents()

        self.assertEqual(
            {name: card.value.text() for name, card in page.metric_cards.items()},
            {
                FILTER_ALL: "6",
                FILTER_NEEDS_DISTRIBUTION: "5",
                FILTER_PUBLISHED: "3",
                FILTER_QUEUED: "2",
                FILTER_RETRY_NEEDED: "1",
                FILTER_COMPLETE: "1",
            },
        )
        self.assertEqual(page.summary.text(), "6 个视频")

        QTest.mouseClick(page.metric_cards[FILTER_RETRY_NEEDED], Qt.LeftButton)
        self.app.processEvents()

        self.assertEqual(page.filter_box.currentText(), "待重试")
        visible_ids = {
            media_id
            for media_id, item in page.items.items()
            if not item.isHidden()
        }
        self.assertEqual(visible_ids, {4})
        self.assertTrue(bool(page.metric_cards[FILTER_RETRY_NEEDED].property("active")))
        page.close()

    def test_completed_page_initial_load_uses_one_bounded_media_page(self) -> None:
        window = _CompletedWindow()
        window.db = _CompletedDatabase()
        for index in range(7, 127):
            window.db.media.append(
                MediaItem(id=index, title=f"Media {index}", video_path=f"D:/missing-{index}.mp4")
            )
        page = CompletedPage(window)
        page.resize(1280, 760)
        page.show()
        page.refresh()
        while page._media_render_timer.isActive():
            self.app.processEvents()
        self.assertEqual(page._media_total, 126)
        self.assertEqual(len(page._media_catalog), 50)
        self.assertEqual(len(page.items), 50)
        self.assertTrue(page.load_more_button.isVisible())
        page.close()

    def test_completed_page_search_waits_for_debounce_before_filtering(self) -> None:
        page = CompletedPage(_CompletedWindow())
        page.resize(1280, 760)
        page.show()
        page.refresh()
        while page._media_render_timer.isActive():
            self.app.processEvents()

        page.search_box.setText("Media 4")
        self.app.processEvents()
        self.assertTrue(page._search_filter_timer.isActive())
        self.assertEqual(
            {media_id for media_id, item in page.items.items() if not item.isHidden()},
            {1, 2, 3, 4, 5, 6},
        )

        QTest.qWait(350)
        self.app.processEvents()
        self.assertFalse(page._search_filter_timer.isActive())
        self.assertEqual(
            {media_id for media_id, item in page.items.items() if not item.isHidden()},
            {4},
        )
        page.close()

    def test_completed_page_does_not_open_working_directory_for_missing_path(self) -> None:
        page = CompletedPage(_CompletedWindow())
        with (
            patch.object(
                page.window.db,
                "get_media",
                return_value=MediaItem(id=1, video_path=""),
            ),
            patch("app.ui.completed_page.os.startfile") as startfile,
            patch("app.ui.completed_page.QMessageBox.warning") as warning,
        ):
            page.open_folder(1)

        startfile.assert_not_called()
        warning.assert_called_once()
        self.assertIn("未记录视频路径", str(warning.call_args.args[2]))
        page.close()

    def test_live_publish_updates_replace_only_affected_completed_card(self) -> None:
        window = _CompletedWindow()
        page = CompletedPage(window)
        page.resize(1280, 760)
        page.show()
        page.refresh()
        while page._media_render_timer.isActive():
            self.app.processEvents()

        affected_item = page.items[4]
        affected_card = page.cards[4]
        untouched_card = page.cards[2]
        window.db.statuses[4] = {"douyin": "success"}
        page.refresh_media_distribution(4)
        self.app.processEvents()

        self.assertIs(page.items[4], affected_item)
        self.assertIsNot(page.cards[4], affected_card)
        self.assertIs(page.cards[2], untouched_card)
        self.assertIn("平台覆盖 1/2", page.cards[4].distribution.text())
        self.assertEqual(page.metric_cards[FILTER_RETRY_NEEDED].value.text(), "0")
        self.assertFalse(page.dirty)
        page.close()

    def test_live_publish_queue_update_reuses_existing_tree_item(self) -> None:
        window = _QueueWindow()
        page = PublishQueuePage(window)
        page.show()
        page.refresh()
        while page._queue_render_timer.isActive():
            self.app.processEvents()
        original = page.items[11]
        window.db.rows[0] = {
            **window.db.rows[0],
            "status": "success",
            "result": "published",
        }

        page.refresh_task(11)

        self.assertIs(page.items[11], original)
        self.assertEqual(original.text(3), "已成功")
        self.assertEqual(original.text(5), "published")
        self.assertFalse(page.dirty)
        page.close()

    def test_publish_queue_search_is_debounced_and_matches_localized_history(self) -> None:
        window = _QueueWindow()
        window.db = _PagedQueueDatabase()
        page = PublishQueuePage(window)
        page.show()
        page.refresh()
        while page._queue_render_timer.isActive():
            self.app.processEvents()
        self.assertEqual(len(page.items), 100)

        page.search_box.setText("失败")
        self.app.processEvents()
        self.assertTrue(page._search_filter_timer.isActive())
        self.assertEqual(
            sum(not item.isHidden() for item in page.items.values()),
            100,
        )

        QTest.qWait(350)
        while page._queue_render_timer.isActive():
            self.app.processEvents()
        self.app.processEvents()
        self.assertFalse(page._search_filter_timer.isActive())
        self.assertEqual(
            {task_id for task_id, item in page.items.items() if not item.isHidden()},
            set(range(1, 21)),
        )
        page.close()

    def test_publish_queue_ignores_live_updates_outside_media_scope(self) -> None:
        window = _QueueWindow()
        page = PublishQueuePage(window)
        page.show()
        page.focus_media(MediaItem(id=1, title="first"))
        while page._queue_render_timer.isActive():
            self.app.processEvents()
        original_total = page._queue_total
        self.assertNotIn(12, page.items)
        window.db.rows[1] = {**window.db.rows[1], "status": "success"}

        page.refresh_task(12, window.db.rows[1])

        self.assertEqual(page._queue_total, original_total)
        self.assertNotIn(12, page.items)
        page.close()

    def test_publish_queue_focus_task_clears_filters_and_reveals_notification_target(self) -> None:
        window = _QueueWindow()
        page = PublishQueuePage(window)
        page.show()
        page.focus_media(MediaItem(id=1, title="first"))
        page.search_box.setText("first")
        QTest.qWait(350)

        item = page.focus_task(12)

        self.assertIsNotNone(item)
        self.assertEqual(page._media_filter_id, 0)
        self.assertEqual(page.search_box.text(), "")
        self.assertIs(page.tree.currentItem(), item)
        self.assertFalse(item.isHidden())
        page.close()

    def test_publish_queue_actions_follow_selected_task_status_without_rebuilding(self) -> None:
        window = _QueueWindow()
        page = PublishQueuePage(window)
        page.show()
        page.refresh()
        while page._queue_render_timer.isActive():
            self.app.processEvents()
        failed_item = page.items[11]
        pending_item = page.items[12]

        self.assertFalse(page.run_button.isEnabled())
        self.assertFalse(page.retry_button.isEnabled())
        page.tree.setCurrentItem(failed_item)
        self.assertTrue(page.run_button.isEnabled())
        self.assertTrue(page.retry_button.isEnabled())
        page.retry_button.click()
        self.assertEqual(window.publish_service.retry_calls, [11])
        self.assertIs(page.items[11], failed_item)

        page.tree.setCurrentItem(pending_item)
        self.assertTrue(page.run_button.isEnabled())
        self.assertFalse(page.retry_button.isEnabled())
        page.run_button.click()
        self.assertEqual(window.publish_service.run_calls, [12])
        self.assertIs(page.items[12], pending_item)
        page.close()


if __name__ == "__main__":
    unittest.main()
