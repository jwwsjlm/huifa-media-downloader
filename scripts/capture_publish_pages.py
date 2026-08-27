from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", os.environ.get("HUIFA_QT_PLATFORM", "windows"))

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QLineEdit

from app.storage.models import MediaItem
from app.ui.i18n import apply_runtime_translation
from app.ui.publish_editor import PublishPage
from app.ui.publish_queue import PublishQueuePage
from app.ui.runtime import create_application
from app.ui.theme import THEME_LIGHT, build_application_stylesheet


class FakeSettings:
    def set(self, _key: str, _value: str) -> None:
        pass

    def sync(self) -> None:
        pass


class FakePublishService:
    last_created_count = 0
    last_existing_count = 0

    def account_state(self, _platform: str, _account: str):
        return {"ok": True}

    def run_task(self, _task_id: int) -> None:
        pass

    def retry_task(self, _task_id: int) -> None:
        pass


class FakeDatabase:
    def __init__(self) -> None:
        self.rows = [
            {
                "id": 103,
                "media_id": 7,
                "platform": "youtube",
                "account": "main",
                "status": "uploading",
                "title": "用于检查发布队列标题伸缩和状态展示的示例视频",
                "result": "正在上传分片 7/12",
            },
            {
                "id": 102,
                "media_id": 7,
                "platform": "bilibili",
                "account": "studio",
                "status": "failed",
                "title": "同一视频的哔哩哔哩发布任务",
                "result": "Cookie 已失效，请重新登录",
            },
            {
                "id": 101,
                "media_id": 6,
                "platform": "douyin",
                "account": "main",
                "status": "pending",
                "title": "等待发布的抖音任务",
                "result": "",
            },
            {
                "id": 100,
                "media_id": 5,
                "platform": "youtube",
                "account": "main",
                "status": "success",
                "title": "已经发布完成的视频",
                "result": "https://youtu.be/example",
            },
        ]

    def count_publish_tasks(self, media_id=None) -> int:
        return len([
            row
            for row in self.rows
            if media_id is None or int(row["media_id"]) == int(media_id)
        ])

    def list_publish_tasks(self, limit=None, offset=0, media_id=None):
        rows = [
            row
            for row in self.rows
            if media_id is None or int(row["media_id"]) == int(media_id)
        ]
        return list(rows[offset:] if limit is None else rows[offset:offset + limit])

    def get_publish_task(self, task_id: int):
        return next((row for row in self.rows if int(row["id"]) == int(task_id)), None)


def capture(widget, app, path: Path, width: int, height: int) -> None:
    widget.resize(width, height)
    widget.show()
    app.processEvents()
    render_timer = getattr(widget, "_queue_render_timer", None)
    while render_timer is not None and render_timer.isActive():
        app.processEvents()
    app.processEvents()
    widget.grab().save(str(path))


def main() -> int:
    app, _font = create_application([])
    app.setStyleSheet(build_application_stylesheet(THEME_LIGHT))
    target_dir = ROOT / "data" / "temp" / "ui-review"
    target_dir.mkdir(parents=True, exist_ok=True)

    service = FakePublishService()
    accounts = {
        platform: QLineEdit(account)
        for platform, account in (
            ("douyin", "main"),
            ("bilibili", "studio"),
            ("youtube", "main"),
        )
    }
    shell = SimpleNamespace(
        publish_service=service,
        app_settings=FakeSettings(),
        account_hub=SimpleNamespace(platform_account_fields=accounts),
        tabs=SimpleNamespace(setCurrentWidget=lambda _widget: None),
        publish_queue=SimpleNamespace(mark_dirty=lambda: None),
        completed=SimpleNamespace(mark_dirty=lambda: None),
    )
    media = MediaItem(
        id=7,
        title="用于检查发布编辑器布局、较长标题以及平台设置随动的示例视频",
        description="这是一段示例简介，用于确认文本框和滚动区域在真实 Windows Qt 下显示正常。",
        tags=["示例", "发布测试"],
        source_url="https://www.youtube.com/watch?v=example",
        source_ip="203.0.113.8",
        thumbnail_path="D:/视频归档/示例封面.jpg",
    )
    editor = PublishPage(shell, media, ("douyin", "bilibili", "youtube"))
    editor.partition.setText("17")
    apply_runtime_translation(editor)
    editor_path = target_dir / "publish-editor.png"
    capture(editor, app, editor_path, 1000, 760)

    queue_shell = SimpleNamespace(db=FakeDatabase(), publish_service=service)
    queue = PublishQueuePage(queue_shell)
    apply_runtime_translation(queue)
    queue.refresh()
    while queue._queue_render_timer.isActive():
        app.processEvents()
    queue.tree.setCurrentItem(queue.items[102])
    wide_path = target_dir / "publish-queue-wide.png"
    narrow_path = target_dir / "publish-queue-narrow.png"
    capture(queue, app, wide_path, 1000, 620)
    capture(queue, app, narrow_path, 704, 560)

    editor.close()
    queue.close()
    app.processEvents()
    print(editor_path)
    print(wide_path)
    print(narrow_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
