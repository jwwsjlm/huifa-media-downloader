from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("HUIFA_UI_LOCALE", "zh-CN")
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from app.core.app_settings import AppSettings
from app.core.download_service import DownloadTask
from app.storage.models import MediaItem
from app.ui.completed_page import CompletedPage
from app.ui.dashboard_page import DashboardPage
from app.ui.runtime import create_application


class FakeService:
    def __init__(self) -> None:
        self.tasks: dict[str, DownloadTask] = {}
        self.workers: dict[str, object] = {}

    cancel = pause = resume = retry = lambda self, _task_id: None

    def task_statistics(self, *, top_level_only: bool = False) -> dict[str, int]:
        tasks = [
            task for task in self.tasks.values()
            if not top_level_only or not task.parent_task_id
        ]
        return {
            "total": len(tasks),
            "active": sum(
                task.status in {"downloading", "parsing_collection", "canceling"}
                for task in tasks
            ),
            "queued": sum(task.status == "queued" for task in tasks),
            "paused": sum(task.status in {"paused", "暂停中"} for task in tasks),
            "processing": sum(task.status == "processing" for task in tasks),
            "completed": sum(task.status == "completed" for task in tasks),
            "failed": sum(
                task.status in {"failed", "partial_failed", "canceled"}
                for task in tasks
            ),
        }

    def has_task_status(
        self,
        statuses: set[str],
        *,
        top_level_only: bool = False,
    ) -> bool:
        return any(
            task.status in statuses
            and (not top_level_only or not task.parent_task_id)
            for task in self.tasks.values()
        )


def main() -> int:
    app, _font = create_application([])
    service = FakeService()
    window = SimpleNamespace(
        app_settings=AppSettings(),
        download_service=service,
        tabs=SimpleNamespace(setCurrentWidget=lambda _widget: None),
        settings=object(),
    )
    page = DashboardPage(window)
    page.set_tasks_loaded()

    # Repeated add/update/filter/delete cycles exercise the item-widget
    # ownership paths that previously caused blank rows and native crashes.
    for index in range(40):
        task = DownloadTask(
            f"stress-{index}",
            f"https://www.youtube.com/watch?v={index:011d}",
            "D:/tmp",
            title=f"Stress task {index}",
            status="queued" if index % 2 else "paused",
        )
        service.tasks[task.id] = task
        page.add_task(task)
        task.title = f"Updated {index}"
        page.update_task(task)
        page.sort_tasks()
        page.apply_filter()

    app.processEvents()
    assert page.task_list.count() == 40
    assert len(page.items) == len(page.cards) == 40
    assert all(page.task_list.itemWidget(page.items[task_id]) is page.cards[task_id] for task_id in page.items)

    # Selection and context-menu lookup must remain harmless with many rows.
    page.select_task_from_card("stress-3", Qt.KeyboardModifiers())
    assert page.selected_task_ids() == ["stress-3"]
    for task_id in list(service.tasks):
        page.remove_task(task_id)
        service.tasks.pop(task_id, None)
    app.processEvents()
    assert page.task_list.count() == 0
    assert not page.items and not page.cards

    # A large restored history must not allocate hundreds of QWidget cards on
    # the startup event. Only the first page is materialized; another page is
    # loaded on demand, and search can promote a matching unloaded task.
    restored = []
    for index in range(500):
        task = DownloadTask(
            f"history-{index}",
            f"https://example.com/history/{index}",
            "D:/tmp",
            title="special-oldest" if index == 0 else f"History {index}",
            status="completed",
            created_at=f"2024-01-01T00:00:{index:06d}",
        )
        service.tasks[task.id] = task
        restored.append(task)
    page.begin_task_restore(restored)
    while page._task_render_timer.isActive():
        app.processEvents()
    assert page.task_list.count() == 50
    assert len(page.cards) == 50
    assert not page.load_more_button.isHidden()

    page.load_more_tasks()
    while page._task_render_timer.isActive():
        app.processEvents()
    assert page.task_list.count() == 100

    page.search_box.setText("special-oldest")
    page.apply_filter()
    while page._task_render_timer.isActive():
        app.processEvents()
    assert "history-0" in page.cards
    assert not page.items["history-0"].isHidden()

    service.tasks.clear()
    page.clear_tasks()
    page.close()

    media_items = [
        MediaItem(
            id=index + 1,
            source_url=f"https://example.com/media/{index}",
            title="special-media" if index == 299 else f"Media {index}",
            uploader="Uploader",
            video_path=f"D:/tmp/media-{index}.mp4",
        )
        for index in range(300)
    ]

    class FakeDatabase:
        def list_media(self):
            return list(reversed(media_items))

        def publish_statuses_by_media(self):
            return {1: {"youtube": "success"}}

    completed = CompletedPage(SimpleNamespace(db=FakeDatabase()))
    completed.refresh()
    while completed._media_render_timer.isActive():
        app.processEvents()
    assert completed.list.count() == 50
    assert len(completed.cards) == 50
    assert not completed.load_more_button.isHidden()

    completed.search_box.setText("special-media")
    completed.apply_filter()
    while completed._media_render_timer.isActive():
        app.processEvents()
    assert 300 in completed.cards
    assert not completed.items[300].isHidden()
    completed.close()
    app.processEvents()
    print("ui_stress=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
