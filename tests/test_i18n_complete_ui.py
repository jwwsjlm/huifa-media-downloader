from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QComboBox,
    QGroupBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QTabWidget,
    QTextEdit,
    QTreeWidget,
    QWidget,
)

from app.core.browser_cookies import BrowserCookie
from app.core.download_service import DownloadTask
from app.core.log_service import DownloadLogService
from app.core.update_service import UpdateService
from app.storage.models import MediaItem
from app.ui.download_dialogs import DownloadLogDialog
from app.ui.embedded_browser import CookieViewerDialog
from app.ui.account_hub import AccountHubPage
from app.ui.github_routes_dialog import GithubMirrorDialog
from app.ui.i18n import runtime_text
from app.ui.runtime_components_dialog import UpdateDialog
from app.ui.task_card import DownloadTaskCard
from app.ui.completed_page import CompletedPage
from app.ui.publish_editor import PublishPage
from app.ui.publish_queue import PublishQueuePage
from app.ui.dashboard_page import DashboardPage
from tests.test_account_hub_ui import _Window as AccountWindow
from tests.test_dashboard_overview import _DownloadService, _Settings as DashboardSettings
from tests.test_distribution_plan_ui import (
    _CompletedWindow as CompletedWindow,
    _QueueWindow as QueueWindow,
    _Window as PublishWindow,
)


ROOT = Path(__file__).resolve().parents[1]
CJK_RE = re.compile(r"[\u3400-\u9fff]")


def chinese_widget_texts(root: QWidget) -> list[tuple[str, str, str]]:
    """Collect translated chrome only; test fixtures use English user data."""
    findings: list[tuple[str, str, str]] = []
    for widget in [root, *root.findChildren(QWidget)]:
        values = [
            ("window title", widget.windowTitle()),
            ("tooltip", widget.toolTip()),
            ("status tip", widget.statusTip()),
            ("what's this", widget.whatsThis()),
            ("accessible name", widget.accessibleName()),
            ("accessible description", widget.accessibleDescription()),
        ]
        if isinstance(widget, QGroupBox):
            values.append(("group title", widget.title()))
        if isinstance(widget, (QAbstractButton, QLabel)):
            values.append(("text", widget.text()))
        if isinstance(widget, QLineEdit):
            values.append(("placeholder", widget.placeholderText()))
        if isinstance(widget, (QTextEdit, QPlainTextEdit)):
            values.append(("placeholder", widget.placeholderText()))
            if widget.isReadOnly():
                values.append(("read-only content", widget.toPlainText()))
        if isinstance(widget, QComboBox):
            for index in range(widget.count()):
                values.append(("combo item", widget.itemText(index)))
                values.append(("combo tooltip", str(widget.itemData(index, Qt.ToolTipRole) or "")))
        if isinstance(widget, QTabWidget):
            for index in range(widget.count()):
                values.append(("tab", widget.tabText(index)))
                values.append(("tab tooltip", widget.tabToolTip(index)))
        if isinstance(widget, QTreeWidget):
            header = widget.headerItem()
            if header is not None:
                values.extend(("tree header", header.text(column)) for column in range(widget.columnCount()))
            stack = [widget.topLevelItem(index) for index in range(widget.topLevelItemCount())]
            while stack:
                item = stack.pop()
                for column in range(widget.columnCount()):
                    values.append(("tree item", item.text(column)))
                    values.append(("tree tooltip", item.toolTip(column)))
                stack.extend(item.child(index) for index in range(item.childCount()))
        if isinstance(widget, QListWidget):
            for index in range(widget.count()):
                item = widget.item(index)
                values.append(("list item", item.text()))
                values.append(("list tooltip", item.toolTip()))
        for kind, value in values:
            if value and CJK_RE.search(str(value)):
                findings.append((type(widget).__name__, kind, str(value)))
    return findings


class _RouteService(QObject):
    route_probe_finished = Signal(object)
    route_probe_failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.route_probe_results = {
            "direct": {
                "status": "Available",
                "metadata_ok": True,
                "asset_ok": True,
                "latency_ms": 12,
            }
        }

    def runtime_active(self, *_kinds: str) -> bool:
        return False


class _SettingsPageStub(QWidget):
    def __init__(self, update_service: _RouteService) -> None:
        super().__init__()
        self.window = SimpleNamespace(update_service=update_service)
        self.github_mirror_urls = ""
        self.github_route_profiles = ""
        self.github_download_route = QComboBox()
        self.github_download_route.addItem("Auto", "auto")

    def refresh_github_route_combo(self, *_args) -> None:
        pass


class CompleteEnglishUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        payload = json.loads((ROOT / "languages" / "en-US.json").read_text(encoding="utf-8"))
        cls.english_translations = payload["translations"]

    def setUp(self) -> None:
        self.previous_locale = self.app.property("huifa.ui_locale")
        self.previous_translations = self.app.property("huifa.ui_translations")
        self.app.setProperty("huifa.ui_locale", "en-US")
        self.app.setProperty("huifa.ui_translations", dict(self.english_translations))
        self.widgets: list[QWidget] = []

    def tearDown(self) -> None:
        for widget in reversed(self.widgets):
            widget.close()
            widget.deleteLater()
        self.app.processEvents()
        self.app.setProperty("huifa.ui_locale", self.previous_locale)
        self.app.setProperty("huifa.ui_translations", self.previous_translations)

    def keep(self, name: str, widget: QWidget) -> QWidget:
        self.widgets.append(widget)
        self.app.processEvents()
        findings = chinese_widget_texts(widget)
        self.assertEqual(findings, [], f"{name} contains untranslated UI text: {findings}")
        return widget

    def test_primary_pages_and_task_states_have_no_chinese_chrome(self) -> None:
        service = _DownloadService()
        dashboard_window = SimpleNamespace(
            app_settings=DashboardSettings(),
            download_service=service,
            completed=SimpleNamespace(mark_dirty=lambda: None),
            tabs=SimpleNamespace(setCurrentWidget=lambda _widget: None),
            settings=object(),
        )
        self.keep("DashboardPage", DashboardPage(dashboard_window))
        self.keep("AccountHubPage", AccountHubPage(AccountWindow()))

        completed = CompletedPage(CompletedWindow())
        completed.refresh()
        self.app.processEvents()
        self.keep("CompletedPage", completed)

        self.keep("PublishPage", PublishPage(PublishWindow(), MediaItem(id=1, title="Demo")))
        queue = PublishQueuePage(QueueWindow())
        queue.refresh()
        self.app.processEvents()
        self.keep("PublishQueuePage", queue)
        self.keep(
            "CookieViewerDialog",
            CookieViewerDialog(
                lambda: [BrowserCookie("sid", "secret", ".example.com")],
                profile_id="download",
            ),
        )

        task_states = (
            ("queued", "queued"),
            ("parsing", "downloading"),
            ("waiting_selection", "waiting_selection"),
            ("parsing_collection", "parsing_collection"),
            ("merging", "downloading"),
            ("transcoding", "processing"),
            ("pausing", "暂停中"),
            ("paused", "paused"),
            ("canceling", "canceling"),
            ("completed", "completed"),
            ("failed", "failed"),
            ("partial_failed", "partial_failed"),
            ("canceled", "canceled"),
            ("completed", "deleted"),
        )
        tasks = tuple(
            DownloadTask(
                f"state-{index}",
                f"https://example.com/{index}",
                "D:/downloads",
                title=f"Task state {index}",
                task_kind="collection" if status in {"parsing_collection", "partial_failed"} else "video",
                status=status,
                stage=stage,
                stage_text="",
                progress=42,
                stage_progress=42 if stage == "transcoding" else 0,
                error="",
            )
            for index, (stage, status) in enumerate(task_states)
        )
        for task in tasks:
            self.keep(f"DownloadTaskCard:{task.id}", DownloadTaskCard(task))

    def test_dynamic_service_messages_are_not_translated(self) -> None:
        message = "下载失败：磁盘空间不足"
        self.assertEqual(runtime_text(message), message)

        task = DownloadTask(
            "failed",
            "https://example.com",
            "D:/downloads",
            status="failed",
            stage="failed",
            error=message,
        )
        card = DownloadTaskCard(task)
        self.widgets.append(card)
        self.app.processEvents()
        visible_values = [
            value
            for label in card.findChildren(QLabel)
            for value in (label.text(), label.toolTip())
        ]
        self.assertTrue(any(message in value for value in visible_values), visible_values)

    def test_update_and_route_prompts_have_no_chinese_chrome(self) -> None:
        route_service = _RouteService()
        settings_page = _SettingsPageStub(route_service)
        self.widgets.append(settings_page)
        self.keep("GithubMirrorDialog", GithubMirrorDialog(settings_page))

        with tempfile.TemporaryDirectory() as directory:
            service = UpdateService(Path(directory) / "updates")
            try:
                dialog = UpdateDialog(
                    [{
                        "name": "Deno",
                        "current": "Not installed",
                        "source": "",
                        "runtime_path": "",
                        "latest": "Not detected",
                        "assets": [],
                        "auto_install_supported": False,
                        "has_update": False,
                        "error": "Check failed: Network unavailable",
                        "url": "https://example.com",
                    }],
                    service,
                )
                self.keep("UpdateDialog dynamic error", dialog)
            finally:
                service.shutdown(timeout_ms=0)

if __name__ == "__main__":
    unittest.main()
