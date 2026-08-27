from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", os.environ.get("HUIFA_QT_PLATFORM", "windows"))
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMainWindow, QVBoxLayout, QWidget

from app.core.download_service import DownloadTask
from app.ui.dashboard_page import DashboardPage
from app.ui.navigation import SidebarNavigation, configure_main_navigation
from app.ui.runtime import create_application
from app.ui.theme import THEME_DARK, THEME_LIGHT, build_application_stylesheet


class FakeSettings:
    values = {
        "download_dir": "D:/youtube",
        "quality": "best",
        "playlist_mode": "auto",
        "max_concurrent": "3",
        "fragment_concurrent": "12",
    }

    def get(self, key: str) -> str:
        return self.values.get(key, "")

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


class FakeService:
    def __init__(self) -> None:
        self.tasks: dict[str, DownloadTask] = {}
        self.workers: dict[str, object] = {}

    def cancel(self, _task_id: str) -> None:
        return

    def pause(self, _task_id: str) -> None:
        return

    def resume(self, _task_id: str) -> None:
        return

    def retry(self, _task_id: str) -> None:
        return

    def task_statistics(self, *, top_level_only: bool = False) -> dict[str, int]:
        tasks = [
            task
            for task in self.tasks.values()
            if not top_level_only or not task.parent_task_id
        ]
        counts: dict[str, int] = {}
        for task in tasks:
            counts[task.status] = counts.get(task.status, 0) + 1
        return {
            "total": len(tasks),
            "active": sum(counts.get(status, 0) for status in (
                "downloading", "parsing_collection", "canceling",
            )),
            "queued": counts.get("queued", 0),
            "paused": counts.get("paused", 0) + counts.get("暂停中", 0),
            "processing": counts.get("processing", 0),
            "completed": counts.get("completed", 0),
            "failed": sum(counts.get(status, 0) for status in (
                "failed", "partial_failed", "canceled",
            )),
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


def placeholder_page(title: str) -> QWidget:
    page = QWidget()
    layout = QVBoxLayout(page)
    label = QLabel(title)
    label.setObjectName("pageTitle")
    label.setAlignment(Qt.AlignCenter)
    layout.addWidget(label, 1)
    return page


def state_tasks() -> list[DownloadTask]:
    mib = 1024 * 1024
    return [
        DownloadTask(
            "queued", "https://example.com/queued", "D:/youtube",
            title="等待开始：刚加入队列的任务", status="queued", stage="queued",
        ),
        DownloadTask(
            "parsing", "https://www.youtube.com/watch?v=parse", "D:/youtube",
            title="正在解析视频标题和播放列表信息", status="downloading", stage="parsing",
        ),
        DownloadTask(
            "formats", "https://www.bilibili.com/video/BV1demo", "D:/youtube",
            title="正在读取可用清晰度和音视频格式", status="downloading", stage="formats",
        ),
        DownloadTask(
            "selection", "https://example.com/select", "D:/youtube",
            title="等待用户选择分辨率", status="waiting_selection", stage="waiting_selection",
        ),
        DownloadTask(
            "disk", "https://example.com/disk", "D:/youtube",
            title="磁盘空间暂时不足，正在等待其他任务释放预留空间",
            status="downloading", stage="waiting_disk", elapsed_seconds=18,
        ),
        DownloadTask(
            "video", "https://www.youtube.com/watch?v=download", "D:/youtube",
            title="长视频下载中：视频流与音频流分开处理", status="downloading",
            stage="downloading_video", progress=36, video_progress=52, audio_progress=18,
            downloaded_bytes=742 * mib, total_bytes=2048 * mib,
            speed="24.8 MiB/s", eta="00:53", elapsed_seconds=76, stage_elapsed_seconds=41,
        ),
        DownloadTask(
            "reconnect", "https://example.com/reconnect", "D:/youtube",
            title="网络抖动后的恢复测试：标题非常长，用于确认不会挤压状态和操作按钮" * 2,
            status="downloading", stage="reconnecting", progress=42,
            downloaded_bytes=420 * mib, total_bytes=1000 * mib,
            speed="3.1 MiB/s", eta="03:08", retry_count=2, retry_total=5,
            reconnect_message="3 秒后重试，本次将继续使用已下载的分片",
        ),
        DownloadTask(
            "merge", "https://www.youtube.com/watch?v=merge", "D:/youtube",
            title="下载完成，正在合并视频与音频", status="downloading", stage="merging",
            progress=87, downloaded_bytes=870 * mib, total_bytes=1000 * mib,
            speed="8.2 MiB/s", eta="00:26",
        ),
        DownloadTask(
            "paused", "https://example.com/paused", "D:/youtube",
            title="用户暂停的任务", status="paused", stage="paused", progress=58,
            downloaded_bytes=580 * mib, total_bytes=1000 * mib,
        ),
        DownloadTask(
            "canceling", "https://example.com/canceling", "D:/youtube",
            title="正在安全停止工作线程", status="canceling", stage="canceled", progress=21,
        ),
        DownloadTask(
            "failed", "https://example.com/failed?token=redacted", "D:/youtube",
            title="下载失败：用于验证错误状态、重试按钮和超长错误提示",
            status="failed", stage="failed", progress=64,
            error="HTTP 403: 登录状态失效或站点触发访问频率限制，请更新 Cookie 后重试。" * 3,
        ),
        DownloadTask(
            "canceled", "https://example.com/canceled", "D:/youtube",
            title="已取消，保留记录以便稍后重试", status="canceled", stage="canceled", progress=12,
        ),
        DownloadTask(
            "completed", "https://example.com/completed", "D:/youtube",
            title="下载完成但旧记录没有保存最终进度值", status="completed", stage="completed",
            progress=0, size="1.4 GiB", uploader="演示作者", downloaded_at="2026-08-24T10:30:00",
        ),
        DownloadTask(
            "deleted", "https://example.com/deleted", "D:/youtube",
            title="媒体文件已被外部删除，可以重新下载", status="deleted", stage="completed",
            progress=100, size="860 MiB",
        ),
    ]


def main() -> int:
    app, font = create_application([])
    app.setStyleSheet(build_application_stylesheet(THEME_LIGHT))
    service = FakeService()

    shell = QMainWindow()
    shell.setWindowTitle("汇发视频下载工具 · UI 状态审查")
    tabs = SidebarNavigation()
    configure_main_navigation(tabs)
    shell.setCentralWidget(tabs)
    window = SimpleNamespace(
        app_settings=FakeSettings(),
        download_service=service,
        tabs=tabs,
        settings=placeholder_page("应用设置"),
    )
    dashboard = DashboardPage(window)
    tabs.addTab(dashboard, "下载任务")
    tabs.addTab(placeholder_page("账号中心"), "账号中心")
    tabs.addTab(placeholder_page("完成列表"), "完成列表")
    tabs.addTab(placeholder_page("发布队列"), "发布队列")
    tabs.addTab(window.settings, "设置")

    tasks = state_tasks()
    for task in tasks:
        service.tasks[task.id] = task
        dashboard.add_task(task, defer_refresh=True)
    dashboard.set_tasks_loaded()
    dashboard.apply_filter()
    dashboard._update_count()
    shell.resize(1440, 900)
    shell.show()
    app.processEvents()

    target_dir = ROOT / "data" / "temp" / "ui-review"
    target_dir.mkdir(parents=True, exist_ok=True)
    scrollbar = dashboard.task_list.verticalScrollBar()
    captures: list[str] = []
    positions = (0, max(0, scrollbar.maximum() // 2), scrollbar.maximum())
    for name, position in zip(("top", "middle", "bottom"), positions):
        scrollbar.setValue(position)
        app.processEvents()
        target = target_dir / f"navigation-task-states-{name}.png"
        if not shell.grab().save(str(target)):
            raise RuntimeError(f"Could not save screenshot: {target}")
        captures.append(str(target))

    app.setStyleSheet(build_application_stylesheet(THEME_DARK))
    scrollbar.setValue(0)
    app.processEvents()
    dark_target = target_dir / "navigation-task-states-dark.png"
    if not shell.grab().save(str(dark_target)):
        raise RuntimeError(f"Could not save screenshot: {dark_target}")
    captures.append(str(dark_target))

    tabs.setCollapsed(True)
    app.processEvents()
    collapsed_controls = {
        "content_menu": dashboard.task_download_menu,
        "quality_menu": dashboard.task_quality_menu,
        "format_menu": dashboard.task_container,
        "sort_box": dashboard.sort_box,
        "filter_box": dashboard.filter_box,
        "pause_all": dashboard.pause_all_button,
        "resume_all": dashboard.resume_all_button,
        "cleanup": dashboard.cleanup_button,
    }
    collapsed_control_visibility = {
        name: widget.isVisibleTo(shell)
        for name, widget in collapsed_controls.items()
    }
    collapsed_control_geometry = {
        name: [
            widget.geometry().x(),
            widget.geometry().y(),
            widget.geometry().width(),
            widget.geometry().height(),
        ]
        for name, widget in collapsed_controls.items()
    }
    collapsed_target = target_dir / "navigation-task-states-collapsed.png"
    if not shell.grab().save(str(collapsed_target)):
        raise RuntimeError(f"Could not save screenshot: {collapsed_target}")
    captures.append(str(collapsed_target))

    completed_card = dashboard.cards["completed"]
    viewport_width = dashboard.task_list.viewport().width()
    card_widths = [card.width() for card in dashboard.cards.values()]
    report = {
        "qt_platform": app.platformName(),
        "font_locale": font.locale,
        "task_count": len(tasks),
        "card_count": len(dashboard.cards),
        "horizontal_scroll_maximum": dashboard.task_list.horizontalScrollBar().maximum(),
        "completed_progress": completed_card.progress.value(),
        "task_viewport_width": viewport_width,
        "task_card_width_min": min(card_widths, default=0),
        "task_card_width_max": max(card_widths, default=0),
        "navigation_collapsed": tabs.isCollapsed(),
        "navigation_width": tabs.sidebar.width(),
        "navigation_accessible_name": tabs.accessibleName(),
        "collapsed_control_visibility": collapsed_control_visibility,
        "collapsed_control_geometry": collapsed_control_geometry,
        "screenshots": captures,
        "ok": (
            len(dashboard.cards) == len(tasks)
            and dashboard.task_list.horizontalScrollBar().maximum() == 0
            and completed_card.progress.value() == 100
            and bool(card_widths)
            and max(abs(width - max(320, viewport_width - 8)) for width in card_widths) <= 2
            and tabs.isCollapsed()
            and tabs.sidebar.width() == tabs.COLLAPSED_WIDTH
            and all(collapsed_control_visibility.values())
        ),
    }
    report_path = target_dir / "navigation-task-states-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    shell.close()
    app.processEvents()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(report_path)
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
