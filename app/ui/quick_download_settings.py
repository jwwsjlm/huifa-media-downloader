from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QFontMetrics
from PySide6.QtWidgets import QComboBox, QLabel, QMenu, QMessageBox, QPushButton

from app.core.cookie_sources import COOKIE_SOURCE_NONE, normalize_cookie_source
from app.core.download_options import DownloadOptions
from app.core.download_performance import (
    effective_download_performance,
    normalize_download_performance_mode,
)
from app.core.subtitles import normalize_subtitle_language
from app.ui.download_control_presentation import (
    download_quality_text,
    set_combo_current_data,
)
from app.ui.i18n import format_text as ui_format
from app.ui.i18n import runtime_text, text as ui_text


@dataclass(slots=True)
class QuickDownloadControls:
    download_dir_hint: QLabel
    mode_badge: QLabel
    summary: QLabel
    content_menu: QPushButton
    quality_menu: QPushButton
    container: QComboBox
    content_mode: QComboBox
    quality: QComboBox
    video_fps: QComboBox
    source_codec: QComboBox
    vr_mode: QComboBox
    subtitle_language: QComboBox
    audio_track: QComboBox
    content_actions: dict[str, QAction]
    subtitle_actions: dict[str, QAction]
    audio_track_actions: dict[str, QAction]
    quality_actions: dict[str, QAction]
    fps_actions: dict[str, QAction]
    codec_actions: dict[str, QAction]
    vr_actions: dict[str, QAction]
    fps_menu: QMenu
    codec_menu: QMenu
    vr_menu: QMenu


