from __future__ import annotations

import json
import os
import platform
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.core.cookie_sources import (
    COOKIE_BROWSER_LABELS,
    COOKIE_SOURCE_BROWSER,
    COOKIE_SOURCE_EMBEDDED,
    COOKIE_SOURCE_FILE,
    COOKIE_SOURCE_NONE,
    SUPPORTED_COOKIE_BROWSERS,
    normalize_cookie_browser,
    normalize_cookie_source,
)
from app.core.cover_service import CoverFitMode
from app.core.download_options import AUDIO_TRACKS, DownloadOptions
from app.core.download_performance import (
    normalize_download_performance_mode,
    smart_download_performance,
)
from app.core.github_mirrors import (
    ROUTE_AUTO,
    github_download_routes,
    normalize_github_route,
)
from app.core.language_packs import discover_language_packs, language_pack_directory
from app.core.paths import resolve_portable_path
from app.core.subtitles import normalize_subtitle_language
from app.core.transcode_service import (
    clear_ffmpeg_encoder_cache,
    normalize_transcode_encoder,
)
from app.core.update_service import (
    FFMPEG_BUILD_LATEST,
    FFMPEG_BUILD_NVENC_LEGACY,
    normalize_ffmpeg_build_channel,
)
from app.core.version import APP_UPDATE_REPOSITORY, APP_VERSION
from app.core.ytdlp_core_selection import normalize_ytdlp_core_mode
from app.core.ytdlp_ejs import normalize_ytdlp_ejs_source
from app.ui.cover_studio import populate_cover_preset_combo
from app.ui.download_control_presentation import (
    AUDIO_TRACK_LABELS,
    DOWNLOAD_QUALITY_VALUES,
    SUBTITLE_LANGUAGE_LABELS,
    TRANSCODE_ENCODER_NATIVE_LABELS,
    download_quality_text,
    set_combo_current_data,
    transcode_encoder_label,
)
from app.ui.download_cookie_controller import DownloadCookieController
from app.ui.download_options import AdvancedDownloadOptionsDialog
from app.ui.diagnostics_export_controller import export_diagnostics
from app.ui.github_route_presentation import github_route_display_name
from app.ui.github_routes_dialog import GithubMirrorDialog
from app.ui.i18n import format_text as ui_format
from app.ui.i18n import runtime_text, text as ui_text
from app.ui.local_core_version_controller import LocalCoreVersionController
from app.ui.path_widgets import CompactPathLineEdit, PortablePathLineEdit
from app.ui.runtime_component_presentation import (
    build_runtime_component_presentation,
    runtime_result_component,
)
from app.ui.runtime_component_update_controller import RuntimeComponentUpdateController
from app.ui.theme import (
    THEME_DARK,
    THEME_LIGHT,
    THEME_SYSTEM,
    normalize_theme,
)
from app.ui.widget_behavior import ExplicitWheelFocusGuard

if TYPE_CHECKING:
    from app.ui.main_window import MainWindow


