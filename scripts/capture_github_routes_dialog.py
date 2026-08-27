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
from PySide6.QtWidgets import QComboBox, QWidget

from app.ui.github_routes_dialog import GithubMirrorDialog
from app.ui.runtime import create_application
from app.ui.theme import THEME_LIGHT, build_application_stylesheet


class PreviewSettings:
    def __init__(self) -> None:
        self.values = {}

    def set(self, key: str, value: str) -> None:
        self.values[key] = value

    def sync(self) -> None:
        pass


class PreviewUpdateService(QObject):
    route_probe_finished = Signal(object)
    route_probe_failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.route_probe_thread = None
        self.route_probe_results = {
            "direct": {
                "status": "可用",
                "usable": True,
                "metadata_ok": True,
                "asset_ok": True,
                "metadata_latency_ms": 82,
                "asset_latency_ms": 146,
                "detected_kind": "direct",
            },
            "mirror:ghfast": {
                "status": "可用",
                "usable": True,
                "metadata_ok": True,
                "asset_ok": True,
                "metadata_latency_ms": 118,
                "asset_latency_ms": 207,
                "detected_kind": "prefix",
            },
            "mirror:jsdelivr": {
                "status": "可用（元数据与 CDN）",
                "usable": True,
                "metadata_only": True,
                "metadata_ok": True,
                "asset_ok": False,
                "cdn_ok": True,
                "metadata_latency_ms": 91,
                "cdn_latency_ms": 73,
                "detected_kind": "jsdelivr",
            },
        }

    def set_download_routes(self, *_args) -> None:
        pass

    def probe_download_routes(self) -> bool:
        return False

    def serialized_route_profiles(self) -> str:
        return "{}"


class PreviewHost(QWidget):
    def __init__(self, service: PreviewUpdateService) -> None:
        super().__init__()
        self.github_mirror_urls = "http://proxy.example/"
        self.github_route_profiles = "{}"
        self.github_download_route = QComboBox()
        self.github_download_route.addItem("自动测速", "auto")
        self.window = SimpleNamespace(
            update_service=service,
            app_settings=PreviewSettings(),
        )

    def refresh_github_route_combo(self, selected=None) -> None:
        pass


def main() -> int:
    app, font = create_application([], requested_locale="zh-CN")
    app.setStyleSheet(build_application_stylesheet(THEME_LIGHT))
    service = PreviewUpdateService()
    host = PreviewHost(service)
    dialog = GithubMirrorDialog(host)
    dialog.show()
    app.processEvents()

    target_dir = ROOT / "data" / "temp" / "ui-review"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "github-routes-dialog.png"
    saved = dialog.grab().save(str(target))
    dialog.close()
    app.processEvents()

    service.route_probe_thread = object()
    busy_dialog = GithubMirrorDialog(host)
    busy_dialog.show()
    app.processEvents()
    busy_target = target_dir / "github-routes-dialog-busy.png"
    busy_saved = busy_dialog.grab().save(str(busy_target))
    report = {
        "qt_platform": app.platformName(),
        "font_locale": font.locale,
        "route_count": dialog.tree.topLevelItemCount(),
        "selected_route": (
            dialog.tree.currentItem().text(0)
            if dialog.tree.currentItem() is not None
            else ""
        ),
        "probe_button_enabled": dialog.probe_button.isEnabled(),
        "busy_probe_button_enabled": busy_dialog.probe_button.isEnabled(),
        "screenshots": [str(target), str(busy_target)],
        "ok": (
            saved
            and busy_saved
            and dialog.tree.topLevelItemCount() >= 10
            and not busy_dialog.probe_button.isEnabled()
        ),
    }
    report_path = target_dir / "github-routes-dialog-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    busy_dialog.close()
    host.close()
    app.processEvents()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(report_path)
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