class QuickDownloadSettingsController:
    """Synchronize dashboard shortcuts with the durable download settings."""

    def __init__(self, *, window: Any, controls: QuickDownloadControls) -> None:
        self.window = window
        self.controls = controls
        self.syncing = False
        self._download_dir_text = ""
        self._summary_text = ""
        self._summary_tooltip = ""

    @staticmethod
    def _stored_options(settings: Any) -> dict[str, object]:
        try:
            stored = json.loads(settings.get('download_options_json') or '{}')
        except (TypeError, json.JSONDecodeError):
            return {}
        return stored if isinstance(stored, dict) else {}

    def global_options(self) -> dict[str, object]:
        stored = self._stored_options(self.window.app_settings)
        stored['processing_temp_dir'] = self.window.app_settings.get(
            'processing_temp_dir'
        )
        return DownloadOptions.from_mapping(stored).to_dict()

    def refresh(self) -> None:
        settings = self.window.app_settings
        controls = self.controls
        path = str(settings.get('download_dir') or '')
        self._download_dir_text = f"{ui_text('Current download folder')}: {path}"
        controls.download_dir_hint.setToolTip(self._download_dir_text + ui_text(
            '\nChange the download folder centrally on the Settings page.',
        ))

        options = DownloadOptions.from_mapping(self._stored_options(settings))
        self.syncing = True
        try:
            selections = (
                (controls.content_mode, options.content_mode, 'video'),
                (controls.quality, settings.get('quality') or 'best', 'best'),
                (controls.video_fps, options.video_fps, 'best'),
                (controls.source_codec, options.source_video_codec, 'auto'),
                (controls.vr_mode, options.vr_mode, 'any'),
                (controls.container, options.container, 'auto'),
                (
                    controls.subtitle_language,
                    normalize_subtitle_language(settings.get('subtitle_language')),
                    'none',
                ),
                (controls.audio_track, options.audio_track, 'default'),
            )
            for combo, value, fallback in selections:
                set_combo_current_data(combo, value, fallback=fallback)
        finally:
            self.syncing = False

        self.sync_download_menu()
        self.sync_quality_menu()
        self.sync_format_controls()
        self._refresh_summary_source(options)
        self.refresh_elided_text()

    def _refresh_summary_source(self, options: DownloadOptions) -> None:
        settings = self.window.app_settings
        controls = self.controls
        content_text = {
            'manual': ui_text('Manual'),
            'audio': ui_text('Audio'),
            'video': ui_text('Video'),
        }.get(options.content_mode, ui_text('Video'))
        if options.content_mode == 'audio':
            format_text = (
                ui_text('Best available audio')
                if options.audio_format == 'best'
                else options.audio_format.upper()
            )
        else:
            format_text = (
                ui_text('Automatic')
                if options.container == 'auto'
                else options.container.upper()
            )
        quality = str(controls.quality.currentData() or 'best')
        playlist_mode = str(settings.get('playlist_mode') or 'auto')
        playlist_text = {
            'auto': ui_text('Auto-detect playlist'),
            'single': ui_text('Single video'),
            'playlist': ui_text('Full playlist'),
        }.get(playlist_mode, ui_text('Auto-detect playlist'))
        performance_mode = normalize_download_performance_mode(
            settings.get('download_performance_mode')
        )
        task_workers, fragment_workers, request_delay = (
            effective_download_performance(settings)
        )
        is_smart = performance_mode == 'smart'
        controls.mode_badge.setText(
            ui_text('Smart Download') if is_smart else ui_text('Manual Download')
        )
        summary = f"{content_text} · {format_text} · " + ui_format(
            '{quality} · {playlist} · {tasks} concurrent tasks · {fragments} fragment workers · {delay}s request delay',
            quality=download_quality_text(quality),
            playlist=playlist_text,
            tasks=task_workers,
            fragments=fragment_workers,
            delay=f"{request_delay:g}",
        )
        if str(settings.get('proxy') or '').strip():
            summary += f" · {ui_text('Proxy enabled')}"
        cookie_source = normalize_cookie_source(
            settings.get('download_cookie_source')
        )
        if (
            cookie_source != COOKIE_SOURCE_NONE
            or str(settings.get('download_cookie_file') or '').strip()
        ):
            summary += f" · {ui_text('Cookie configured')}"
        self._summary_text = summary
        self._summary_tooltip = (
            summary
            + '\n'
            + (
                ui_text(
                    'Smart mode derives a balanced profile from local logical processors; switch to Manual in Settings if needed.',
                )
                if is_smart
                else ui_text(
                    'Manual performance values are active; switch to Smart in Settings if preferred.',
                )
            )
            + '\n'
            + ui_text('Changes apply to subsequently created download tasks.')
        )
        controls.summary.setToolTip(self._summary_tooltip)

    def refresh_elided_text(self) -> None:
        """Repaint width-sensitive labels without re-reading durable settings."""

        controls = self.controls
        if self._download_dir_text:
            path_metrics = QFontMetrics(controls.download_dir_hint.font())
            path_width = max(180, controls.download_dir_hint.width())
            controls.download_dir_hint.setText(path_metrics.elidedText(
                self._download_dir_text,
                Qt.ElideMiddle,
                path_width,
            ))
        if self._summary_text:
            summary_metrics = QFontMetrics(controls.summary.font())
            summary_width = max(260, controls.summary.width())
            controls.summary.setText(summary_metrics.elidedText(
                self._summary_text,
                Qt.ElideRight,
                summary_width,
            ))

    def persist(self, *_args) -> None:
        if self.syncing:
            return
        controls = self.controls
        settings = self.window.app_settings
        stored = self._stored_options(settings)
        stored.update({
            'content_mode': str(controls.content_mode.currentData() or 'video'),
            'container': str(controls.container.currentData() or 'auto'),
            'audio_track': str(controls.audio_track.currentData() or 'default'),
            'video_fps': str(controls.video_fps.currentData() or 'best'),
            'source_video_codec': str(
                controls.source_codec.currentData() or 'auto'
            ),
            'vr_mode': str(controls.vr_mode.currentData() or 'any'),
        })
        normalized = DownloadOptions.from_mapping(stored).to_dict()
        quality = str(controls.quality.currentData() or 'best')
        try:
            settings.set_many({
                'quality': quality,
                'subtitle_language': normalize_subtitle_language(
                    controls.subtitle_language.currentData()
                ),
                'download_options_json': json.dumps(
                    normalized,
                    ensure_ascii=False,
                    separators=(',', ':'),
                ),
            })
        except Exception as exc:
            self.refresh()
            QMessageBox.warning(
                self.window,
                ui_text('Save Failed'),
                runtime_text(exc),
            )
            return
        self.sync_settings_page(normalized, quality)
        self.refresh()

    def sync_settings_page(
        self,
        options: dict[str, object],
        quality: str,
    ) -> None:
        settings_page = self.window.settings
        controls = self.controls
        selectors = (
            (settings_page.download_content_mode, options.get('content_mode')),
            (settings_page.quality, quality),
            (settings_page.download_container, options.get('container')),
            (settings_page.download_audio_track, options.get('audio_track')),
            (settings_page.download_video_fps, options.get('video_fps')),
            (settings_page.download_source_codec, options.get('source_video_codec')),
            (settings_page.download_vr_mode, options.get('vr_mode')),
            (
                settings_page.subtitle_language,
                normalize_subtitle_language(controls.subtitle_language.currentData()),
            ),
        )
        for combo, value in selectors:
            index = combo.findData(value)
            if index < 0:
                continue
            previous = combo.blockSignals(True)
            try:
                combo.setCurrentIndex(index)
            finally:
                combo.blockSignals(previous)
        settings_page.download_options_json = dict(options)
        settings_page.update_download_format_controls()

    def sync_format_controls(self, *_args) -> None:
        controls = self.controls
        video_enabled = str(controls.content_mode.currentData() or 'video') != 'audio'
        controls.container.setEnabled(video_enabled)
        controls.container.setToolTip(
            ui_text(
                'Changes here are saved immediately and stay synchronized with Download Settings.'
            )
            if video_enabled
            else ui_text('Final video format does not apply to audio-only downloads.')
        )

    def sync_download_menu(self) -> None:
        controls = self.controls
        content_mode = str(controls.content_mode.currentData() or 'video')
        subtitle = str(controls.subtitle_language.currentData() or 'none')
        audio_track = str(controls.audio_track.currentData() or 'default')
        for value, action in controls.content_actions.items():
            action.setChecked(value == content_mode)
        for value, action in controls.subtitle_actions.items():
            action.setChecked(value == subtitle)
        for value, action in controls.audio_track_actions.items():
            action.setChecked(value == audio_track)
        content_text = {
            'manual': ui_text('Manual'),
            'video': ui_text('Video'),
            'audio': ui_text('Audio'),
        }.get(content_mode, ui_text('Video'))
        controls.content_menu.setText(content_text)
        controls.content_menu.setToolTip(
            f"{ui_text('Download Content')}: {content_text}\n"
            f"{ui_text('Subtitles')}: {controls.subtitle_language.currentText()}\n"
            f"{ui_text('Audio Track')}: {controls.audio_track.currentText()}"
        )

    def sync_quality_menu(self) -> None:
        controls = self.controls
        quality = str(controls.quality.currentData() or 'best')
        fps = str(controls.video_fps.currentData() or 'best')
        codec = str(controls.source_codec.currentData() or 'auto')
        vr_mode = str(controls.vr_mode.currentData() or 'any')
        for value, action in controls.quality_actions.items():
            action.setChecked(value == quality)
        for value, action in controls.fps_actions.items():
            action.setChecked(value == fps)
        for value, action in controls.codec_actions.items():
            action.setChecked(value == codec)
        for value, action in controls.vr_actions.items():
            action.setChecked(value == vr_mode)
        controls.quality_menu.setText(download_quality_text(quality))
        fps_text = controls.video_fps.currentText()
        codec_text = controls.source_codec.currentText()
        vr_text = controls.vr_mode.currentText()
        controls.fps_menu.menuAction().setText(
            f"{ui_text('Frame Rate')} · {fps_text}"
        )
        controls.codec_menu.menuAction().setText(
            f"{ui_text('Video Codec')} · {codec_text}"
        )
        controls.vr_menu.menuAction().setText(f"{ui_text('VR')} · {vr_text}")
        controls.quality_menu.setToolTip(
            f"{ui_text('Download Quality')}: {download_quality_text(quality)}\n"
            f"{ui_text('Frame Rate')}: {fps_text}\n"
            f"{ui_text('Video Codec')}: {codec_text}\n"
            f"{ui_text('VR')}: {vr_text}"
        )
