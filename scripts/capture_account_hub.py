from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault(
    "QT_QPA_PLATFORM",
    os.environ.get("HUIFA_QT_PLATFORM", "windows"),
)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QWidget

from app.ui.account_hub import AccountHubPage
from app.ui.runtime import create_application
from app.ui.theme import THEME_LIGHT, build_application_stylesheet


class PreviewSettings:
    def __init__(self) -> None:
        self.values = {
            "publish_target_platforms": "youtube,douyin,bilibili",
            "publish_account/youtube": "studio-main",
            "publish_account/douyin": "douyin-team",
        }

    def get(self, key: str) -> str:
        return str(self.values.get(key, ""))

    def set(self, key: str, value: str) -> None:
        self.values[key] = str(value)

    def sync(self) -> None:
        pass


class PreviewPublishService(QObject):
    account_status = Signal(str, str, str, bool, str)

    def __init__(self) -> None:
        super().__init__()
        self.states = {
            ("youtube", "studio-main"): {"ok": True},
            ("douyin", "douyin-team"): {"ok": False},
        }

    def account_state(self, platform: str, account: str):
        return self.states.get((platform, account))

    def run_account_action(self, *_args, **_kwargs) -> bool:
        return True


def main() -> int:
    app, font = create_application([], requested_locale="zh-CN")
    app.setStyleSheet(build_application_stylesheet(THEME_LIGHT))
    service = PreviewPublishService()
    placeholder = QWidget()
    window = SimpleNamespace(
        app_settings=PreviewSettings(),
        publish_service=service,
        tabs=SimpleNamespace(setCurrentWidget=lambda _page: None),
        completed=placeholder,
    )
    page = AccountHubPage(window)
    page.resize(1000, 760)
    page.show()
    app.processEvents()

    target_dir = ROOT / "data" / "temp" / "ui-review"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "account-hub.png"
    saved = page.grab().save(str(target))

    page.account_action("youtube", "check")
    app.processEvents()
    busy_target = target_dir / "account-hub-busy.png"
    busy_saved = page.grab().save(str(busy_target))
    report = {
        "qt_platform": app.platformName(),
        "font_locale": font.locale,
        "platform_count": len(page.platform_account_fields),
        "youtube_account_enabled_while_busy": page.platform_account_fields[
            "youtube"
        ].isEnabled(),
        "screenshots": [str(target), str(busy_target)],
        "ok": (
            saved
            and busy_saved
            and len(page.platform_account_fields) >= 3
            and not page.platform_account_fields["youtube"].isEnabled()
        ),
    }
    report_path = target_dir / "account-hub-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    page.close()
    placeholder.close()
    app.processEvents()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(report_path)
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
