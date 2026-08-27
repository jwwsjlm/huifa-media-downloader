from __future__ import annotations

import json
import os
import sys
from pathlib import Path


# This verifier must use the real Windows Qt backend so font metrics and
# native combo-box rendering match the packaged desktop application.
os.environ.setdefault("QT_QPA_PLATFORM", os.environ.get("HUIFA_QT_PLATFORM", "windows"))
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ui.download_dialogs import FormatSelectionDialog
from app.ui.runtime import create_application
from app.ui.theme import THEME_LIGHT, build_application_stylesheet


def main() -> int:
    app, font = create_application([])
    app.setStyleSheet(build_application_stylesheet(THEME_LIGHT))
    choices = [
        {
            "kind": "video",
            "label": "1080p source · webm · 60fps · vp9 · HDR · Japanese",
            "selector": "313+bestaudio/best",
            "height": 1080,
            "width": 1080,
            "source_height": 1920,
            "ext": "webm",
            "fps": "60",
            "codec": "vp9",
            "hdr": True,
            "has_audio": False,
            "language": "ja",
            "format_note": "Premium HDR",
        },
        {
            "kind": "video",
            "label": "1080p source · mp4 · 30fps · h264 · video and audio",
            "selector": "22",
            "height": 1080,
            "width": 1920,
            "source_height": 1080,
            "ext": "mp4",
            "fps": "30",
            "codec": "h264",
            "has_audio": True,
        },
        {
            "kind": "video",
            "label": "720p source · mp4 · 30fps · h264",
            "selector": "136+bestaudio/best",
            "height": 720,
            "width": 1280,
            "source_height": 720,
            "ext": "mp4",
            "fps": "30",
            "codec": "h264",
        },
        {
            "kind": "audio",
            "label": "m4a · mp4a.40.2 · 129 kbps · English",
            "selector": "140",
            "ext": "m4a",
            "codec": "mp4a.40.2",
            "abr": 129,
            "language": "en",
        },
    ]
    dialog = FormatSelectionDialog(
        "Vertical HDR sample with a deliberately long title for format selection preview",
        "",
        choices,
    )
    dialog.resize(760, dialog.sizeHint().height())
    dialog.show()
    app.processEvents()

    target = ROOT / "data" / "temp" / "format-selection-windows.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    saved = dialog.grab().save(str(target))
    report = {
        "qt_platform": app.platformName(),
        "font_source": "system",
        **font.as_dict(),
        "dialog_width": dialog.width(),
        "visible_video_choices": dialog.list.count(),
        "screenshot_ok": bool(saved and font.cjk_supported and font.latin_supported),
    }
    report_path = target.with_name("format-selection-windows-report.json")
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    dialog.close()
    app.processEvents()
    print(target)
    print(report_path)
    return 0 if report["screenshot_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
