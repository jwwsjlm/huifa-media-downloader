from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot

from app.core.app_settings import AppSettings
from app.core.application_update_service import ApplicationUpdateService
from app.core.application_updater import velopack_persistent_data_dir
from app.core.cover_service import CoverService
from app.core.download_performance import effective_download_performance
from app.core.download_service import DownloadService
from app.core.paths import application_dir
from app.core.publish_service import PublishService
from app.core.update_service import UpdateService
from app.storage.database import Database
from app.storage.secure_store import SecureStore


class ApplicationRuntime(QObject):
    """Own the application services independently from a particular UI toolkit."""

    routeProfilesChanged = Signal(str)

    def __init__(self, data_dir: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.app_settings = AppSettings()
        self.secure_store = SecureStore()
        self.cover_service = CoverService()
        self.db = Database(data_dir / "app.db")
        self.update_service = UpdateService(
            data_dir / "updates",
            {
                "ffmpeg": self.app_settings.get("ffmpeg_path"),
                "ffprobe": self.app_settings.get("ffprobe_path"),
                "deno": self.app_settings.get("deno_path"),
            },
            ffmpeg_build_channel=self.app_settings.get("ffmpeg_build_channel"),
        )
        self.update_service.set_download_routes(
            self.app_settings.get("github_download_route"),
            self.app_settings.get("github_mirror_urls"),
            self.app_settings.get("github_route_profiles"),
        )
        self.update_service.route_probe_finished.connect(self._persist_github_route_profiles)
        self.application_update_dir = data_dir / "updates" / "application"
        self.application_update_mode = (
            "velopack"
            if velopack_persistent_data_dir(application_dir()) is not None
            else ""
        )
        self.application_update_service = ApplicationUpdateService(
            self.application_update_dir,
            updater_factory=None,
            parent=self,
        )
        self.application_updates_supported = bool(self.application_update_mode)
        task_workers, fragment_workers, request_delay = effective_download_performance(
            self.app_settings
        )
        self.download_service = DownloadService(
            self.db,
            max_concurrent=task_workers,
            request_delay=request_delay,
            fragment_concurrent=fragment_workers,
            ytdlp_core_mode=self.app_settings.get("ytdlp_core_mode"),
            deno_path=self.app_settings.get("deno_path"),
            ffprobe_path=self.app_settings.get("ffprobe_path"),
            ytdlp_ejs_source=self.app_settings.get("ytdlp_ejs_source"),
            cover_convert_jpeg=self.app_settings.get_bool("download_cover_convert_jpeg", False),
            cover_jpeg_quality=self.app_settings.get_int("cover_jpeg_quality", 90, 50, 100),
        )
        self.publish_service = PublishService(self.db)
        self.publish_service.recover_stale_tasks()

    def replace_database(self, database: Database) -> None:
        """Switch the shared task database after lifecycle coordination."""

        self.db = database
        self.download_service.db = database
        self.publish_service.db = database

    @Slot(object)
    def _persist_github_route_profiles(self, _results: object) -> None:
        profiles = self.update_service.serialized_route_profiles()
        self.app_settings.set("github_route_profiles", profiles)
        self.routeProfilesChanged.emit(profiles)
