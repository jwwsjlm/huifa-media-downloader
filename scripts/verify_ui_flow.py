from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("HUIFA_UI_LOCALE", "zh-CN")
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QAbstractItemView, QDialog, QPushButton, QWidget

from app.core.app_settings import AppSettings
from app.core.download_service import DownloadTask
from app.core.update_service import UpdateService
from app.storage.models import MediaItem
from app.ui.completed_media_card import CompletedMediaCard
from app.ui.dashboard_page import DashboardPage
from app.ui.runtime_components_dialog import UpdateDialog
from app.ui.runtime import create_application


class FakeService:
    def __init__(self) -> None:
        self.tasks: dict[str, DownloadTask] = {}
        self.workers: dict[str, object] = {}

    def cancel(self, _task_id: str) -> None: pass
    def pause(self, _task_id: str) -> None: pass
    def resume(self, _task_id: str) -> None: pass
    def retry(self, _task_id: str) -> None: pass

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
    fake_window = SimpleNamespace(
        app_settings=AppSettings(),
        download_service=service,
        tabs=SimpleNamespace(setCurrentWidget=lambda _widget: None),
        settings=object(),
    )
    page = DashboardPage(fake_window)
    page.set_tasks_loaded()
    app.processEvents()
    assert page.task_list.count() == 0
    assert page.task_list.selectionMode() == QAbstractItemView.NoSelection
    assert page.task_list.itemAt(QPoint(4, 4)) is None

    tasks = [
        DownloadTask("ui-a", "https://www.youtube.com/watch?v=demo", "D:/tmp", title="视频 A", status="paused"),
        DownloadTask("ui-b", "https://example.com/b", "D:/tmp", title="视频 B", status="queued"),
    ]
    for task in tasks:
        service.tasks[task.id] = task
        page.add_task(task)
    app.processEvents()
    assert page.task_list.count() == 2
    assert page.cards["ui-a"].title.text() == "视频 A"
    assert page.cards["ui-b"].url.text() == "https://example.com/b"
    assert "已暂停" in page.cards["ui-a"].pipeline.text()
    assert "等待开始" in page.cards["ui-b"].pipeline.text()
    assert not page.cards["ui-a"].platform_icon.pixmap().isNull()
    assert "YouTube" in page.cards["ui-a"].platform_icon.toolTip()
    page.select_task_from_card("ui-a", Qt.KeyboardModifiers())
    assert page.selected_task_ids() == ["ui-a"]

    page.remove_task("ui-a")
    page.remove_task("ui-b")
    app.processEvents()
    assert page.task_list.count() == 0
    assert page.task_list.selectionMode() == QAbstractItemView.NoSelection
    page.task_context_menu(QPoint(4, 4))
    page.close()
    app.processEvents()

    # The update dialog must explain where a detected tool comes from and make
    # the independently updatable external yt-dlp option explicit.
    with tempfile.TemporaryDirectory() as directory:
        class UpdateHost(QWidget):
            def __init__(self) -> None:
                super().__init__()
                self.application_checks = 0

            def check_application_update(self) -> None:
                self.application_checks += 1

        update_host = UpdateHost()
        update_service = UpdateService(directory)
        dialog = UpdateDialog(
            [
                {
                    "name": "yt-dlp",
                    "current": "2026.08.19",
                    "source": "程序内置 yt-dlp 模块",
                    "latest": "2026.08.20",
                    "assets": [],
                    "managed_by_application": True,
                    "upstream_update_available": True,
                    "auto_install_supported": False,
                },
                {
                    "name": "FFmpeg",
                    "current": "n9.0.1",
                    "source": "程序目录 ffmpeg.exe",
                    "latest": "n9.0.1",
                    "assets": [],
                    "auto_install_supported": True,
                },
                {
                    # Defensive UI check: even a stale/custom result from an
                    # older worker must not present the embedded Qt runtime as
                    # a downloadable external component.
                    "name": "PySide6",
                    "current": "未安装",
                    "source": "",
                    "latest": "6.11.2",
                    "assets": [],
                    "install_available": True,
                },
            ],
            update_service,
            update_host,
        )
        assert dialog.tree.topLevelItemCount() == 2
        assert [dialog.tree.topLevelItem(index).text(0) for index in range(2)] == ["yt-dlp", "FFmpeg"]
        embedded = dialog.tree.topLevelItem(0)
        local = dialog.tree.topLevelItem(1)
        assert embedded.text(6) == "上游有新版（随主程序更新）"
        assert "独立更新" in dialog.detail.text()
        assert "程序目录" in local.toolTip(2)
        assert dialog.download_button.text() == "检查本程序更新"
        assert dialog.download_button.isEnabled()
        dialog.download_selected()
        assert update_host.application_checks == 1
        assert dialog.result() == QDialog.Accepted
        update_host.close()
        app.processEvents()

    # Completed media cards distinguish successful distribution from queued,
    # failed and never-created platform jobs at a glance.
    media = MediaItem(id=7, title="分发状态示例", video_path="D:/missing/example.mp4")
    completed = CompletedMediaCard(
        media,
        {"douyin": "success", "bilibili": "pending", "kuaishou": "failed"},
        ("douyin", "bilibili", "kuaishou", "xiaohongshu"),
    )
    assert completed.distribution.text() == "平台覆盖 1/4  ·  尚差 3 个平台"
    assert completed.distribution_chips["success"].text() == "✓ 已发布 1"
    assert completed.distribution_chips["active"].text() == "◷ 队列中 1"
    assert completed.distribution_chips["failed"].text() == "! 待重试 1"
    assert completed.distribution_chips["notStarted"].text() == "○ 未创建 1"
    publish_button = next(button for button in completed.findChildren(QPushButton) if button.objectName() == "primaryButton")
    assert publish_button.text() == "继续分发（1）"
    assert any(
        button.text() == "处理失败任务（1）"
        for button in completed.findChildren(QPushButton)
    )
    completed.close()
    print("ui_flow=ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
