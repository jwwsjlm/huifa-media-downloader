from __future__ import annotations

import unittest
from types import MethodType, SimpleNamespace

from PySide6.QtCore import QCoreApplication, QTimer

from app.presentation.app_bridge import AppBridge
from app.presentation.completed_media_model import CompletedMediaModel
from app.presentation.distribution_plan import distribution_target_platforms
from app.storage.models import MediaItem


class _Database:
    def __init__(self) -> None:
        self.list_calls = 0

    def list_media(self, *, limit: int | None = None) -> list[MediaItem]:
        self.list_calls += 1
        return [MediaItem(id=1, title="Media")]

    def publish_statuses_for_media_ids(self, _media_ids: list[int]) -> dict:
        return {}


class _Settings:
    def get(self, _key: str) -> str:
        return ""


class CompletedMediaModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_complete_role_uses_configured_distribution_targets(self) -> None:
        model = CompletedMediaModel(_Database(), _Settings())
        targets = distribution_target_platforms("")
        model._entries = [
            (MediaItem(id=1, title="Media"), {target: "success" for target in targets})
        ]

        self.assertTrue(model.data(model.index(0, 0), model.CompleteRole))

    def test_explicit_refresh_loads_media_and_stops_pending_refresh(self) -> None:
        database = _Database()
        model = CompletedMediaModel(database, _Settings())
        timer = QTimer()
        bridge = SimpleNamespace(
            _completed_media_model=model,
            _completed_media_refresh_timer=timer,
        )
        bridge._refresh_completed_media_if_loaded = MethodType(
            AppBridge._refresh_completed_media_if_loaded, bridge
        )

        self.assertFalse(model.loaded)
        for expected_calls in (1, 2):
            timer.start(180)
            AppBridge.refreshCompletedMedia(bridge)

            self.assertTrue(model.loaded)
            self.assertFalse(model.dirty)
            self.assertEqual(model.rowCount(), 1)
            self.assertEqual(database.list_calls, expected_calls)
            self.assertFalse(timer.isActive())

    def test_background_refresh_keeps_unopened_media_unloaded(self) -> None:
        database = _Database()
        model = CompletedMediaModel(database, _Settings())
        timer = QTimer()
        bridge = SimpleNamespace(
            _completed_media_model=model,
            _completed_media_refresh_timer=timer,
        )

        AppBridge._schedule_completed_media_refresh(bridge)
        AppBridge._refresh_completed_media_if_loaded(bridge)

        self.assertFalse(model.loaded)
        self.assertTrue(model.dirty)
        self.assertEqual(database.list_calls, 0)
        self.assertFalse(timer.isActive())

    def test_background_refresh_schedules_loaded_media(self) -> None:
        database = _Database()
        model = CompletedMediaModel(database, _Settings())
        timer = QTimer()
        bridge = SimpleNamespace(
            _completed_media_model=model,
            _completed_media_refresh_timer=timer,
        )
        AppBridge.refreshCompletedMedia(bridge)

        AppBridge._schedule_completed_media_refresh(bridge)

        self.assertTrue(model.dirty)
        self.assertTrue(timer.isActive())
        self.assertEqual(database.list_calls, 1)

        AppBridge._refresh_completed_media_if_loaded(bridge)

        self.assertFalse(model.dirty)
        self.assertFalse(timer.isActive())
        self.assertEqual(database.list_calls, 2)


if __name__ == "__main__":
    unittest.main()
