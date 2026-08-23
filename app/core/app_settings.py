from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings


class AppSettings:
    """Persistent user preferences backed by the Windows registry."""

    def __init__(self) -> None:
        self._settings = QSettings("SourceFlow", "SourceFlowStudio")
        default_dir = Path.home() / ".youtube-release-studio" / "downloads"
        self.defaults = {
            "download_dir": str(default_dir),
            "quality": "best",
            "proxy": "",
            "filename_template": "%(title)s [%(id)s].%(ext)s",
            "sau_path": "sau",
            "ffmpeg_path": "",
        }

    def get(self, key: str) -> str:
        value = self._settings.value(key, self.defaults.get(key, ""))
        return str(value) if value is not None else self.defaults.get(key, "")

    def set(self, key: str, value: str) -> None:
        self._settings.setValue(key, value)

    def sync(self) -> None:
        self._settings.sync()

