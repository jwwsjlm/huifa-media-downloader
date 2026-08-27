from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from PySide6.QtWidgets import QMessageBox, QWidget

from app.adapters.openai_cover_provider import (
    OpenAICoverGenerationError,
    normalize_openai_image_endpoint,
)
from app.core.cookie_sources import (
    COOKIE_SOURCE_FILE,
    normalize_cookie_browser,
    normalize_cookie_source,
)
from app.core.cover_service import CoverFitMode, CoverPresetId
from app.core.download_options import DownloadOptions
from app.core.download_performance import normalize_download_performance_mode
from app.core.download_service import validate_filename_template
from app.core.github_mirrors import normalize_github_route, parse_custom_mirror_urls
from app.core.paths import resolve_portable_path
from app.core.subtitles import normalize_subtitle_language
from app.core.transcode_service import (
    normalize_transcode_encoder,
    transcode_encoder_codec,
    transcode_encoder_device,
)
from app.core.update_service import normalize_ffmpeg_build_channel
from app.core.ytdlp_core_selection import normalize_ytdlp_core_mode
from app.core.ytdlp_ejs import normalize_ytdlp_ejs_source
from app.ui.i18n import format_text as ui_format
from app.ui.i18n import runtime_text, text as ui_text
from app.ui.theme import THEME_SYSTEM

if TYPE_CHECKING:
    from app.ui.settings_page import SettingsPage


@dataclass(frozen=True, slots=True)
class SettingsSavePlan:
    values: dict[str, str]
    success_message: str
    warning_lines: tuple[str, ...] = ()
    secret_updates: tuple[tuple[str, str], ...] = ()
    after_commit: Callable[[], None] | None = None


@dataclass(frozen=True, slots=True)
class DownloadSettingsPaths:
    download_path: Path
    processing_temp_path: Path | None
    filename_template: str
    normalized_temp_dir: str


@dataclass(frozen=True, slots=True)
class DownloadPerformancePlan:
    mode: str
    manual_tasks: int
    manual_fragments: int
    manual_delay: float
    effective_tasks: int
    effective_fragments: int
    effective_delay: float


