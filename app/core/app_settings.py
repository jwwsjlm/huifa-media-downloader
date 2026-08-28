from __future__ import annotations

import math
import re
from pathlib import Path

from PySide6.QtCore import QSettings

from app.core.paths import (
    application_dir,
    data_dir,
    downloads_dir,
    portable_path_value,
    resolve_portable_path,
)


_PORTABLE_PATH_KEYS = frozenset({
    "download_dir",
    "processing_temp_dir",
    "download_cookie_file",
    "ffmpeg_path",
    "ffprobe_path",
    "deno_path",
})
_COMMAND_PATH_KEYS = frozenset({"ffmpeg_path", "ffprobe_path", "deno_path"})


def is_transient_bundle_path(value: str) -> bool:
    """Return whether a saved path points into PyInstaller's one-file temp dir."""
    return bool(re.search(r"(?i)(?:^|[\\/])_MEI[^\\/]*(?:[\\/]|$)", str(value or "")))


_DEFAULT_SETTINGS = {
    "processing_temp_dir": "",
    "quality": "best",
    "transcode_codec": "original",
    "transcode_device": "auto",
    "transcode_encoder": "original",
    "subtitle_language": "none",
    "playlist_mode": "auto",
    "download_options_json": "{}",
    "filename_template": "%(title)s [%(id)s].%(ext)s",
    "organize_task_folder": "false",
    "download_performance_mode": "smart",
    "max_concurrent": "3",
    "fragment_concurrent": "12",
    "request_delay": "0",
    "proxy": "",
    "download_cookie_file": "",
    "download_cookie_source": "none",
    "download_cookie_browser": "chrome",
    "download_cookie_profile": "",
    "download_cookie_keyring": "",
    "download_cookie_container": "",
    "ffmpeg_path": "",
    "ffprobe_path": "",
    "ffmpeg_build_channel": "nvenc_13_0",
    "deno_path": "",
    "ytdlp_ejs_source": "auto",
    "ytdlp_core_mode": "auto",
    "github_download_route": "auto",
    "github_mirror_urls": "",
    "github_route_profiles": "{}",
    "ui_language": "auto",
    "desktop_notifications": "true",
    "appearance_theme": "system",
    "publish_target_platforms": "",
    "auto_check_updates": "true",
    "update_prerelease": "false",
    "update_channel": "",
    "cover_preset": "landscape_16_9",
    "cover_fit_mode": "crop",
    "cover_focus_x": "50",
    "cover_focus_y": "50",
    "download_cover_convert_jpeg": "false",
    "cover_jpeg_quality": "90",
    "prepend_cover_enabled": "false",
    "prepend_cover_frames": "3",
    "cover_ai_model": "gpt-image-2",
    "cover_ai_api_url": "",
}


def default_settings(default_download_dir: str) -> dict[str, str]:
    """Return a fresh authoritative settings snapshot for one application."""
    return {"download_dir": default_download_dir, **_DEFAULT_SETTINGS}


class AppSettings:
    """Portable preferences stored in data/settings.ini beside the software."""

    def __init__(self) -> None:
        settings_path = data_dir() / "settings.ini"
        self._settings = QSettings(str(settings_path), QSettings.IniFormat)
        app_root = application_dir()
        persistent_root = data_dir()
        default_dir = portable_path_value(
            downloads_dir(), application_root=app_root, persistent_root=persistent_root
        )
        self.defaults = default_settings(default_dir)

    def _portable_path_value(self, value: str) -> str:
        return portable_path_value(
            value,
            application_root=application_dir(),
            persistent_root=data_dir(),
        )

    def get(self, key: str) -> str:
        value = self._settings.value(key, self.defaults.get(key, ""))
        return str(value) if value is not None else self.defaults.get(key, "")

    def normalize_value(self, key: str, value: str) -> str:
        """Normalize one value exactly as it will be stored in settings.ini."""

        value = str(value if value is not None else "")
        if key in _PORTABLE_PATH_KEYS:
            value = value.strip()
        if key in _COMMAND_PATH_KEYS and is_transient_bundle_path(value):
            value = ""
        elif key in _PORTABLE_PATH_KEYS and value:
            value = self._portable_path_value(value)
        return value

    def set(self, key: str, value: str) -> None:
        value = self.normalize_value(key, value)
        self._settings.setValue(key, value)

    def set_many(self, values: dict[str, str]) -> dict[str, str]:
        """Persist one logical settings group and roll it back on write failure."""

        normalized = {
            str(key): self.normalize_value(str(key), value)
            for key, value in values.items()
        }
        previous = {
            key: (self._settings.contains(key), self._settings.value(key))
            for key in normalized
        }
        try:
            for key, value in normalized.items():
                self._settings.setValue(key, value)
            self._settings.sync()
            status = self._settings.status()
            if status != QSettings.NoError:
                raise OSError(f"QSettings write failed with status {status}")
        except Exception:
            for key, (existed, old_value) in previous.items():
                if existed:
                    self._settings.setValue(key, old_value)
                else:
                    self._settings.remove(key)
            self._settings.sync()
            raise
        return normalized

    def get_resolved_path(self, key: str) -> str:
        """Return a configured path ready for filesystem/subprocess use."""
        value = self.get(key).strip()
        if not value:
            return ""
        if key in _COMMAND_PATH_KEYS and not Path(value).is_absolute() and not any(
            char in value for char in ("/", "\\")
        ):
            return value
        return str(resolve_portable_path(value, application_dir()))

    def get_bool(self, key: str, default: bool = False) -> bool:
        value = self.get(key).strip().casefold()
        if value in {"1", "true", "yes", "on", "enabled"}:
            return True
        if value in {"0", "false", "no", "off", "disabled"}:
            return False
        return bool(default)

    def get_int(self, key: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
        try:
            value = int(self.get(key))
        except (TypeError, ValueError, OverflowError):
            value = int(default)
        if minimum is not None:
            value = max(int(minimum), value)
        if maximum is not None:
            value = min(int(maximum), value)
        return value

    def get_float(
        self,
        key: str,
        default: float,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> float:
        fallback = float(default)
        if not math.isfinite(fallback):
            fallback = 0.0
        try:
            value = float(self.get(key))
        except (TypeError, ValueError, OverflowError):
            value = fallback
        if not math.isfinite(value):
            value = fallback
        if minimum is not None:
            value = max(float(minimum), value)
        if maximum is not None:
            value = min(float(maximum), value)
        return value

    def sync(self) -> None:
        self._settings.sync()
