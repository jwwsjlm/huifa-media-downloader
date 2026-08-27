from __future__ import annotations

import os
import json
import sys
from pathlib import Path
from types import SimpleNamespace

# Run screenshot capture in the real Windows Qt platform so the test process
# sees the same system font registry as the packaged desktop application.
# Set HUIFA_QT_PLATFORM=offscreen explicitly when a headless CI runner needs
# that platform; such a runner must provide a Qt-visible font separately.
os.environ.setdefault("QT_QPA_PLATFORM", os.environ.get("HUIFA_QT_PLATFORM", "windows"))
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication

from app.core.app_settings import AppSettings
from app.core.download_service import DownloadTask
from app.ui.dashboard_page import DashboardPage
from app.ui.runtime import create_application
from app.ui.i18n import apply_runtime_translation
from app.ui.theme import THEME_LIGHT, build_application_stylesheet


class FakeService:
    def __init__(self) -> None:
        self.tasks = {}
        self.workers = {}

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
    app, font = create_application([])
    app.setStyleSheet(build_application_stylesheet(THEME_LIGHT))
    service = FakeService()
    window = SimpleNamespace(
        app_settings=AppSettings(),
        download_service=service,
        tabs=SimpleNamespace(setCurrentWidget=lambda _widget: None),
        settings=object(),
    )
    page = DashboardPage(window)
    apply_runtime_translation(page)
    page.resize(1280, 760)
    page.set_tasks_loaded()
    title_a = "Example video: merging streams" if font.locale == "en-US" else "示例视频：正在合并音视频"
    stage_a = "Merging video and audio" if font.locale == "en-US" else "正在合并视频和音频"
    title_b = "Example video: reconnecting" if font.locale == "en-US" else "示例视频：网络恢复中"
    stage_b = "Network interrupted, retrying" if font.locale == "en-US" else "网络中断，正在重连"
    tasks = [
        DownloadTask(
            "snapshot-active", "https://www.youtube.com/watch?v=demo",
            "D:/tmp", title=title_a, status="downloading",
            progress=87.0, downloaded_bytes=870 * 1024 * 1024,
            total_bytes=1000 * 1024 * 1024, speed="8.2 MiB/s", eta="00:26",
            stage="merging", stage_text=stage_a, stage_progress=99,
        ),
        DownloadTask(
            "snapshot-retry", "https://example.com/video",
            "D:/tmp", title=title_b, status="downloading",
            progress=42.0, downloaded_bytes=420 * 1024 * 1024,
            total_bytes=1000 * 1024 * 1024, speed="3.1 MiB/s", eta="03:08",
            stage="reconnecting", stage_text=stage_b,
            retry_count=1, reconnect_message="Retrying in 2 seconds" if font.locale == "en-US" else "2 秒后重试",
        ),
    ]
    for task in tasks:
        service.tasks[task.id] = task
        page.add_task(task)
    app.processEvents()
    target = ROOT / "data" / "temp" / "ui-task-stages.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    page.grab().save(str(target))
    critical = [page.empty_label.text(), page.url.placeholderText(), page.status.text()]
    critical_renderable = bool(font.latin_supported and all(bool(value.strip()) for value in critical))
    report = {
        "qt_platform": app.platformName(),
        "font_source": "system",
        **font.as_dict(),
        "critical_texts_renderable": critical_renderable,
        "screenshot_ok": critical_renderable,
    }
    report_path = target.with_name("ui-snapshot-report.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    page.close()
    app.processEvents()
    print(target)
    print(report_path)
    return 0 if report["screenshot_ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
