from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault(
    "QT_QPA_PLATFORM",
    os.environ.get("HUIFA_QT_PLATFORM", "windows"),
)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QLineEdit, QVBoxLayout, QWidget

from app.storage.models import MediaItem
from app.ui.i18n import apply_runtime_translation
from app.ui.navigation import SidebarNavigation, navigation_icon
from app.ui.publish_ui_controller import PublishUiBindings, PublishUiController
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


class PassivePage(QWidget):
    def mark_dirty(self) -> None:
        pass


class QueuePage(PassivePage):
    def focus_media(self, _media: MediaItem) -> None:
        pass


class FakeDatabase:
    def get_publish_task(self, _task_id: int):
        return None


class FakeNotifications:
    def publish_finished(self, *_args) -> None:
        pass


class Shell(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.app_settings = FakeSettings()
        self.publish_service = FakePublishService()
        self.account_hub = PassivePage()
        self.account_hub.platform_account_fields = {
            "douyin": QLineEdit("main"),
            "bilibili": QLineEdit("studio"),
            "youtube": QLineEdit("main"),
        }
        self.tabs = SidebarNavigation(self)
        self.downloads = PassivePage()
        self.completed = PassivePage()
        self.publish_queue = QueuePage()
        for label, page, icon_key in (
            ("下载任务", self.downloads, "download"),
            ("账号中心", self.account_hub, "accounts"),
            ("完成列表", self.completed, "completed"),
            ("发布队列", self.publish_queue, "publish"),
        ):
            self.tabs.addTab(page, label, navigation_icon(icon_key))
        self.publish_ui = PublishUiController(PublishUiBindings(
            parent=self,
            database=lambda: FakeDatabase(),
            tabs=self.tabs,
            publish_queue=self.publish_queue,
            completed_page=self.completed,
            desktop_notifications=FakeNotifications(),
            show_status=lambda _message, _timeout: None,
        ))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tabs)


def capture(shell: Shell, app, target: Path) -> None:
    shell.resize(1180, 760)
    shell.show()
    app.processEvents()
    if not shell.grab().save(str(target)):
        raise RuntimeError(f"Could not save screenshot: {target}")


def main() -> int:
    app, font = create_application([])
    app.setStyleSheet(build_application_stylesheet(THEME_LIGHT))
    target_dir = ROOT / "data" / "temp" / "ui-review"
    target_dir.mkdir(parents=True, exist_ok=True)
    shell = Shell()
    apply_runtime_translation(shell)
    media = MediaItem(
        id=7,
        title="用于确认发布编辑页复用和侧边栏动态移除的示例视频",
        source_url="https://www.youtube.com/watch?v=example",
    )

    editor = shell.publish_ui.open_editor(media, ("youtube",))
    reused = shell.publish_ui.open_editor(media, ("youtube", "bilibili"))
    if reused is not editor or shell.tabs.count() != 5:
        raise RuntimeError("Publish editor was not reused")
    opened = target_dir / "publish-navigation-editor-open.png"
    capture(shell, app, opened)

    shell.publish_ui.complete_editor(editor)
    app.processEvents()
    if shell.tabs.count() != 4 or shell.tabs.currentWidget() is not shell.publish_queue:
        raise RuntimeError("Publish editor was not removed cleanly")
    closed = target_dir / "publish-navigation-editor-closed.png"
    capture(shell, app, closed)

    shell.close()
    app.processEvents()
    print(opened)
    print(closed)
    print(f"font={font.family}; locale={font.locale}; platform={app.platformName()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
