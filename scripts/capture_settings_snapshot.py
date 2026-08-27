from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", os.environ.get("HUIFA_QT_PLATFORM", "windows"))
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ui.settings_page import SettingsPage
from app.ui.runtime import create_application
from app.ui.theme import THEME_LIGHT, build_application_stylesheet
from PySide6.QtWidgets import QScrollArea


class _PreviewSettings:
    def __init__(self) -> None:
        self.values = {
            "download_dir": "D:/youtube",
            "quality": "best",
            "download_options_json": "{}",
            "download_performance_mode": "manual",
            "max_concurrent": "3",
            "fragment_concurrent": "8",
            "request_delay": "0",
        }

    def get(self, key: str) -> str:
        return str(self.values.get(key, ""))

    def get_int(self, key: str, default: int, minimum=None, maximum=None) -> int:
        value = int(self.values.get(key, default))
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def get_float(self, key: str, default: float, minimum=None, maximum=None) -> float:
        value = float(self.values.get(key, default))
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = str(self.values.get(key, "true" if default else "false")).casefold()
        return value in {"1", "true", "yes", "on"}


class _Signal:
    def connect(self, _callback) -> None:
        pass


def main() -> int:
    app, _font = create_application([])
    app.setStyleSheet(build_application_stylesheet(THEME_LIGHT))
    signal = _Signal()
    no_op = lambda *_args, **_kwargs: None
    update_service = SimpleNamespace(
        finished=signal,
        failed=signal,
        download_failed=signal,
        install_finished=signal,
        install_failed=signal,
        last_results=[],
        thread=None,
        download_thread=None,
        install_thread=None,
        set_ffmpeg_build_channel=no_op,
        check=no_op,
        download_asset=no_op,
    )
    window = SimpleNamespace(
        app_settings=_PreviewSettings(),
        update_service=update_service,
        secure_store=SimpleNamespace(get=lambda _key: ""),
        desktop_notifications_available=True,
        application_updates_supported=False,
        application_update_mode="",
        dashboard=SimpleNamespace(show_download_readiness=no_op),
        clear_openai_api_key=no_op,
        open_log_directory=no_op,
        export_diagnostics=no_op,
        check_application_update=no_op,
        check_updates=no_op,
        save_settings=no_op,
        settings_status=no_op,
    )
    page = SettingsPage(window)
    page.show()
    target_dir = ROOT / "data" / "temp" / "ui-review"
    target_dir.mkdir(parents=True, exist_ok=True)
    captures = []
    scroll = page.findChild(QScrollArea)
    for name, width in (("wide", 1080), ("narrow", 704)):
        page.resize(width, 780)
        if scroll is not None:
            scroll.verticalScrollBar().setValue(0)
        app.processEvents()
        target = target_dir / f"settings-refactor-{name}.png"
        captures.append((target, page.grab().save(str(target))))
        if scroll is not None:
            scroll.ensureWidgetVisible(
                page.runtime_version_labels["yt-dlp"],
                0,
                80,
            )
            app.processEvents()
            tools_target = target_dir / f"settings-refactor-tools-{name}.png"
            captures.append((tools_target, page.grab().save(str(tools_target))))
            scroll.ensureWidgetVisible(
                page.runtime_version_labels["FFprobe"],
                0,
                80,
            )
            app.processEvents()
            tools_detail_target = (
                target_dir / f"settings-refactor-tools-detail-{name}.png"
            )
            captures.append((
                tools_detail_target,
                page.grab().save(str(tools_detail_target)),
            ))
    page.close()
    app.processEvents()
    for target, _saved in captures:
        print(target)
    return 0 if all(saved for _target, saved in captures) else 2


if __name__ == "__main__":
    raise SystemExit(main())
