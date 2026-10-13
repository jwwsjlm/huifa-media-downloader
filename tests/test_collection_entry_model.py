from __future__ import annotations

import unittest
from contextlib import contextmanager
from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication

from app.core.cookie_sources import CookieSource
from app.core.download_service import DownloadService
from app.presentation.collection_entry_model import CollectionEntryModel


class _Database:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.entries = [
            {"index": 1, "title": "Alpha", "uploader": "Alice", "url": "https://example.test/a"},
            {"index": 2, "title": "Beta", "uploader": "Bob", "url": "https://example.test/b"},
        ]

    def list_collection_probe_entries(self, _parent_task_id: str, **options: object) -> list[dict[str, object]]:
        query = str(options.get("query") or "").casefold()
        self.queries.append(query)
        return [
            dict(entry)
            for entry in self.entries
            if not query or query in " ".join(map(str, entry.values())).casefold()
        ]

    def count_selected_collection_probe_entries(self, _parent_task_id: str) -> int:
        return 0


class _DownloadService:
    def set_collection_probe_entry_selected(
        self,
        _parent_task_id: str,
        _collection_index: int,
        _selected: bool,
    ) -> bool:
        return True

    def set_collection_probe_selection(self, _parent_task_id: str, _mode: str) -> bool:
        return True


class _ProbeDatabase:
    def __init__(self) -> None:
        self.writes: list[tuple[str, list[dict[str, object]]]] = []

    def upsert_collection_probe_entries(
        self,
        parent_id: str,
        entries: list[dict[str, object]],
    ) -> None:
        self.writes.append((parent_id, entries))

    def collection_probe_entry_count(self, parent_id: str) -> int:
        return sum(len(entries) for stored_id, entries in self.writes if stored_id == parent_id)


class _ProbeService:
    def __init__(self) -> None:
        self.db = _ProbeDatabase()
        self.tasks = {
            "collection": SimpleNamespace(
                id="collection",
                task_kind="collection",
                status="downloading",
                cookie_source="none",
                cookie_file="",
                cookie_browser="chrome",
                cookie_profile="",
                cookie_keyring="",
                cookie_container="",
            ),
            "video": SimpleNamespace(
                id="video",
                task_kind="video",
                status="queued",
                cookie_source="none",
                cookie_file="",
                cookie_browser="chrome",
                cookie_profile="",
                cookie_keyring="",
                cookie_container="",
            ),
        }
        self.task_updated = SimpleNamespace(emit=lambda _task: None)

    @contextmanager
    def _durable_task_mutations(self, _tasks: object):
        yield

    def _collection_tree_tasks(self, _task_id: str) -> list[object]:
        return [self.tasks["collection"], self.tasks["video"]]


class CollectionEntryModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_search_exposes_only_matching_entries(self) -> None:
        database = _Database()
        model = CollectionEntryModel(database, _DownloadService())
        model.setParentTaskId("collection-1")
        model.setFilterText("beta")

        self.assertEqual(database.queries[-1], "beta")
        self.assertEqual(model.rowCount(), 1)
        self.assertEqual(model.count, 1)
        self.assertEqual(
            model.data(model.index(0, 0), CollectionEntryModel.TitleRole),
            "Beta",
        )

    def test_probe_entries_require_a_live_collection_parent(self) -> None:
        service = _ProbeService()

        count = DownloadService.store_collection_probe_entries(
            service,
            "collection",
            [{"index": 1}],
        )

        self.assertEqual(count, 1)
        self.assertEqual(service.db.writes, [("collection", [{"index": 1}])])
        self.assertIsNone(
            DownloadService.store_collection_probe_entries(service, "video", [{"index": 2}])
        )

        self.assertEqual(
            DownloadService.persist_interrupted_tasks(service, {"collection"}),
            1,
        )
        self.assertEqual(service.tasks["collection"].status, "paused")

    def test_task_authentication_is_updated_through_the_service(self) -> None:
        service = _ProbeService()

        changed = DownloadService.set_task_authentication(
            service,
            "collection",
            CookieSource(
                source="browser",
                browser="edge",
                profile="Profile 1",
                keyring="key",
                container="container",
            ),
        )

        self.assertTrue(changed)
        for task in service.tasks.values():
            self.assertEqual(task.cookie_source, "browser")
            self.assertEqual(task.cookie_browser, "edge")
            self.assertEqual(task.cookie_profile, "Profile 1")


if __name__ == "__main__":
    unittest.main()
