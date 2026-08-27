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

from app.core.update_service import UpdateService
from app.ui.runtime import create_application
from app.ui.runtime_components_dialog import UpdateDialog
from app.ui.theme import THEME_LIGHT, build_application_stylesheet


def main() -> int:
    app, font = create_application([])
    app.setStyleSheet(build_application_stylesheet(THEME_LIGHT))
    with tempfile.TemporaryDirectory() as directory:
        service = UpdateService(Path(directory) / "updates")
        dialog = UpdateDialog(
            [
                {
                    "name": "yt-dlp",
                    "current": "2026.08.19",
                    "source": "Bundled Python module",
                    "runtime_path": "Bundled in application",
                    "latest": "2026.08.25",
                    "assets": [],
                    "managed_by_application": True,
                    "upstream_update_available": True,
                    "url": "https://github.com/yt-dlp/yt-dlp/releases",
                },
                {
                    "name": "FFmpeg",
                    "current": "n7.1.1",
                    "source": "App-local tools folder",
                    "runtime_path": "tools\\ffmpeg\\x64\\ffmpeg.exe",
                    "latest": "n8.0",
                    "assets": [{
                        "name": "ffmpeg-master-latest-win64-gpl.zip",
                        "size": 128 * 1024 * 1024,
                        "browser_download_url": "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
                    }],
                    "auto_install_supported": True,
                    "has_update": True,
                    "metadata_route": "direct",
                    "url": "https://github.com/yt-dlp/FFmpeg-Builds/releases",
                },
                {
                    "name": "Deno",
                    "current": "Not installed",
                    "source": "",
                    "runtime_path": "",
                    "latest": "2.5.0",
                    "assets": [],
                    "auto_install_supported": True,
                    "install_available": True,
                    "metadata_cached": True,
                    "url": "https://github.com/denoland/deno/releases",
                },
            ],
            service,
        )
        dialog.resize(1100, 500)
        dialog.show()
        app.processEvents()

        target = ROOT / "data" / "temp" / "runtime-components-windows.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        saved = dialog.grab().save(str(target))
        report = {
            "qt_platform": app.platformName(),
            "font_source": "system",
            **font.as_dict(),
            "visible_components": dialog.tree.topLevelItemCount(),
            "screenshot_ok": bool(saved and font.cjk_supported and font.latin_supported),
        }
        report_path = target.with_name("runtime-components-windows-report.json")
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        dialog.close()
        service.shutdown(timeout_ms=0)
        app.processEvents()

    print(target)
    print(report_path)
    return 0 if report["screenshot_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