class SettingsSaveController:
    """Validate, commit and apply one settings group as a small transaction."""

    def __init__(self, window: Any) -> None:
        self.parent = window if isinstance(window, QWidget) else None
        self.app_settings = window.app_settings
        self.secure_store = window.secure_store
        self.download_service = window.download_service
        self.update_service = window.update_service
        self.dashboard = window.dashboard
        self.completed = window.completed
        self.application_updates_supported = bool(window.application_updates_supported)
        self.apply_theme = window.apply_theme
        self.sync_desktop_notification_visibility = (
            window.desktop_notification_controller.sync_visibility
        )
        self.configure_application_updater = lambda: (
            window.application_update_controller.configure(silent=True)
        )

    def save(self, page: SettingsPage, scope: str) -> bool:
        handlers = {
            "download": self.prepare_download,
            "network": self.prepare_network,
            "experience": self.prepare_experience,
            "appearance": self.prepare_appearance,
            "tools": self.prepare_tools,
            "cover": self.prepare_cover,
            "updates": self.prepare_updates,
        }
        handler = handlers.get(str(scope or "").strip().casefold())
        if handler is None:
            return False
        plan = handler(page)
        return self.commit(plan) if plan is not None else False

    def validated_directory(
        self,
        raw_value: str,
        *,
        error_template: str,
    ) -> Path | None:
        path = resolve_portable_path(raw_value)
        try:
            if path.exists() and not path.is_dir():
                raise OSError(ui_text('The target path already exists and is not a folder'))
            path.mkdir(parents=True, exist_ok=True)
            handle, probe_name = tempfile.mkstemp(
                prefix=".huifa-write-test-",
                dir=path,
            )
            os.close(handle)
            Path(probe_name).unlink()
        except OSError as exc:
            QMessageBox.warning(
                self.parent,
                ui_text('Folder Unavailable'),
                ui_format(
                    error_template,
                    path=raw_value,
                    error=runtime_text(exc),
                ),
            )
            return None
        return path

    @staticmethod
    def download_options_from_page(
        page: SettingsPage,
        processing_temp_dir: str,
    ) -> dict[str, object]:
        options = DownloadOptions.from_mapping(
            page.download_options_json
        ).to_dict()
        options.update({
            'content_mode': str(page.download_content_mode.currentData() or 'video'),
            'container': str(page.download_container.currentData() or 'auto'),
            'video_fps': str(page.download_video_fps.currentData() or 'best'),
            'source_video_codec': str(page.download_source_codec.currentData() or 'auto'),
            'vr_mode': str(page.download_vr_mode.currentData() or 'any'),
            'compatibility_target': str(
                page.download_compatibility_target.currentData() or 'auto'
            ),
            'audio_track': str(page.download_audio_track.currentData() or 'default'),
        })
        options['processing_temp_dir'] = processing_temp_dir
        return DownloadOptions.from_mapping(options).to_dict()

    def prepare_download(self, page: SettingsPage) -> SettingsSavePlan | None:
        paths = self.prepare_download_paths(page)
        if paths is None:
            return None
        performance = self.download_performance_plan(page)
        encoder = normalize_transcode_encoder(page.transcode_encoder.currentData())
        saved_download_options = self.download_options_from_page(
            page,
            paths.normalized_temp_dir,
        )
        values = self.download_settings_values(
            page,
            paths,
            performance,
            encoder,
            saved_download_options,
        )

        def apply_download_settings() -> None:
            page.download_options_json = dict(saved_download_options)
            self.download_service.configure_performance(
                max_concurrent=performance.effective_tasks,
                fragment_concurrent=performance.effective_fragments,
                request_delay=performance.effective_delay,
            )
            self.dashboard.refresh_settings()

        return SettingsSavePlan(
            values=values,
            success_message=ui_text('Download settings were saved.'),
            after_commit=apply_download_settings,
        )

    def prepare_download_paths(
        self,
        page: SettingsPage,
    ) -> DownloadSettingsPaths | None:
        download_dir = page.download_dir.text().strip()
        if not download_dir:
            QMessageBox.warning(
                self.parent,
                ui_text('Notice'),
                ui_text('The download folder cannot be empty.'),
            )
            return None
        try:
            filename_template = validate_filename_template(page.template.text())
        except ValueError as exc:
            QMessageBox.warning(
                self.parent,
                ui_text('Invalid Filename Template'),
                runtime_text(exc),
            )
            return None

        download_path = self.validated_directory(
            download_dir,
            error_template='The download folder cannot be used:\n{path}\n\n{error}',
        )
        if download_path is None:
            return None

        processing_temp_dir = page.processing_temp_dir.text().strip()
        processing_temp_path: Path | None = None
        if processing_temp_dir:
            processing_temp_path = self.validated_directory(
                processing_temp_dir,
                error_template=(
                    'The processing temporary folder cannot be used:\n{path}\n\n{error}'
                ),
            )
            if processing_temp_path is None:
                return None
        normalized_temp_dir = self.app_settings.normalize_value(
            "processing_temp_dir",
            str(processing_temp_path) if processing_temp_path is not None else "",
        )
        return DownloadSettingsPaths(
            download_path=download_path,
            processing_temp_path=processing_temp_path,
            filename_template=filename_template,
            normalized_temp_dir=normalized_temp_dir,
        )

    @staticmethod
    def download_performance_plan(page: SettingsPage) -> DownloadPerformancePlan:
        manual_tasks, manual_fragments, manual_delay = (
            page.manual_download_performance_values()
        )
        tasks, fragments, delay = page.effective_download_performance_values()
        return DownloadPerformancePlan(
            mode=normalize_download_performance_mode(page.performance_mode.currentData()),
            manual_tasks=manual_tasks,
            manual_fragments=manual_fragments,
            manual_delay=manual_delay,
            effective_tasks=tasks,
            effective_fragments=fragments,
            effective_delay=delay,
        )

    @staticmethod
    def download_settings_values(
        page: SettingsPage,
        paths: DownloadSettingsPaths,
        performance: DownloadPerformancePlan,
        encoder: str,
        saved_download_options: dict[str, object],
    ) -> dict[str, str]:
        return {
            "download_dir": str(paths.download_path),
            "processing_temp_dir": (
                str(paths.processing_temp_path)
                if paths.processing_temp_path is not None else ""
            ),
            "filename_template": paths.filename_template,
            "organize_task_folder": (
                "true" if page.organize_task_folder.isChecked() else "false"
            ),
            "quality": str(page.quality.currentData() or "best"),
            "transcode_encoder": encoder,
            "transcode_codec": transcode_encoder_codec(encoder),
            "transcode_device": transcode_encoder_device(encoder),
            "subtitle_language": normalize_subtitle_language(
                page.subtitle_language.currentData()
            ),
            "playlist_mode": str(page.playlist_mode.currentData() or "auto"),
            "download_options_json": json.dumps(
                saved_download_options,
                ensure_ascii=False,
                separators=(',', ':'),
            ),
            "download_performance_mode": performance.mode,
            "max_concurrent": str(performance.manual_tasks),
            "fragment_concurrent": str(performance.manual_fragments),
            "request_delay": str(performance.manual_delay),
        }

    def prepare_network(self, page: SettingsPage) -> SettingsSavePlan | None:
        proxy = page.proxy.text().strip()
        if proxy:
            parsed_proxy = urlparse(proxy)
            if (
                parsed_proxy.scheme.lower() not in {
                    "http", "https", "socks4", "socks5", "socks5h",
                }
                or not parsed_proxy.netloc
            ):
                QMessageBox.warning(
                    self.parent,
                    ui_text('Invalid Proxy Address'),
                    ui_text(
                        'Enter a complete download proxy address, for example http://127.0.0.1:7890',
                    ),
                )
                return None

        cookie_file = page.download_cookie_file.text().strip()
        cookie_source = normalize_cookie_source(
            page.download_cookie_source.currentData()
        )
        if cookie_file and cookie_source == COOKIE_SOURCE_FILE:
            cookie_path = resolve_portable_path(cookie_file)
            if not cookie_path.exists() or not cookie_path.is_file():
                QMessageBox.warning(
                    self.parent,
                    ui_text('Cookie File Unavailable'),
                    ui_text(
                        'The download Cookie file does not exist or is not a file. Select a Netscape Cookie file again.',
                    ),
                )
                return None
            cookie_file = str(cookie_path)
        if cookie_source == COOKIE_SOURCE_FILE and not cookie_file:
            QMessageBox.warning(
                self.parent,
                ui_text('Incomplete Cookie Source'),
                ui_text('Select a file before using the Netscape Cookie file source.'),
            )
            return None

        return SettingsSavePlan(
            values={
                "proxy": proxy,
                "download_cookie_file": cookie_file,
                "download_cookie_source": cookie_source,
                "download_cookie_browser": normalize_cookie_browser(
                    page.download_cookie_browser.currentData()
                ),
                "download_cookie_profile": page.download_cookie_profile.text().strip(),
                "download_cookie_keyring": page.download_cookie_keyring.text().strip(),
                "download_cookie_container": page.download_cookie_container.text().strip(),
            },
            success_message=ui_text('Network and Cookie settings were saved.'),
            after_commit=self.dashboard.refresh_settings,
        )

    def prepare_experience(self, page: SettingsPage) -> SettingsSavePlan:
        return SettingsSavePlan(
            values={
                "desktop_notifications": (
                    "true" if page.desktop_notifications.isChecked() else "false"
                ),
            },
            success_message=ui_text('Notification settings were saved.'),
            after_commit=self.sync_desktop_notification_visibility,
        )

    def prepare_appearance(self, page: SettingsPage) -> SettingsSavePlan:
        previous_language = self.app_settings.get("ui_language") or "auto"
        selected_language = str(page.ui_language.currentData() or "auto")
        success_message = ui_text('Appearance settings were saved.')
        if selected_language != previous_language:
            success_message += " " + ui_text(
                'The interface language will take effect after restarting the application.',
            )
        theme = str(page.appearance_theme.currentData() or THEME_SYSTEM)
        return SettingsSavePlan(
            values={
                "appearance_theme": theme,
                "ui_language": selected_language,
            },
            success_message=success_message,
            after_commit=lambda: self.apply_theme(theme),
        )

    def prepare_tools(self, page: SettingsPage) -> SettingsSavePlan:
        deno_path = page.deno.text().strip()
        ffmpeg_path = page.ffmpeg.text().strip()
        ffprobe_path = page.ffprobe.text().strip()
        warning_lines: list[str] = []
        for label, raw_value in (
            ("Deno", deno_path),
            ("FFmpeg", ffmpeg_path),
            ("FFprobe", ffprobe_path),
        ):
            if not raw_value:
                continue
            tool_path = Path(raw_value).expanduser()
            looks_like_path = (
                tool_path.is_absolute()
                or "\\" in raw_value
                or "/" in raw_value
                or tool_path.suffix.lower() == ".exe"
            )
            if looks_like_path and not tool_path.is_absolute():
                tool_path = resolve_portable_path(raw_value)
            if looks_like_path and (not tool_path.exists() or not tool_path.is_file()):
                warning_lines.append(ui_format(
                    '{label} path does not exist: {path}',
                    label=label,
                    path=raw_value,
                ))

        core_mode = normalize_ytdlp_core_mode(page.ytdlp_core_mode.currentData())
        ejs_source = normalize_ytdlp_ejs_source(page.ytdlp_ejs_source.currentData())
        ffmpeg_channel = normalize_ffmpeg_build_channel(
            page.ffmpeg_build_channel.currentData()
        )

        def apply_tool_settings() -> None:
            settings = self.app_settings
            self.download_service.ytdlp_core_mode = core_mode
            self.download_service.deno_path = settings.get("deno_path")
            self.download_service.ffprobe_path = settings.get("ffprobe_path")
            self.download_service.ytdlp_ejs_source = ejs_source
            self.update_service.set_tool_overrides({
                "ffmpeg": settings.get("ffmpeg_path"),
                "ffprobe": settings.get("ffprobe_path"),
                "deno": settings.get("deno_path"),
            })
            self.update_service.set_ffmpeg_build_channel(ffmpeg_channel)
            page.refresh_local_core_versions(force=True)

        return SettingsSavePlan(
            values={
                "ffmpeg_path": ffmpeg_path,
                "ffprobe_path": ffprobe_path,
                "ffmpeg_build_channel": ffmpeg_channel,
                "deno_path": deno_path,
                "ytdlp_core_mode": core_mode,
                "ytdlp_ejs_source": ejs_source,
            },
            success_message=ui_text('Tool settings were saved.'),
            warning_lines=tuple(warning_lines),
            after_commit=apply_tool_settings,
        )

    def prepare_cover(self, page: SettingsPage) -> SettingsSavePlan | None:
        ai_model = page.cover_ai_model.text().strip()
        if not ai_model:
            QMessageBox.warning(
                self.parent,
                ui_text('Model Name Required'),
                ui_text('The GPT Image model name cannot be empty.'),
            )
            return None
        ai_api_url = page.cover_ai_api_url.text().strip()
        try:
            normalized_api_url = normalize_openai_image_endpoint(ai_api_url)
        except OpenAICoverGenerationError as exc:
            QMessageBox.warning(
                self.parent,
                ui_text('Invalid API URL'),
                runtime_text(exc),
            )
            return None

        api_key = page.openai_key.text().strip()
        cover_convert_jpeg = page.cover_convert_jpeg.isChecked()
        cover_quality = page.cover_quality.value()

        def apply_cover_settings() -> None:
            self.download_service.cover_convert_jpeg = cover_convert_jpeg
            self.download_service.cover_jpeg_quality = cover_quality
            page.openai_key.clear()
            if self.secure_store.get("openai_api_key"):
                page.openai_key.setPlaceholderText(
                    ui_text('Saved securely; leave blank to keep it'),
                )
            self.completed.mark_dirty()

        return SettingsSavePlan(
            values={
                "cover_preset": str(
                    page.cover_preset.currentData()
                    or CoverPresetId.LANDSCAPE_16_9.value
                ),
                "cover_fit_mode": str(
                    page.cover_fit.currentData() or CoverFitMode.CROP.value
                ),
                "cover_focus_x": str(page.cover_focus_x.value()),
                "cover_focus_y": str(page.cover_focus_y.value()),
                "download_cover_convert_jpeg": (
                    "true" if cover_convert_jpeg else "false"
                ),
                "cover_jpeg_quality": str(cover_quality),
                "prepend_cover_enabled": (
                    "true" if page.prepend_cover_enabled.isChecked() else "false"
                ),
                "prepend_cover_frames": str(page.prepend_cover_frames.value()),
                "cover_ai_model": ai_model,
                "cover_ai_api_url": normalized_api_url if ai_api_url else "",
            },
            success_message=ui_text('Cover settings were saved.'),
            secret_updates=(("openai_api_key", api_key),) if api_key else (),
            after_commit=apply_cover_settings,
        )

    def prepare_updates(self, page: SettingsPage) -> SettingsSavePlan | None:
        try:
            mirror_urls = "\n".join(parse_custom_mirror_urls(page.github_mirror_urls))
        except ValueError as exc:
            QMessageBox.warning(
                self.parent,
                ui_text('Invalid GitHub Download Route'),
                runtime_text(exc),
            )
            return None
        route_mode = normalize_github_route(page.github_download_route.currentData())
        route_profiles = str(page.github_route_profiles or "{}")

        def apply_update_settings() -> None:
            self.update_service.set_download_routes(
                route_mode,
                mirror_urls,
                route_profiles,
            )
            if self.application_updates_supported:
                self.configure_application_updater()

        return SettingsSavePlan(
            values={
                "auto_check_updates": (
                    "true" if page.auto_check_updates.isChecked() else "false"
                ),
                "update_prerelease": (
                    "true" if page.update_prerelease.isChecked() else "false"
                ),
                "github_download_route": route_mode,
                "github_mirror_urls": mirror_urls,
                "github_route_profiles": route_profiles,
            },
            success_message=ui_text('Update settings were saved.'),
            after_commit=apply_update_settings,
        )

    def write_settings_values(self, values: dict[str, str]) -> None:
        self.app_settings.set_many(values)

    def commit(self, plan: SettingsSavePlan) -> bool:
        secret_previous: list[tuple[str, str | None]] = []
        try:
            for key, value in plan.secret_updates:
                secret_previous.append((key, self.secure_store.get(key)))
                self.secure_store.set(key, value)
            self.write_settings_values(plan.values)
        except Exception as exc:
            for key, previous in reversed(secret_previous):
                try:
                    if previous is None:
                        self.secure_store.delete(key)
                    else:
                        self.secure_store.set(key, previous)
                except Exception:
                    pass
            QMessageBox.warning(
                self.parent,
                ui_text('Save Failed'),
                runtime_text(exc),
            )
            return False

        warning_lines = list(plan.warning_lines)
        if plan.after_commit is not None:
            try:
                plan.after_commit()
            except Exception as exc:
                warning_lines.append(runtime_text(exc))
        success_message = plan.success_message
        if warning_lines:
            success_message += (
                ui_text('\n\nConfiguration warnings:\n')
                + "\n".join(warning_lines)
                + ui_text(
                    '\nThe settings can still be saved, but related features may be unavailable.',
                )
            )
        QMessageBox.information(
            self.parent,
            ui_text('Settings Saved'),
            success_message,
        )
        return True


__all__ = [
    "DownloadPerformancePlan",
    "DownloadSettingsPaths",
    "SettingsSaveController",
    "SettingsSavePlan",
]
