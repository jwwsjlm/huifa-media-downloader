from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", os.environ.get("HUIFA_QT_PLATFORM", "windows"))
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication

from app.core.app_settings import AppSettings
from app.core.download_service import DownloadTask
from app.ui.dashboard_page import DashboardPage
from app.ui.runtime import create_application
from app.ui.theme import THEME_LIGHT, build_application_stylesheet


class _PreviewService:
    def __init__(self) -> None:
        self.tasks: dict[str, DownloadTask] = {}
        self.workers: dict[str, object] = {}
        self.logs: list[object] = []

    def task_statistics(self, *, top_level_only: bool = False) -> dict[str, int]:
        return {
            "total": len(self.tasks),
            "active": 0,
            "queued": 0,
            "paused": len(self.tasks),
            "processing": 0,
            "completed": 0,
            "failed": 0,
        }

    def has_task_status(self, _statuses: set[str], *, top_level_only: bool = False) -> bool:
        return False


def main() -> int:
    app, _font = create_application([])
    app.setStyleSheet(build_application_stylesheet(THEME_LIGHT))
    service = _PreviewService()
    window = SimpleNamespace(
        app_settings=AppSettings(),
        download_service=service,
        tabs=SimpleNamespace(setCurrentWidget=lambda _widget: None),
        settings=object(),
    )
    page = DashboardPage(window)
    page.resize(1180, 780)
    page.show()
    output_dir = ROOT / "data" / "temp"
    output_dir.mkdir(parents=True, exist_ok=True)

    tasks = (
        DownloadTask(
            "context-video",
            "https://example.com/video",
            str(output_dir),
            task_kind="video",
            status="completed",
            media_path=str(output_dir / "ui-task-stages.png"),
        ),
        DownloadTask(
            "context-collection",
            "https://example.com/playlist",
            str(output_dir),
            task_kind="collection",
            status="paused",
        ),
    )
    captures: list[Path] = []
    for task in tasks:
        menu, _actions = page.task_menu_controller.build(task)
        menu.ensurePolished()
        menu.adjustSize()
        app.processEvents()
        target = output_dir / f"task-context-{task.task_kind}.png"
        if not menu.grab().save(str(target)):
            return 2
        captures.append(target)
        menu.deleteLater()

    page.close()
    app.processEvents()
    for target in captures:
        print(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
