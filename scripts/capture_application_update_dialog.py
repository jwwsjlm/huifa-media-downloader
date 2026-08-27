from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


os.environ.setdefault("QT_QPA_PLATFORM", os.environ.get("HUIFA_QT_PLATFORM", "windows"))
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QWidget

from app.core.application_update_service import ApplicationUpdateService
from app.core.application_updater import ApplicationUpdate
from app.ui.application_update_dialog import ApplicationUpdateDialog
from app.ui.runtime import create_application
from app.ui.theme import THEME_LIGHT, build_application_stylesheet


class _Host(QWidget):
    def install_application_update(self, _update, *, confirmed: bool = False) -> None:
        _ = confirmed


def main() -> int:
    app, font = create_application([])
    app.setStyleSheet(build_application_stylesheet(THEME_LIGHT))
    with tempfile.TemporaryDirectory() as directory:
        service = ApplicationUpdateService(Path(directory) / "updates")
        host = _Host()
        update = ApplicationUpdate(
            token="snapshot-0.4.0",
            current_version="0.3.0",
            version="0.4.0",
            package_id="Huifa.VideoDownloader",
            file_name="Huifa.VideoDownloader-0.4.0-full.nupkg",
            size_bytes=318 * 1024 * 1024,
            sha256="a" * 64,
            release_notes_markdown=(
                "# 更新内容\n\n"
                "- 优化任务列表和下载恢复性能\n"
                "- 修复应用更新断点续传与退出稳定性\n"
                "- 更新内置下载核心"
            ),
            is_downgrade=False,
            is_portable=True,
            downloaded=True,
        )
        dialog = ApplicationUpdateDialog(update, service, host)
        dialog.resize(700, 540)
        dialog.show()
        app.processEvents()

        target = ROOT / "data" / "temp" / "application-update-windows.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        saved = dialog.grab().save(str(target))
        report = {
            "qt_platform": app.platformName(),
            "font_source": "system",
            **font.as_dict(),
            "downloaded": update.downloaded,
            "screenshot_ok": bool(saved and font.cjk_supported and font.latin_supported),
        }
        report_path = target.with_name("application-update-windows-report.json")
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        dialog.close()
        host.close()
        service.shutdown(timeout_ms=0)
        app.processEvents()

    print(target)
    print(report_path)
    return 0 if report["screenshot_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
