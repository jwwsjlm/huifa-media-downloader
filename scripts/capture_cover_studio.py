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

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QWidget

from app.core.cover_service import CoverService
from app.storage.models import MediaItem
from app.ui.cover_studio import CoverStudioDialog
from app.ui.runtime import create_application
from app.ui.theme import THEME_LIGHT, build_application_stylesheet


class PreviewSettings:
    def __init__(self) -> None:
        self.values = {
            "cover_preset": "portrait_9_16",
            "cover_fit_mode": "crop",
            "cover_jpeg_quality": "90",
            "cover_focus_x": "42",
            "cover_focus_y": "50",
        }

    def get(self, key: str) -> str:
        return str(self.values.get(key, ""))

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

    def set(self, key: str, value: str) -> None:
        self.values[key] = str(value)

    def sync(self) -> None:
        pass


class PreviewWindow(QWidget):
    def __init__(self, cover_service: CoverService) -> None:
        super().__init__()
        self.cover_service = cover_service
        self.app_settings = PreviewSettings()
        self.secure_store = None


def main() -> int:
    app, _font = create_application([], requested_locale="zh-CN")
    app.setStyleSheet(build_application_stylesheet(THEME_LIGHT))
    target_dir = ROOT / "data" / "temp"
    target_dir.mkdir(parents=True, exist_ok=True)
    source_path = target_dir / "cover-studio-source.png"
    source = QImage(1280, 720, QImage.Format.Format_ARGB32)
    source.fill(QColor("#336699"))
    if not source.save(str(source_path), "PNG"):
        return 2

    service = CoverService()
    window = PreviewWindow(service)
    dialog = CoverStudioDialog(
        MediaItem(
            id=1,
            title="封面裁切与导出预览",
            thumbnail_path=str(source_path),
        ),
        window,
    )
    dialog.show()
    app.processEvents()

    target = target_dir / "cover-studio-windows.png"
    saved = dialog.grab().save(str(target))
    dialog.close()
    window.close()
    service.close()
    app.processEvents()
    print(target)
    return 0 if saved else 2


if __name__ == "__main__":
    raise SystemExit(main())