class SettingsPage(QWidget):
    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.window = window
        self._initialize_settings_state(window)
        root, scroll, content = self._build_settings_shell()

        root.addWidget(self._build_download_group(window))

        root.addWidget(self._build_network_group(window))
        root.addWidget(self._build_experience_group(window))
        root.addWidget(self._build_appearance_group(window))

        root.addWidget(self._build_tools_group(window))
        self.local_core_versions = LocalCoreVersionController(self)
        self.runtime_component_updates = RuntimeComponentUpdateController(self)
        self._connect_runtime_update_service(window)
        root.addWidget(self._build_cover_group(window))

        root.addWidget(self._build_diagnostics_group(window))
        root.addWidget(self._build_update_group(window))

        root.addStretch(1)

        self._wheel_focus_guard = ExplicitWheelFocusGuard(scroll, self)
        guarded_types = (QComboBox, QSpinBox, QDoubleSpinBox)
        for widget in content.findChildren(QWidget):
            if isinstance(widget, guarded_types):
                self._wheel_focus_guard.watch(widget)

    def _initialize_settings_state(self, window: "MainWindow") -> None:
        try:
            saved_download_options = json.loads(window.app_settings.get('download_options_json') or '{}')
        except (TypeError, json.JSONDecodeError):
            saved_download_options = {}
        self.download_options_json = DownloadOptions.from_mapping(
            saved_download_options if isinstance(saved_download_options, dict) else {}
        ).to_dict()
        self.github_mirror_urls = window.app_settings.get("github_mirror_urls")
        self.github_route_profiles = window.app_settings.get("github_route_profiles") or "{}"
        self._local_core_details: dict[str, tuple[str, str, str]] = {}
        self.runtime_version_labels: dict[str, QLabel] = {}
        self.runtime_update_buttons: dict[str, QPushButton] = {}

    def _build_settings_shell(self) -> tuple[QVBoxLayout, QScrollArea, QWidget]:
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        content = QWidget()
        content.setMinimumWidth(0)
        content.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        root = QVBoxLayout(content)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)
        scroll.setWidget(content)
        outer_layout.addWidget(scroll)
        title = QLabel(ui_text('Settings', context="settings.title"))
        title.setObjectName("pageTitle")
        subtitle = QLabel(ui_text(
            'Configure the download folder and defaults for new tasks here. The download page uses these settings automatically.',
        ))
        subtitle.setObjectName("mutedText")
        root.addWidget(title)
        root.addWidget(subtitle)
        return root, scroll, content

    def _build_download_group(self, window: "MainWindow") -> QGroupBox:
        self._initialize_download_path_controls(window)
        transcode_encoder_row = self._initialize_download_format_controls(window)
        performance_row = self._initialize_download_behavior_controls(window)

        download_group = QGroupBox(ui_text(
            'Download Settings',
            context="settings.download_group",
        ))
        download_form = QFormLayout(download_group)
        download_form.setContentsMargins(14, 14, 14, 14)
        download_form.setVerticalSpacing(10)
        download_form.addRow(ui_text('Download Folder'), self._path_row(self.download_dir, ui_text('Choose Folder'), self.choose_download_dir))
        download_form.addRow(
            ui_text('Processing Temporary Folder'),
            self._path_row(
                self.processing_temp_dir,
                ui_text('Choose Folder'),
                self.choose_processing_temp_dir,
            ),
        )
        download_form.addRow(ui_text('Filename Template'), self.template)
        download_form.addRow(ui_text('Task Folder'), self.organize_task_folder)
        download_form.addRow(ui_text('Default Content Type'), self.download_content_mode)
        download_form.addRow(ui_text('Default Video Format'), self.download_container)
        download_form.addRow(ui_text('Default Quality'), self.quality)
        download_form.addRow(ui_text('Audio Track'), self.download_audio_track)
        download_form.addRow(ui_text('Frame Rate Preference'), self.download_video_fps)
        download_form.addRow(ui_text('Source Video Codec Preference'), self.download_source_codec)
        download_form.addRow(ui_text('VR'), self.download_vr_mode)
        download_form.addRow(ui_text('Playback Compatibility'), self.download_compatibility_target)
        download_form.addRow(ui_text('Subtitle Language'), self.subtitle_language)
        download_form.addRow(ui_text('Video Encoder'), transcode_encoder_row)
        download_form.addRow(ui_text('Playlist Mode'), self.playlist_mode)
        download_form.addRow(ui_text('Performance Mode'), performance_row)
        download_form.addRow(ui_text('Concurrent Tasks'), self.max_concurrent)
        download_form.addRow(ui_text('Fragment Workers'), self.fragment_concurrent)
        download_form.addRow(ui_text('Request Delay'), self.request_delay)
        self.request_delay.setToolTip(ui_text(
            'Minimum delay between requests. Use 0 for no extra delay, or 1–3 seconds when a site applies rate limits or anti-bot checks.',
        ))
        self.performance_mode.currentIndexChanged.connect(self.update_performance_controls)
        self.update_download_format_controls()
        self.update_performance_controls()
        download_actions = QWidget()
        download_actions_layout = QHBoxLayout(download_actions)
        download_actions_layout.setContentsMargins(0, 0, 0, 0)
        readiness_button = QPushButton(ui_text('Download Environment'))
        readiness_button.setToolTip(ui_text(
            'Check whether the download core, FFmpeg, Deno, folder and cookies are ready for new tasks.',
        ))
        readiness_button.clicked.connect(lambda: window.dashboard.show_download_readiness())
        download_actions_layout.addWidget(readiness_button)
        advanced_defaults = QPushButton(ui_text('Advanced Defaults'))
        advanced_defaults.setToolTip(ui_text('Configure bounded yt-dlp features used by new tasks'))
        advanced_defaults.clicked.connect(self.edit_default_download_options)
        download_actions_layout.addWidget(advanced_defaults)
        download_actions_layout.addStretch(1)
        download_form.addRow(ui_text('Check'), download_actions)
        download_form.addRow("", self._group_save_button("download"))
        return download_group

    def _initialize_download_path_controls(self, window: "MainWindow") -> None:
        self.download_dir = PortablePathLineEdit(window.app_settings.get("download_dir"))
        self.download_dir.setClearButtonEnabled(True)
        self.processing_temp_dir = PortablePathLineEdit(
            window.app_settings.get("processing_temp_dir")
        )
        self.processing_temp_dir.setClearButtonEnabled(True)
        self.processing_temp_dir.setPlaceholderText(ui_text(
            'Leave blank to process temporary files beside the final download',
        ))
        self.processing_temp_dir.setToolTip(ui_text(
            'Optional. Select any available folder for fragments, merging and transcoding. Verified results are moved to the download folder; clearing this field restores the default same-folder behavior.',
        ))

    def _initialize_download_format_controls(self, window: "MainWindow") -> QWidget:
        self._initialize_download_media_controls(window)
        return self._initialize_download_postprocessing_controls(window)

    def _initialize_download_media_controls(self, window: "MainWindow") -> None:
        self.quality = QComboBox()
        for quality_value in DOWNLOAD_QUALITY_VALUES:
            self.quality.addItem(download_quality_text(quality_value), quality_value)
        saved_quality = window.app_settings.get("quality") or "best"
        set_combo_current_data(self.quality, saved_quality, fallback="best")
        self.quality.setToolTip(ui_text(
            'Default quality for new downloads. Manual parses a single video first and waits for your video or audio selection.',
        ))
        default_download_options = DownloadOptions.from_mapping(self.download_options_json)
        self.download_content_mode = QComboBox()
        self.download_content_mode.addItem(ui_text('Manual'), 'manual')
        self.download_content_mode.addItem(ui_text('Video'), 'video')
        self.download_content_mode.addItem(ui_text('Audio'), 'audio')
        set_combo_current_data(
            self.download_content_mode,
            default_download_options.content_mode,
            fallback="video",
        )
        self.download_content_mode.setToolTip(ui_text(
            'Choose video, audio, or Manual. Manual asks you to confirm the content type after parsing each submitted single-video link.',
        ))
        self.download_container = QComboBox()
        for label, value in (
            (ui_text('Automatic'), 'auto'),
            ('MP4', 'mp4'),
            ('MKV', 'mkv'),
        ):
            self.download_container.addItem(label, value)
        set_combo_current_data(
            self.download_container,
            default_download_options.container,
            fallback="auto",
        )
        self.download_container.setToolTip(ui_text(
            'Choose the final container for new video tasks. Automatic keeps yt-dlp compatibility decisions.',
        ))
        self.download_audio_track = QComboBox()
        for audio_track in AUDIO_TRACKS:
            translation_key = AUDIO_TRACK_LABELS[audio_track]
            label = ui_text(translation_key)
            if audio_track not in {'default', 'original', 'all'}:
                label = f'{label} ({audio_track})'
            self.download_audio_track.addItem(label, audio_track)
        set_combo_current_data(
            self.download_audio_track,
            default_download_options.audio_track,
            fallback="default",
        )
        self.download_audio_track.setToolTip(ui_text(
            'Prefer the original or a selected language when the site exposes multiple audio tracks. Missing languages fall back to the default track; All audio tracks enables multi-audio merging.',
        ))
        self.download_video_fps = QComboBox()
        self.download_video_fps.addItem(ui_text('Highest available frame rate'), 'best')
        for fps in ('240', '120', '60', '50', '30', '25', '24'):
            self.download_video_fps.addItem(f'{fps} FPS', fps)
        set_combo_current_data(
            self.download_video_fps,
            default_download_options.video_fps,
            fallback="best",
        )
        self.download_video_fps.setToolTip(ui_text(
            'Frame rate is a preference after resolution. If unavailable, yt-dlp selects another frame rate instead of failing.',
        ))
        self.download_source_codec = QComboBox()
        for label, value in (
            (ui_text('Automatic codec'), 'auto'),
            ('H.264 / AVC', 'h264'),
            ('H.265 / HEVC', 'h265'),
            ('AV1', 'av1'),
            ('VP9', 'vp9'),
        ):
            self.download_source_codec.addItem(label, value)
        set_combo_current_data(
            self.download_source_codec,
            default_download_options.source_video_codec,
            fallback="auto",
        )
        self.download_source_codec.setToolTip(ui_text(
            'Prefer a source video codec after resolution and frame rate. This does not transcode and falls back when the site does not provide that codec.',
        ))
        self.download_vr_mode = QComboBox()
        for label, value in (
            (ui_text('Any'), 'any'),
            ('2D / 360°', '2d360'),
            ('3D / 180°', '3d180'),
            ('3D / 360°', '3d360'),
            (ui_text('No VR'), 'none'),
        ):
            self.download_vr_mode.addItem(label, value)
        set_combo_current_data(
            self.download_vr_mode,
            default_download_options.vr_mode,
            fallback="any",
        )
        self.download_vr_mode.setToolTip(ui_text(
            'Prefer a VR layout when the extractor exposes it in format metadata. Unsupported sites fall back to the normal best format.',
        ))
        self.download_compatibility_target = QComboBox()
        for label, value in (
            (ui_text('Automatic compatibility'), 'auto'),
            ('Windows', 'windows'),
            ('macOS', 'macos'),
            ('Linux', 'linux'),
            ('iOS', 'ios'),
            ('Android', 'android'),
        ):
            self.download_compatibility_target.addItem(label, value)
        set_combo_current_data(
            self.download_compatibility_target,
            default_download_options.compatibility_target,
            fallback="auto",
        )
        self.download_compatibility_target.setToolTip(ui_text(
            'Automatic keeps yt-dlp defaults. Device presets choose a broadly compatible container and source codec unless you explicitly choose them.',
        ))
        self.download_content_mode.currentIndexChanged.connect(
            self.update_download_format_controls
        )

    def _initialize_download_postprocessing_controls(
        self,
        window: "MainWindow",
    ) -> QWidget:
        self.subtitle_language = QComboBox()
        for language_code, translation_key in SUBTITLE_LANGUAGE_LABELS.items():
            label = ui_text(translation_key)
            if language_code != "none":
                label = f"{label} ({language_code})"
            self.subtitle_language.addItem(label, language_code)
        saved_subtitle_language = normalize_subtitle_language(
            window.app_settings.get("subtitle_language")
        )
        subtitle_index = self.subtitle_language.findData(saved_subtitle_language)
        self.subtitle_language.setCurrentIndex(max(0, subtitle_index))
        self.subtitle_language.setToolTip(ui_text(
            'Prefer creator-uploaded subtitles in the selected language, fall back to automatic subtitles, and skip subtitles when neither exists.',
        ))
        self.transcode_encoder = QComboBox()
        self.transcode_encoder_status = QLabel(ui_text(
            'Reading encoders provided by the current FFmpeg…',
        ))
        self.transcode_encoder_status.setObjectName("mutedText")
        self.transcode_encoder_status.setWordWrap(True)
        encoder_tooltip = ui_text(
            'Shows common encoders provided by the current FFmpeg. GPU entries are marked GPU, and hardware compatibility is checked when transcoding starts. The selected encoder is used strictly without switching to another encoder. Keep original disables transcoding.',
        )
        self.transcode_encoder.setToolTip(encoder_tooltip)
        self.transcode_encoder_status.setToolTip(encoder_tooltip)
        saved_transcode_encoder = normalize_transcode_encoder(
            window.app_settings.get("transcode_encoder")
        )
        self.transcode_encoder.addItem(transcode_encoder_label("original"), "original")
        if saved_transcode_encoder != "original":
            self.transcode_encoder.addItem(
                transcode_encoder_label(saved_transcode_encoder),
                saved_transcode_encoder,
            )
            self.transcode_encoder.setCurrentIndex(1)
        self.transcode_encoder.setEnabled(False)
        transcode_encoder_row = QWidget()
        transcode_encoder_layout = QVBoxLayout(transcode_encoder_row)
        transcode_encoder_layout.setContentsMargins(0, 0, 0, 0)
        transcode_encoder_layout.setSpacing(3)
        transcode_encoder_layout.addWidget(self.transcode_encoder)
        transcode_encoder_layout.addWidget(self.transcode_encoder_status)
        return transcode_encoder_row

    def _initialize_download_behavior_controls(self, window: "MainWindow") -> QWidget:
        self.playlist_mode = QComboBox()
        self.playlist_mode.addItem(ui_text('Auto-detect (recommended)'), "auto")
        self.playlist_mode.addItem(ui_text('Single video only'), "single")
        self.playlist_mode.addItem(ui_text('Entire album/playlist'), "playlist")
        saved_mode = window.app_settings.get("playlist_mode")
        if saved_mode not in {"auto", "single", "playlist"}:
            saved_mode = "auto"
        self.playlist_mode.setCurrentIndex(max(0, self.playlist_mode.findData(saved_mode)))
        self.playlist_mode.setToolTip(ui_text(
            'How new tasks handle video, album and playlist links by default.',
        ))
        self.template = QLineEdit(window.app_settings.get("filename_template"))
        self.template.setClearButtonEnabled(True)
        self.organize_task_folder = QCheckBox(ui_text(
            'Store each download task in its own folder',
        ))
        self.organize_task_folder.setChecked(
            window.app_settings.get_bool("organize_task_folder", False)
        )
        self.organize_task_folder.setToolTip(ui_text(
            'Disabled by default. When enabled, each new video task stores its media, cover, subtitles, description and info.json together in a Windows-safe folder. Existing tasks are unchanged.',
        ))
        self.performance_mode = QComboBox()
        self.performance_mode.addItem(ui_text('Smart (recommended)'), "smart")
        self.performance_mode.addItem(ui_text('Manual'), "manual")
        saved_performance_mode = normalize_download_performance_mode(
            window.app_settings.get("download_performance_mode")
        )
        self.performance_mode.setCurrentIndex(
            max(0, self.performance_mode.findData(saved_performance_mode))
        )
        self.performance_mode.setToolTip(ui_text(
            'Smart mode chooses balanced concurrency from local logical processors and caps fragment workers at 8 to reduce anti-bot risk. Switch to Manual to raise it yourself. No hardware information is uploaded.',
        ))
        self.max_concurrent = QSpinBox()
        self.max_concurrent.setRange(1, 8)
        self.max_concurrent.setValue(window.app_settings.get_int("max_concurrent", 3, 1, 8))
        self.fragment_concurrent = QSpinBox()
        self.fragment_concurrent.setRange(1, 32)
        self.fragment_concurrent.setValue(window.app_settings.get_int("fragment_concurrent", 12, 1, 32))
        self.fragment_concurrent.setSuffix(ui_text(' workers'))
        self.fragment_concurrent.setToolTip(ui_text(
            'Concurrent DASH/HLS fragments per video. Smart mode is capped at 8; switch to Manual for values up to 32. Higher concurrency may trigger throttling or anti-bot checks, and progressive single-file streams may not benefit.',
        ))
        self.request_delay = QDoubleSpinBox()
        self.request_delay.setRange(0, 60)
        self.request_delay.setSingleStep(0.5)
        self.request_delay.setDecimals(1)
        self.request_delay.setSuffix(ui_text(' s'))
        self.request_delay.setValue(window.app_settings.get_float("request_delay", 0.0, 0.0, 60.0))
        self._manual_performance_values = (
            self.max_concurrent.value(),
            self.fragment_concurrent.value(),
            self.request_delay.value(),
        )
        performance_row = QWidget()
        performance_layout = QHBoxLayout(performance_row)
        performance_layout.setContentsMargins(0, 0, 0, 0)
        performance_layout.setSpacing(10)
        performance_layout.addWidget(self.performance_mode)
        self.performance_summary = QLabel()
        self.performance_summary.setObjectName("mutedText")
        self.performance_summary.setWordWrap(True)
        performance_layout.addWidget(self.performance_summary, 1)
        return performance_row

    def _build_diagnostics_group(self, window: "MainWindow") -> QGroupBox:
        group = QGroupBox(ui_text('Diagnostics and Logs'))
        layout = QHBoxLayout(group)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.addWidget(QLabel(ui_text(
            'Download logs distinguish anti-bot, login, network, proxy and format problems.',
        )), 1)
        open_logs = QPushButton(ui_text('Open Log Folder'))
        open_logs.clicked.connect(window.open_log_directory)
        export_logs = QPushButton(ui_text('Export Diagnostics'))
        export_logs.clicked.connect(lambda _checked=False: export_diagnostics(
            window,
            window.download_service.logs,
            window.app_settings,
            lambda: window.db,
        ))
        layout.addWidget(open_logs)
        layout.addWidget(export_logs)
        return group

    def _build_tools_group(self, window: "MainWindow") -> QGroupBox:
        self._initialize_tool_controls(window)
        group = QGroupBox(ui_text('Tools'))
        form = QFormLayout(group)
        form.setContentsMargins(14, 14, 14, 14)
        form.setVerticalSpacing(10)

        self.ytdlp_core_mode = QComboBox()
        self.ytdlp_core_mode.addItem(
            ui_text('Auto Select (recommended)'),
            "auto",
        )
        self.ytdlp_core_mode.addItem(
            ui_text('Standalone yt-dlp (independently updatable)'),
            "external",
        )
        self.ytdlp_core_mode.addItem(
            ui_text('Bundled yt-dlp (updates with app)'),
            "builtin",
        )
        saved_core_mode = normalize_ytdlp_core_mode(
            window.app_settings.get("ytdlp_core_mode")
        )
        self.ytdlp_core_mode.setCurrentIndex(
            max(0, self.ytdlp_core_mode.findData(saved_core_mode))
        )
        self.ytdlp_core_mode.setToolTip(ui_text(
            'Auto prefers the standalone core and falls back to bundled. Standalone mode never silently falls back.',
        ))
        form.addRow(
            self._runtime_component_label(
                "yt-dlp",
                ui_text('yt-dlp Download Core'),
            ),
            self._runtime_component_row(self.ytdlp_core_mode, "yt-dlp"),
        )
        form.addRow(
            self._runtime_component_label(
                "yt-dlp-ejs",
                ui_text('yt-dlp-ejs Source'),
            ),
            self._runtime_component_row(self.ytdlp_ejs_source, "yt-dlp-ejs"),
        )
        form.addRow(
            self._runtime_component_label("Deno", ui_text('Deno Path')),
            self._runtime_component_row(self.deno, "Deno"),
        )
        form.addRow(ui_text('FFmpeg Build'), self.ffmpeg_build_channel)
        form.addRow(
            self._runtime_component_label("FFmpeg", ui_text('FFmpeg Path')),
            self._runtime_component_row(self.ffmpeg, "FFmpeg"),
        )
        form.addRow(
            self._runtime_component_label("FFprobe", ui_text('FFprobe Path')),
            self._runtime_component_row(self.ffprobe, "FFprobe"),
        )

        publishing_core_note = QLabel(ui_text(
            'The publishing core, Python runtime dependencies and Chromium are bundled with the application. End users do not install Python or choose an external upload executable.',
        ))
        publishing_core_note.setObjectName("mutedText")
        publishing_core_note.setWordWrap(True)
        form.addRow(ui_text('Publishing Core'), publishing_core_note)

        local_core_actions = QWidget()
        local_core_layout = QHBoxLayout(local_core_actions)
        local_core_layout.setContentsMargins(0, 0, 0, 0)
        local_core_layout.setSpacing(10)
        manage_cores = QPushButton(ui_text('Open Local Core Manager'))
        manage_cores.clicked.connect(window.check_updates)
        refresh_cores = QPushButton(ui_text('Refresh Versions'))
        refresh_cores.clicked.connect(
            lambda: self.refresh_runtime_component_status(
                force_remote=True,
                force_local=True,
            )
        )
        local_core_note = QLabel(ui_text(
            'On a new PC, install or update every download core at once without relying on the system PATH.',
        ))
        local_core_note.setObjectName("mutedText")
        local_core_note.setWordWrap(True)
        local_core_layout.addWidget(manage_cores)
        local_core_layout.addWidget(refresh_cores)
        local_core_layout.addWidget(local_core_note, 1)
        form.addRow(ui_text('App-local Cores'), local_core_actions)
        form.addRow("", self._group_save_button("tools"))
        return group

    def _initialize_tool_controls(self, window: "MainWindow") -> None:
        self.ffmpeg = CompactPathLineEdit(window.app_settings.get("ffmpeg_path"))
        self.ffmpeg.setClearButtonEnabled(True)
        self.ffmpeg.setPlaceholderText(ui_text(
            'Leave blank to use FFmpeg from the app folder or bundled runtime',
        ))

        self.ffmpeg_build_channel = QComboBox()
        self.ffmpeg_build_channel.addItem(
            ui_text('Latest build (recommended)'),
            FFMPEG_BUILD_LATEST,
        )
        self.ffmpeg_build_channel.addItem(
            ui_text('Legacy GPU build (NVENC API 13.0, 2026-05-31)'),
            FFMPEG_BUILD_NVENC_LEGACY,
        )
        saved_ffmpeg_channel = normalize_ffmpeg_build_channel(
            window.app_settings.get("ffmpeg_build_channel")
        )
        self.ffmpeg_build_channel.setCurrentIndex(max(
            0,
            self.ffmpeg_build_channel.findData(saved_ffmpeg_channel),
        ))
        self.ffmpeg_build_channel.setToolTip(ui_text(
            'Latest follows the rolling yt-dlp/FFmpeg-Builds release. The legacy GPU option is pinned to the tested 2026-05-31 build for older NVIDIA drivers that provide NVENC API 13.0; switching replaces the app-local FFmpeg/FFprobe.',
        ))
        self.ffmpeg_build_channel.currentIndexChanged.connect(
            self._ffmpeg_build_channel_changed
        )

        self.ffprobe = CompactPathLineEdit(
            window.app_settings.get("ffprobe_path")
        )
        self.ffprobe.setClearButtonEnabled(True)
        self.ffprobe.setPlaceholderText(ui_text(
            'Leave blank to use FFprobe beside the selected FFmpeg',
        ))
        self.ffprobe.setToolTip(ui_text(
            'Used to validate completed media files; normally installed beside FFmpeg.',
        ))

        self.deno = CompactPathLineEdit(window.app_settings.get("deno_path"))
        self.deno.setClearButtonEnabled(True)
        self.deno.setPlaceholderText(ui_text(
            'Leave blank to find Deno in the app folder, tools folder, or system PATH',
        ))
        self.deno.setToolTip(ui_text(
            "yt-dlp's recommended JavaScript runtime for yt-dlp-ejs. The minimum version depends on the current yt-dlp build.",
        ))

        self.ytdlp_ejs_source = QComboBox()
        self.ytdlp_ejs_source.addItem(
            ui_text('Auto (local first, npm fallback)'),
            "auto",
        )
        self.ytdlp_ejs_source.addItem(
            ui_text('npm remote component (no GitHub required)'),
            "npm",
        )
        self.ytdlp_ejs_source.addItem(
            ui_text('GitHub remote component'),
            "github",
        )
        self.ytdlp_ejs_source.addItem(
            ui_text('App-local core only (no remote fetch)'),
            "local",
        )
        saved_ejs_source = normalize_ytdlp_ejs_source(
            window.app_settings.get("ytdlp_ejs_source")
        )
        self.ytdlp_ejs_source.setCurrentIndex(max(
            0,
            self.ytdlp_ejs_source.findData(saved_ejs_source),
        ))
        self.ytdlp_ejs_source.setToolTip(ui_text(
            'Auto prefers the independently updatable yt-dlp-ejs wheel in the app tools folder and uses Deno/npm only when the local core is missing.',
        ))

    def _connect_runtime_update_service(self, window: "MainWindow") -> None:
        service = window.update_service
        controller = self.runtime_component_updates
        service.finished.connect(controller.remote_versions_ready)
        service.failed.connect(controller.remote_versions_failed)
        service.result_ready.connect(controller.remote_version_ready)
        service.download_failed.connect(controller.component_download_failed)
        service.install_finished.connect(controller.component_installed)
        service.install_failed.connect(controller.component_install_failed)

    def _build_cover_group(self, window: "MainWindow") -> QGroupBox:
        group = QGroupBox(ui_text('Cover Settings'))
        form = QFormLayout(group)
        form.setContentsMargins(14, 14, 14, 14)
        form.setVerticalSpacing(10)

        self.cover_preset = QComboBox()
        populate_cover_preset_combo(self.cover_preset)
        self.cover_preset.setCurrentIndex(max(
            0,
            self.cover_preset.findData(window.app_settings.get("cover_preset")),
        ))
        self.cover_fit = QComboBox()
        self.cover_fit.addItem(
            ui_text('Smart crop to fill'),
            CoverFitMode.CROP.value,
        )
        self.cover_fit.addItem(
            ui_text('Keep full image with padding'),
            CoverFitMode.PAD.value,
        )
        self.cover_fit.setCurrentIndex(max(
            0,
            self.cover_fit.findData(window.app_settings.get("cover_fit_mode")),
        ))

        self.cover_focus_x = QSpinBox()
        self.cover_focus_x.setRange(0, 100)
        self.cover_focus_x.setSuffix(" %")
        self.cover_focus_x.setValue(
            window.app_settings.get_int("cover_focus_x", 50, 0, 100)
        )
        self.cover_focus_x.setToolTip(ui_text(
            '0% left, 50% center, 100% right',
        ))
        self.cover_focus_y = QSpinBox()
        self.cover_focus_y.setRange(0, 100)
        self.cover_focus_y.setSuffix(" %")
        self.cover_focus_y.setValue(
            window.app_settings.get_int("cover_focus_y", 50, 0, 100)
        )
        self.cover_focus_y.setToolTip(ui_text(
            '0% top, 50% center, 100% bottom',
        ))
        cover_focus_row = QWidget()
        cover_focus_layout = QHBoxLayout(cover_focus_row)
        cover_focus_layout.setContentsMargins(0, 0, 0, 0)
        cover_focus_layout.setSpacing(6)
        cover_focus_layout.addWidget(QLabel(ui_text('Horizontal')))
        cover_focus_layout.addWidget(self.cover_focus_x)
        cover_focus_layout.addWidget(QLabel(ui_text('Vertical')))
        cover_focus_layout.addWidget(self.cover_focus_y)

        self.cover_quality = QSpinBox()
        self.cover_quality.setRange(50, 100)
        self.cover_quality.setSuffix(" %")
        self.cover_quality.setValue(
            window.app_settings.get_int("cover_jpeg_quality", 90, 50, 100)
        )
        self.cover_convert_jpeg = QCheckBox(ui_text(
            'Automatically convert downloaded cover images to JPG',
        ))
        self.cover_convert_jpeg.setChecked(
            window.app_settings.get_bool("download_cover_convert_jpeg", False)
        )
        self.cover_convert_jpeg.setToolTip(ui_text(
            'When enabled, WebP, PNG, AVIF and other downloaded cover images are saved as JPG using the quality below. The original dimensions are preserved.',
        ))

        self.prepend_cover_enabled = QCheckBox(ui_text(
            'Insert the downloaded cover before the original video',
        ))
        self.prepend_cover_enabled.setChecked(
            window.app_settings.get_bool("prepend_cover_enabled", False)
        )
        self.prepend_cover_enabled.setToolTip(ui_text(
            'Disabled by default. Inserts new cover frames before the first original frame and delays both the original video and audio together, preserving synchronization.',
        ))
        self.prepend_cover_frames = QSpinBox()
        self.prepend_cover_frames.setRange(1, 300)
        self.prepend_cover_frames.setValue(
            window.app_settings.get_int("prepend_cover_frames", 3, 1, 300)
        )
        self.prepend_cover_frames.setToolTip(ui_text(
            'Number of cover frames inserted at the final video frame rate. For example, 3 frames equal 0.1 seconds at 30 FPS.',
        ))
        prepend_cover_row = QWidget()
        prepend_cover_layout = QHBoxLayout(prepend_cover_row)
        prepend_cover_layout.setContentsMargins(0, 0, 0, 0)
        prepend_cover_layout.setSpacing(8)
        prepend_cover_layout.addWidget(self.prepend_cover_enabled, 1)
        prepend_cover_layout.addWidget(self.prepend_cover_frames)
        prepend_cover_layout.addWidget(QLabel(ui_text('frames')))
        self.prepend_cover_enabled.toggled.connect(
            self.prepend_cover_frames.setEnabled
        )
        self.prepend_cover_frames.setEnabled(
            self.prepend_cover_enabled.isChecked()
        )

        self.cover_ai_model = QLineEdit(
            window.app_settings.get("cover_ai_model") or "gpt-image-2"
        )
        self.cover_ai_model.setClearButtonEnabled(True)
        self.cover_ai_model.setToolTip(ui_text(
            'The model name may vary with OpenAI account access and API version.',
        ))
        self.cover_ai_api_url = QLineEdit(
            window.app_settings.get("cover_ai_api_url")
        )
        self.cover_ai_api_url.setClearButtonEnabled(True)
        self.cover_ai_api_url.setPlaceholderText(ui_text(
            'Leave blank for OpenAI; enter a compatible service /v1 URL',
        ))
        self.cover_ai_api_url.setToolTip(ui_text(
            'Accepts an HTTP/HTTPS API base URL or a complete /images/edits endpoint. With HTTP, the API key is sent over an unencrypted connection; use only a trusted service.',
        ))

        self.openai_key = QLineEdit()
        self.openai_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.openai_key.setClearButtonEnabled(True)
        self.openai_key.setPlaceholderText(
            ui_text('Saved securely; leave blank to keep it')
            if window.secure_store.get("openai_api_key")
            else ui_text('Enter a key to save it in Windows Credential Manager')
        )
        clear_key = QPushButton(ui_text('Clear Key'))
        clear_key.clicked.connect(self.clear_openai_api_key)
        key_row = QWidget()
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.addWidget(self.openai_key, 1)
        key_layout.addWidget(clear_key)

        form.addRow(ui_text('Default Cover Size'), self.cover_preset)
        form.addRow(
            ui_text('Fit Mode', context="settings.cover_fit"),
            self.cover_fit,
        )
        form.addRow(ui_text('Default Crop Focus'), cover_focus_row)
        form.addRow(ui_text('Downloaded Cover Format'), self.cover_convert_jpeg)
        form.addRow(ui_text('JPG Quality'), self.cover_quality)
        form.addRow(ui_text('Video Opening Cover'), prepend_cover_row)
        form.addRow(ui_text('GPT Image Model'), self.cover_ai_model)
        form.addRow(ui_text('OpenAI API URL'), self.cover_ai_api_url)
        form.addRow(ui_text('OpenAI API Key'), key_row)

        cover_note = QLabel(ui_text(
            'The API URL is stored in regular settings. The API key is stored only in the system credential vault and is never written to settings.ini, the database, diagnostic bundles or task logs.',
        ))
        cover_note.setObjectName("mutedText")
        cover_note.setWordWrap(True)
        form.addRow(ui_text('Security Note'), cover_note)
        self.cover_fit.currentIndexChanged.connect(
            self.update_cover_focus_controls
        )
        self.update_cover_focus_controls()
        form.addRow("", self._group_save_button("cover"))
        return group

    def clear_openai_api_key(self) -> None:
        answer = QMessageBox.question(
            self,
            ui_text("Clear API Key"),
            ui_text(
                "The OpenAI API Key will be removed from the system credential vault. Continue?",
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.window.secure_store.delete("openai_api_key")
        except Exception as exc:
            QMessageBox.warning(
                self,
                ui_text("Clear Failed"),
                ui_format(
                    "Cannot access the system credential vault:\n{error}",
                    error=runtime_text(exc),
                ),
            )
            return

        self.openai_key.clear()
        self.openai_key.setPlaceholderText(ui_text(
            "Enter a key to save it in Windows Credential Manager",
        ))
        QMessageBox.information(
            self,
            ui_text("Cleared"),
            ui_text(
                "The OpenAI API key was removed from the system credential vault.",
            ),
        )

    def _build_network_group(self, window: "MainWindow") -> QGroupBox:
        self._initialize_network_controls(window)
        group = QGroupBox(ui_text('Network Settings'))
        form = QFormLayout(group)
        form.setContentsMargins(14, 14, 14, 14)
        form.addRow(ui_text('Download Proxy (download only)'), self.proxy)
        form.addRow(ui_text('Download Cookie Source'), self.download_cookie_source)

        browser_row = QWidget()
        browser_layout = QHBoxLayout(browser_row)
        browser_layout.setContentsMargins(0, 0, 0, 0)
        browser_layout.setSpacing(8)
        browser_layout.addWidget(self.download_cookie_browser)
        browser_layout.addWidget(self.download_cookie_profile, 1)
        form.addRow(ui_text('Browser / Profile'), browser_row)

        advanced_cookie_row = QWidget()
        advanced_cookie_layout = QHBoxLayout(advanced_cookie_row)
        advanced_cookie_layout.setContentsMargins(0, 0, 0, 0)
        advanced_cookie_layout.setSpacing(8)
        advanced_cookie_layout.addWidget(self.download_cookie_keyring, 1)
        advanced_cookie_layout.addWidget(self.download_cookie_container, 1)
        form.addRow(ui_text('Advanced (optional)'), advanced_cookie_row)
        form.addRow(
            ui_text('Netscape Cookie File'),
            self._path_row(
                self.download_cookie_file,
                ui_text('Browse'),
                self.choose_download_cookie_file,
            ),
        )

        cookie_actions = QWidget()
        cookie_actions_layout = QHBoxLayout(cookie_actions)
        cookie_actions_layout.setContentsMargins(0, 0, 0, 0)
        self.open_cookie_login_button = QPushButton(ui_text('Open login page'))
        check_cookie_button = QPushButton(ui_text('Check cookies'))
        view_cookie_button = QPushButton(ui_text('View cookies'))
        cookie_actions_layout.addWidget(self.open_cookie_login_button)
        cookie_actions_layout.addWidget(check_cookie_button)
        cookie_actions_layout.addWidget(view_cookie_button)
        cookie_actions_layout.addStretch(1)
        form.addRow(ui_text('Browser actions'), cookie_actions)

        self.download_cookie_controller = DownloadCookieController(self)
        self.open_cookie_login_button.clicked.connect(
            self.download_cookie_controller.open_login
        )
        check_cookie_button.clicked.connect(
            self.download_cookie_controller.check_source
        )
        view_cookie_button.clicked.connect(
            self.download_cookie_controller.open_viewer
        )
        self.download_cookie_source.currentIndexChanged.connect(
            self.download_cookie_controller.update_controls
        )
        window.publish_service.account_status.connect(
            self.download_cookie_controller.login_result
        )
        self.download_cookie_controller.update_controls()
        form.addRow("", self._group_save_button("network"))
        return group

    def _initialize_network_controls(self, window: "MainWindow") -> None:
        self.proxy = QLineEdit(window.app_settings.get("proxy"))
        self.proxy.setClearButtonEnabled(True)
        self.proxy.setPlaceholderText(ui_text(
            'Optional, e.g. http://127.0.0.1:7890',
        ))
        self.proxy.setToolTip(ui_text(
            'Used only for download requests; publishing tasks do not inherit this proxy.',
        ))

        self.download_cookie_file = PortablePathLineEdit(
            window.app_settings.get("download_cookie_file")
        )
        self.download_cookie_source = QComboBox()
        self.download_cookie_source.addItem(
            ui_text('Do not use cookies'),
            COOKIE_SOURCE_NONE,
        )
        self.download_cookie_source.addItem(
            ui_text('Embedded browser cookies (recommended)'),
            COOKIE_SOURCE_EMBEDDED,
        )
        self.download_cookie_source.addItem(
            ui_text('Read existing browser cookies (advanced)'),
            COOKIE_SOURCE_BROWSER,
        )
        self.download_cookie_source.addItem(
            ui_text('Netscape cookie file'),
            COOKIE_SOURCE_FILE,
        )
        saved_cookie_source = normalize_cookie_source(
            window.app_settings.get("download_cookie_source")
        )
        if (
            saved_cookie_source == COOKIE_SOURCE_NONE
            and window.app_settings.get("download_cookie_file").strip()
        ):
            saved_cookie_source = COOKIE_SOURCE_FILE
        self.download_cookie_source.setCurrentIndex(max(
            0,
            self.download_cookie_source.findData(saved_cookie_source),
        ))

        self.download_cookie_browser = QComboBox()
        for browser in SUPPORTED_COOKIE_BROWSERS:
            self.download_cookie_browser.addItem(
                COOKIE_BROWSER_LABELS[browser],
                browser,
            )
        saved_cookie_browser = normalize_cookie_browser(
            window.app_settings.get("download_cookie_browser")
        )
        self.download_cookie_browser.setCurrentIndex(max(
            0,
            self.download_cookie_browser.findData(saved_cookie_browser),
        ))

        self.download_cookie_file.setClearButtonEnabled(True)
        self.download_cookie_profile = QLineEdit(
            window.app_settings.get("download_cookie_profile")
        )
        self.download_cookie_keyring = QLineEdit(
            window.app_settings.get("download_cookie_keyring")
        )
        self.download_cookie_container = QLineEdit(
            window.app_settings.get("download_cookie_container")
        )
        self.download_cookie_profile.setPlaceholderText(ui_text(
            'Optional profile; blank uses browser default',
        ))
        self.download_cookie_keyring.setPlaceholderText(ui_text(
            'Optional keyring',
        ))
        self.download_cookie_container.setPlaceholderText(ui_text(
            'Optional Firefox container',
        ))
        self.download_cookie_file.setPlaceholderText(ui_text(
            'Optional, for login-required or age-restricted videos',
        ))
        self.download_cookie_file.setToolTip(ui_text(
            'Choose a Netscape Cookie text file exported by a browser. Only its path is saved; cookie contents are not written to the database or logs.',
        ))

    def _build_experience_group(self, window: "MainWindow") -> QGroupBox:
        group = QGroupBox(ui_text('Notifications and Experience'))
        layout = QVBoxLayout(group)
        layout.setContentsMargins(14, 12, 14, 12)
        self.desktop_notifications = QCheckBox(ui_text(
            'Notify when downloads or publishing tasks finish or fail while the window is in the background',
        ))
        self.desktop_notifications.setChecked(
            window.app_settings.get_bool("desktop_notifications", True)
        )
        self.desktop_notifications.setToolTip(ui_text(
            'Show results through Windows notifications. No duplicate notification is shown while the window is active; closing the main window still exits the app.',
        ))
        layout.addWidget(self.desktop_notifications)

        notification_note = QLabel(ui_text(
            'Click a notification to return to the task. Notifications are only reminders; full status and failure details remain in the task list and logs.',
        ))
        notification_note.setObjectName("mutedText")
        notification_note.setWordWrap(True)
        layout.addWidget(notification_note)
        if not window.desktop_notifications_available:
            self.desktop_notifications.setEnabled(False)
            notification_note.setText(ui_text(
                'System notifications are unavailable in this desktop environment; task status will still be shown in the app.',
            ))
        layout.addWidget(self._group_save_button("experience"))
        return group

    def _build_appearance_group(self, window: "MainWindow") -> QGroupBox:
        group = QGroupBox(ui_text('Appearance'))
        form = QFormLayout(group)
        form.setContentsMargins(14, 12, 14, 12)

        self.appearance_theme = QComboBox()
        self.appearance_theme.addItem(ui_text('Follow System'), THEME_SYSTEM)
        self.appearance_theme.addItem(ui_text('Light'), THEME_LIGHT)
        self.appearance_theme.addItem(ui_text('Dark'), THEME_DARK)
        saved_theme = normalize_theme(window.app_settings.get("appearance_theme"))
        saved_theme_index = self.appearance_theme.findData(saved_theme)
        self.appearance_theme.setCurrentIndex(max(0, saved_theme_index))
        self.appearance_theme.setToolTip(ui_text(
            'Follow System updates the interface automatically when Windows switches between light and dark mode.',
        ))
        form.addRow(ui_text('Interface Theme'), self.appearance_theme)

        self.ui_language = QComboBox()
        self.ui_language.addItem(
            ui_text('Match system language automatically'),
            "auto",
        )
        packs = sorted(
            discover_language_packs().values(),
            key=lambda item: item.native_name.casefold(),
        )
        for pack in packs:
            label = f"{runtime_text(pack.native_name)} ({pack.locale})"
            self.ui_language.addItem(label, pack.locale)
            index = self.ui_language.count() - 1
            authors = (
                "、".join(pack.authors)
                if pack.authors
                else ui_text('Not specified')
            )
            self.ui_language.setItemData(index, ui_format(
                'Language pack: {filename}\nAuthors: {authors}',
                filename=pack.path.name,
                authors=authors,
            ), Qt.ToolTipRole)
        saved_language = window.app_settings.get("ui_language") or "auto"
        self.ui_language.setCurrentIndex(
            max(0, self.ui_language.findData(saved_language))
        )

        language_row = QWidget()
        language_layout = QHBoxLayout(language_row)
        language_layout.setContentsMargins(0, 0, 0, 0)
        language_layout.setSpacing(8)
        language_layout.addWidget(self.ui_language, 1)
        open_languages = QPushButton(ui_text('Language Pack Folder'))
        open_languages.clicked.connect(getattr(
            window,
            "open_language_pack_directory",
            lambda: os.startfile(str(language_pack_directory())),
        ))
        language_layout.addWidget(open_languages)
        form.addRow(ui_text('Interface Language'), language_row)

        appearance_note = QLabel(ui_text(
            'Language packs are UTF-8 JSON files in the app-local data/languages folder. Missing system languages fall back to Simplified Chinese; language changes apply after restart.',
        ))
        appearance_note.setObjectName("mutedText")
        appearance_note.setWordWrap(True)
        form.addRow(ui_text('Note'), appearance_note)
        form.addRow("", self._group_save_button("appearance"))
        return group

    def _build_update_group(self, window: "MainWindow") -> QGroupBox:
        group = QGroupBox(ui_text('Application and Tool Updates'))
        layout = QGridLayout(group)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(8)

        self.update_repo = QLabel(APP_UPDATE_REPOSITORY)
        self.update_repo.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.update_repo.setToolTip(ui_text(
            'Application updates use Velopack for both installed and portable builds; user data and independently updated tools remain outside the replaced current directory.',
        ))
        layout.addWidget(QLabel(ui_text('Current Version')), 0, 0)
        layout.addWidget(QLabel(APP_VERSION), 0, 1)
        layout.addWidget(QLabel(ui_text('Application Repository')), 1, 0)
        layout.addWidget(self.update_repo, 1, 1)

        self.application_update_button = QPushButton(ui_text('Check Application'))
        self.application_update_button.setMinimumWidth(110)
        self.application_update_button.clicked.connect(window.check_application_update)
        layout.addWidget(self.application_update_button, 1, 2, Qt.AlignVCenter)

        options_row = QWidget()
        options_layout = QHBoxLayout(options_row)
        options_layout.setContentsMargins(0, 0, 0, 0)
        self.auto_check_updates = QCheckBox(ui_text(
            'Check automatically after startup (at most once every 24 hours)',
        ))
        self.auto_check_updates.setChecked(
            window.app_settings.get_bool("auto_check_updates", True)
        )
        self.update_prerelease = QCheckBox(ui_text('Receive Prerelease Builds'))
        self.update_prerelease.setChecked(
            window.app_settings.get_bool("update_prerelease", False)
        )
        options_layout.addWidget(self.auto_check_updates)
        options_layout.addWidget(self.update_prerelease)
        options_layout.addStretch(1)
        layout.addWidget(QLabel(ui_text('Update Policy')), 2, 0)
        layout.addWidget(options_row, 2, 1, 1, 2)

        self.application_update_status = QLabel(ui_text(
            'Application update has not been checked yet',
        ))
        self.application_update_status.setObjectName("mutedText")
        layout.addWidget(self.application_update_status, 3, 0, 1, 3)
        if not window.application_updates_supported:
            self.application_update_button.setEnabled(False)
            self.auto_check_updates.setEnabled(False)
            self.update_prerelease.setEnabled(False)
            self.application_update_status.setText(ui_text(
                'The app is running from source/development mode; application updates are available in official releases.',
            ))
        else:
            self.application_update_status.setText(ui_text(
                'Velopack-managed updates support full and delta packages for installed and directory-portable builds.',
            ))

        self.check_updates_button = QPushButton(ui_text('Check Runtime'))
        self.check_updates_button.setMinimumWidth(110)
        self.check_updates_button.clicked.connect(window.check_updates)
        layout.addWidget(QLabel(ui_text('Runtime Components')), 4, 0)
        layout.addWidget(
            QLabel(ui_text(
                'Choose auto, standalone, or bundled yt-dlp. Standalone updates independently; bundled updates with the app.',
            )),
            4,
            1,
        )
        layout.addWidget(self.check_updates_button, 4, 2, Qt.AlignVCenter)

        self.github_download_route = QComboBox()
        self.refresh_github_route_combo(
            window.app_settings.get("github_download_route")
        )
        self.github_download_route.setToolTip(ui_text(
            'This route is used for runtime-component Release metadata and asset downloads. Third-party CDNs/proxies may have synchronization delays.',
        ))
        manage_routes = QPushButton(ui_text('Test and Manage'))
        manage_routes.setMinimumWidth(110)
        manage_routes.clicked.connect(self.open_github_mirror_dialog)
        layout.addWidget(QLabel(ui_text('Component GitHub Route')), 5, 0)
        layout.addWidget(self.github_download_route, 5, 1)
        layout.addWidget(manage_routes, 5, 2, Qt.AlignVCenter)

        self.update_status = QLabel(ui_text(
            'Runtime components have not been checked yet',
        ))
        self.update_status.setObjectName("mutedText")
        layout.addWidget(self.update_status, 6, 0, 1, 3)
        layout.addWidget(self._group_save_button("updates"), 7, 0, 1, 3)
        layout.setColumnStretch(1, 1)
        return group

    def edit_default_download_options(self) -> None:
        options = dict(self.download_options_json)
        options['content_mode'] = str(
            self.download_content_mode.currentData() or 'video'
        )
        options['container'] = str(
            self.download_container.currentData() or 'auto'
        )
        options['audio_track'] = str(
            self.download_audio_track.currentData() or 'default'
        )
        options['video_fps'] = str(
            self.download_video_fps.currentData() or 'best'
        )
        options['source_video_codec'] = str(
            self.download_source_codec.currentData() or 'auto'
        )
        options['vr_mode'] = str(self.download_vr_mode.currentData() or 'any')
        options['compatibility_target'] = str(
            self.download_compatibility_target.currentData() or 'auto'
        )
        dialog = AdvancedDownloadOptionsDialog(options, self)
        if dialog.exec() == QDialog.Accepted:
            self.download_options_json = dialog.options()
            updated = DownloadOptions.from_mapping(self.download_options_json)
            for combo, value in (
                (self.download_audio_track, updated.audio_track),
                (self.download_video_fps, updated.video_fps),
                (self.download_source_codec, updated.source_video_codec),
                (self.download_vr_mode, updated.vr_mode),
                (self.download_compatibility_target, updated.compatibility_target),
            ):
                combo.setCurrentIndex(max(0, combo.findData(value)))

    def update_download_format_controls(self, *_args) -> None:
        audio_only = self.download_content_mode.currentData() == 'audio'
        self.download_container.setEnabled(not audio_only)
        self.download_video_fps.setEnabled(not audio_only)
        self.download_source_codec.setEnabled(not audio_only)
        self.download_vr_mode.setEnabled(not audio_only)
        self.download_compatibility_target.setEnabled(not audio_only)
        self.download_container.setToolTip(
            ui_text('Final video format does not apply to audio-only downloads.')
            if audio_only
            else ui_text('Choose the final container for new video tasks. Automatic keeps yt-dlp compatibility decisions.')
        )

    @staticmethod
    def _path_row(field: QLineEdit, label: str, callback) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(field, 1)
        button = QPushButton(label)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return container

    def _group_save_button(self, scope: str) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addStretch(1)
        button = QPushButton(ui_text('Save Settings'))
        button.setProperty("settingsGroupSave", True)
        button.setMinimumWidth(100)
        button.setProperty("settingsScope", scope)
        button.clicked.connect(lambda: self.window.save_settings(self, scope))
        layout.addWidget(button)
        return container

    def _runtime_component_label(self, component: str, title: str) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 4, 0)
        layout.setSpacing(1)
        name = QLabel(title)
        # The previous hard 128 px cap clipped "yt-dlp Download Core" on the
        # real Windows font. Size the label from the translated title while
        # keeping the form's left column bounded for narrow windows.
        title_width = name.fontMetrics().horizontalAdvance(title) + 12
        label_width = max(128, min(title_width, 190))
        container.setFixedWidth(label_width)
        name.setWordWrap(title_width > label_width)
        name.setToolTip(title if title_width > label_width else "")
        version = QLabel(ui_text('Detecting…'))
        version.setObjectName("mutedText")
        version.setTextFormat(Qt.RichText)
        version.setTextInteractionFlags(Qt.LinksAccessibleByMouse)
        version.setCursor(Qt.PointingHandCursor)
        version.linkActivated.connect(
            lambda _link, name=component: self.request_runtime_component_update(name)
        )
        version.setAccessibleName(ui_format(
            '{component} current local version',
            component=component,
        ))
        self.runtime_version_labels[component] = version
        layout.addWidget(name)
        layout.addWidget(version)
        return container

    def _runtime_component_row(self, control: QWidget, component: str) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(control, 1)
        button = QPushButton(ui_text('Check Update'))
        button.setMinimumWidth(82)
        button.clicked.connect(lambda _checked=False, name=component: self.request_runtime_component_update(name))
        self.runtime_update_buttons[component] = button
        layout.addWidget(button)
        return container

    def _render_runtime_component_statuses(self) -> None:
        for component in ("yt-dlp", "yt-dlp-ejs", "Deno", "FFmpeg", "FFprobe"):
            self._render_runtime_component_status(component)

    def _render_runtime_component_status(self, component: str) -> None:
        label = self.runtime_version_labels.get(component)
        button = self.runtime_update_buttons.get(component)
        if label is None or button is None:
            return
        presentation = build_runtime_component_presentation(
            component,
            self._local_core_details.get(
                component,
                (ui_text('Detecting…'), "", ""),
            ),
            self.runtime_component_updates.result(component),
            remote_checking=self.runtime_component_updates.remote_checking,
            remote_error=self.runtime_component_updates.remote_error,
            installing_component=self.runtime_component_updates.installing_component,
        )
        label.setText(presentation.label_text)
        label.setCursor(
            Qt.PointingHandCursor
            if presentation.label_clickable
            else Qt.ArrowCursor
        )
        label.setToolTip(presentation.label_tooltip)
        button.setText(presentation.button_text)
        button.setEnabled(presentation.button_enabled)
        button.setToolTip(presentation.button_tooltip)

    def refresh_runtime_component_status(
        self,
        *,
        force_remote: bool = False,
        force_local: bool = False,
    ) -> None:
        self.runtime_component_updates.refresh(
            force_remote=force_remote,
            force_local=force_local,
        )

    def _ffmpeg_build_channel_changed(self, _index: int = -1) -> None:
        self.runtime_component_updates.ffmpeg_build_channel_changed(
            str(self.ffmpeg_build_channel.currentData() or "")
        )

    def request_runtime_component_update(self, component: str) -> None:
        self.runtime_component_updates.request_update(component)

    def _apply_installed_runtime_paths(self, result) -> None:
        installed = {Path(path).name.casefold(): str(path) for path in result.paths}
        component = runtime_result_component(str(result.component))
        values: dict[str, str] = {}
        if component == "Deno" and installed.get("deno.exe"):
            values["deno_path"] = installed["deno.exe"]
        elif component == "FFmpeg":
            if installed.get("ffmpeg.exe"):
                values["ffmpeg_path"] = installed["ffmpeg.exe"]
            if installed.get("ffprobe.exe"):
                values["ffprobe_path"] = installed["ffprobe.exe"]
        if not values:
            return

        stored = self.window.app_settings.set_many(values)
        if component == "Deno":
            stored_path = stored["deno_path"]
            self.deno.setText(stored_path)
            self.window.download_service.deno_path = stored_path
        else:
            clear_ffmpeg_encoder_cache()
            if stored.get("ffmpeg_path"):
                self.ffmpeg.setText(stored["ffmpeg_path"])
            if stored.get("ffprobe_path"):
                stored_ffprobe = stored["ffprobe_path"]
                self.ffprobe.setText(stored_ffprobe)
                self.window.download_service.ffprobe_path = stored_ffprobe
        self.window.update_service.set_tool_overrides({
            "ffmpeg": self.ffmpeg.text().strip(),
            "ffprobe": self.ffprobe.text().strip(),
            "deno": self.deno.text().strip(),
        })

    def refresh_github_route_combo(self, selected: str | None = None) -> None:
        if selected is None:
            selected = str(self.github_download_route.currentData() or ROUTE_AUTO)
        selected = normalize_github_route(selected)
        self.github_download_route.blockSignals(True)
        self.github_download_route.clear()
        self.github_download_route.addItem(ui_text('Auto Test (recommended)'), ROUTE_AUTO)
        for route in github_download_routes(self.github_mirror_urls):
            self.github_download_route.addItem(github_route_display_name(route.id, route.name), route.id)
        index = self.github_download_route.findData(selected)
        self.github_download_route.setCurrentIndex(max(0, index))
        self.github_download_route.blockSignals(False)

    def open_github_mirror_dialog(self) -> None:
        GithubMirrorDialog(self, self).exec()

    def refresh_local_core_versions(self, *, force: bool = False) -> None:
        self.local_core_versions.refresh(force=force)

    def _show_local_core_loading(self) -> None:
        self.transcode_encoder.setEnabled(False)
        self.transcode_encoder_status.setText(ui_text(
            'Reading encoders provided by the current FFmpeg…',
        ))
        for component in ("yt-dlp", "yt-dlp-ejs", "Deno", "FFmpeg", "FFprobe"):
            self._local_core_details[component] = (ui_text('Detecting…'), "", "")
        self._render_runtime_component_statuses()

    def _show_local_core_start_failure(self, error: Exception) -> None:
        error_text = runtime_text(error)
        self._render_transcode_encoder_options((), error_text)
        for component in ("yt-dlp", "yt-dlp-ejs", "Deno", "FFmpeg", "FFprobe"):
            self._local_core_details[component] = ("检测失败", error_text, "")
        self._render_runtime_component_statuses()

    def _apply_local_core_versions(self, results: object) -> None:
        detected = results if isinstance(results, dict) else {}
        encoder_result = detected.get("__video_encoders__", {})
        if isinstance(encoder_result, dict):
            self._render_transcode_encoder_options(
                encoder_result.get("items", ()),
                str(encoder_result.get("error") or ""),
            )
        else:
            self._render_transcode_encoder_options(())
        for component in ("yt-dlp", "yt-dlp-ejs", "Deno", "FFmpeg", "FFprobe"):
            item = detected.get(component, ("检测失败", "", ""))
            self._local_core_details[component] = tuple(str(value or "") for value in item)
        self._render_runtime_component_statuses()

    def request_shutdown(self) -> None:
        self.local_core_versions.request_shutdown()

    @property
    def local_core_version_check_running(self) -> bool:
        return self.local_core_versions.running

    def update_performance_controls(self, *_args) -> None:
        smart = str(self.performance_mode.currentData()) == "smart"
        if smart:
            if self.max_concurrent.isEnabled():
                self._manual_performance_values = (
                    self.max_concurrent.value(),
                    self.fragment_concurrent.value(),
                    self.request_delay.value(),
                )
            tasks, fragments, delay = smart_download_performance()
            self.max_concurrent.setValue(tasks)
            self.fragment_concurrent.setValue(fragments)
            self.request_delay.setValue(delay)
        elif not self.max_concurrent.isEnabled():
            tasks, fragments, delay = self._manual_performance_values
            self.max_concurrent.setValue(tasks)
            self.fragment_concurrent.setValue(fragments)
            self.request_delay.setValue(delay)
        for control in (self.max_concurrent, self.fragment_concurrent, self.request_delay):
            control.setEnabled(not smart)

        tasks, fragments, delay = self.effective_download_performance_values()
        processors = max(1, int(os.cpu_count() or 1))
        if smart:
            self.performance_summary.setText(ui_format(
                '{processors} logical processors: {tasks} tasks · {fragments} fragments · {delay}s delay',
                processors=processors,
                tasks=tasks,
                fragments=fragments,
                delay=f"{delay:g}",
            ))
        else:
            self.performance_summary.setText(ui_text(
                'Manual values apply directly to new tasks. Reduce concurrency or add delay if a site throttles requests.',
            ))

    def manual_download_performance_values(self) -> tuple[int, int, float]:
        if str(self.performance_mode.currentData()) == "manual":
            return (
                self.max_concurrent.value(),
                self.fragment_concurrent.value(),
                self.request_delay.value(),
            )
        return self._manual_performance_values

    def _render_transcode_encoder_options(
        self,
        encoders: object,
        error: str = "",
    ) -> None:
        selected = normalize_transcode_encoder(self.transcode_encoder.currentData())
        available = tuple(
            encoder
            for encoder in (normalize_transcode_encoder(item) for item in (encoders or ()))
            if encoder != "original" and encoder in TRANSCODE_ENCODER_NATIVE_LABELS
        )
        self.transcode_encoder.blockSignals(True)
        self.transcode_encoder.clear()
        self.transcode_encoder.addItem(transcode_encoder_label("original"), "original")
        for encoder in dict.fromkeys(available):
            self.transcode_encoder.addItem(
                transcode_encoder_label(encoder),
                encoder,
            )
        selected_index = self.transcode_encoder.findData(selected)
        self.transcode_encoder.setCurrentIndex(max(0, selected_index))
        self.transcode_encoder.blockSignals(False)
        self.transcode_encoder.setEnabled(True)

        if error:
            self.transcode_encoder_status.setText(ui_format(
                'Encoder detection failed: {error}',
                error=runtime_text(error),
            ))
        elif selected != "original" and selected_index < 0:
            self.transcode_encoder_status.setText(ui_format(
                'The previously selected {encoder} is unavailable; Keep original is now selected.',
                encoder=selected,
            ))
        elif available:
            self.transcode_encoder_status.setText(ui_format(
                'Found {count} encoders in the current FFmpeg; GPU entries are explicitly marked.',
                count=len(available),
            ))
        else:
            self.transcode_encoder_status.setText(ui_text(
                'No common transcoding encoder was found in the current FFmpeg; only Keep original is available.',
            ))

    def effective_download_performance_values(self) -> tuple[int, int, float]:
        if str(self.performance_mode.currentData()) == "smart":
            return smart_download_performance()
        return self.manual_download_performance_values()

    def update_cover_focus_controls(self, *_args) -> None:
        enabled = str(self.cover_fit.currentData()) == CoverFitMode.CROP.value
        self.cover_focus_x.setEnabled(enabled)
        self.cover_focus_y.setEnabled(enabled)

    def choose_download_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self,
            ui_text('Choose Download Folder'),
            str(resolve_portable_path(self.download_dir.text())),
        )
        if path:
            self.download_dir.setText(path)

    def choose_processing_temp_dir(self) -> None:
        initial = (
            str(resolve_portable_path(self.processing_temp_dir.text()))
            if self.processing_temp_dir.text().strip()
            else str(resolve_portable_path(self.download_dir.text()))
        )
        path = QFileDialog.getExistingDirectory(
            self,
            ui_text('Choose Processing Temporary Folder'),
            initial,
        )
        if path:
            self.processing_temp_dir.setText(path)

    def choose_download_cookie_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            ui_text('Choose Download Cookie File'),
            str(resolve_portable_path(self.download_cookie_file.text()))
            if self.download_cookie_file.text().strip()
            else str(resolve_portable_path("data/browser")),
            ui_text('Netscape Cookie Files (*.txt *.cookies);;All Files (*)'),
        )
        if path:
            self.download_cookie_file.setText(path)
