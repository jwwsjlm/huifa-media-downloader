from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSettings

from app.core.paths import data_dir, downloads_dir


class AppSettings:
    """Portable preferences stored in data/settings.ini beside the software."""

    def __init__(self) -> None:
        settings_path = data_dir() / "settings.ini"
        first_run = not settings_path.exists()
        self._settings = QSettings(str(settings_path), QSettings.IniFormat)
        default_dir = downloads_dir()
        self.defaults = {
            "download_dir": str(default_dir),
            "quality": "best",
            "proxy": "",
            "filename_template": "%(title)s [%(id)s].%(ext)s",
            "sau_path": "sau",
            "ffmpeg_path": "",
            "max_concurrent": "3",
        }
        if first_run:
            self._migrate_legacy_settings()

    def _migrate_legacy_settings(self) -> None:
        legacy = QSettings("SourceFlow", "SourceFlowStudio")
        old_default = str(Path.home() / ".youtube-release-studio" / "downloads")
        for key, default in self.defaults.items():
            value = legacy.value(key, None)
            if value is None:
                continue
            value = str(value)
            if key == "download_dir" and Path(value) == Path(old_default):
                value = default
            self._settings.setValue(key, value)
        self._settings.sync()

    def get(self, key: str) -> str:
        value = self._settings.value(key, self.defaults.get(key, ""))
        return str(value) if value is not None else self.defaults.get(key, "")

    def set(self, key: str, value: str) -> None:
        self._settings.setValue(key, value)

    def sync(self) -> None:
        self._settings.sync()
