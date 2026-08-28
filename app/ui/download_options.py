from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable, Mapping

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, Signal, QTimer, QUrl
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from app.core.download_options import DownloadOptions, SPONSORBLOCK_CATEGORIES
from app.core.download_service import DownloadTask
from app.ui.i18n import format_text as ui_format, text as ui_text


class AdvancedDownloadOptionsDialog(QDialog):
    """Validated global/per-task controls; no arbitrary yt-dlp arguments."""

    def __init__(self, options: Mapping[str, Any] | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle(ui_text('Advanced Download Options'))
        self.resize(660, 720)
        current = DownloadOptions.from_mapping(options)
        self._audio_track = current.audio_track

        root, scroll, content, form = self._build_form_shell()

        self._build_media_preferences(form, content)

        self._build_collection_options(form)
        self._build_collection_filters(form)
        self._build_live_options(form)
        self._build_clip_options(form)
        self._build_sidecar_options(form)
        self._build_embedding_options(form)
        self._build_sponsorblock_options(form)

        scroll.setWidget(content)
        root.addWidget(scroll, 1)
        self._add_dialog_buttons(root)
        self._restore_options(current)
        self._connect_control_state()

    def _build_form_shell(
        self,
    ) -> tuple[QVBoxLayout, QScrollArea, QWidget, QFormLayout]:
        root = QVBoxLayout(self)
        note = QLabel(ui_text(
            'These options are stored with each task. They work with both the bundled and standalone yt-dlp cores.',
        ))
        note.setWordWrap(True)
        root.addWidget(note)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        form = QFormLayout(content)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        return root, scroll, content, form

    def _add_dialog_buttons(self, root: QVBoxLayout) -> None:
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _build_media_preferences(self, form: QFormLayout, content: QWidget) -> None:
        self.content_mode = QComboBox(content)
        self.content_mode.addItem(ui_text('Manual'), 'manual')
        self.content_mode.addItem(ui_text('Video'), 'video')
        self.content_mode.addItem(ui_text('Audio'), 'audio')
        self.audio_format = QComboBox()
        for label, value in (
            (ui_text('Original best audio'), 'best'),
            ('AAC', 'aac'),
            ('ALAC', 'alac'),
            ('FLAC', 'flac'),
            ('M4A', 'm4a'),
            ('MP3', 'mp3'),
            ('Opus', 'opus'),
            ('Vorbis', 'vorbis'),
            ('WAV', 'wav'),
        ):
            self.audio_format.addItem(label, value)
        self.container = QComboBox(content)
        for label, value in (
            (ui_text('Automatic'), 'auto'),
            ('MP4', 'mp4'),
            ('MKV', 'mkv'),
        ):
            self.container.addItem(label, value)
        self.video_fps = QComboBox(content)
        self.video_fps.addItem(ui_text('Highest available frame rate'), 'best')
        for fps in ('240', '120', '60', '50', '30', '25', '24'):
            self.video_fps.addItem(f'{fps} FPS', fps)
        self.source_video_codec = QComboBox(content)
        for label, value in (
            (ui_text('Automatic codec'), 'auto'),
            ('H.264 / AVC', 'h264'),
            ('H.265 / HEVC', 'h265'),
            ('AV1', 'av1'),
            ('VP9', 'vp9'),
        ):
            self.source_video_codec.addItem(label, value)
        self.vr_mode = QComboBox(content)
        for label, value in (
            (ui_text('Any'), 'any'),
            ('2D / 360°', '2d360'),
            ('3D / 180°', '3d180'),
            ('3D / 360°', '3d360'),
            (ui_text('No VR'), 'none'),
        ):
            self.vr_mode.addItem(label, value)
        self.compatibility_target = QComboBox(content)
        for label, value in (
            (ui_text('Automatic compatibility'), 'auto'),
            ('Windows', 'windows'),
            ('macOS', 'macos'),
            ('Linux', 'linux'),
            ('iOS', 'ios'),
            ('Android', 'android'),
        ):
            self.compatibility_target.addItem(label, value)
        preference_tip = ui_text(
            'These are preferences, not strict filters. Resolution remains the first priority, and yt-dlp falls back when a site does not provide the preferred frame rate or source codec.',
        )
        self.video_fps.setToolTip(preference_tip)
        self.source_video_codec.setToolTip(preference_tip)
        self.compatibility_target.setToolTip(ui_text(
            'Select a playback target. Automatic keeps yt-dlp defaults; device presets choose a broadly compatible container and source codec unless you explicitly choose them.',
        ))
        # Primary content/container choices already exist on Settings and the
        # new-task bar. Retain the controls only for option synchronization.
        self.content_mode.hide()
        self.container.hide()
        form.addRow(ui_text('Audio Format'), self.audio_format)
        form.addRow(ui_text('Frame Rate Preference'), self.video_fps)
        form.addRow(ui_text('Source Video Codec Preference'), self.source_video_codec)
        form.addRow(ui_text('VR'), self.vr_mode)
        form.addRow(ui_text('Playback Compatibility'), self.compatibility_target)

    def _build_collection_options(self, form: QFormLayout) -> None:
        self.collection_mode = QComboBox()
        self.collection_mode.addItem(ui_text('Parse and choose (recommended)'), 'select')
        self.collection_mode.addItem(ui_text('Download current item only'), 'single')
        self.collection_mode.addItem(ui_text('Download all items directly'), 'all')
        self.collection_order = QComboBox()
        self.collection_order.addItem(ui_text('Original order'), 'original')
        self.collection_order.addItem(ui_text('Reverse order'), 'reverse')
        self.collection_order.addItem(ui_text('Random order'), 'random')
        self.first_n = QSpinBox()
        self.first_n.setRange(0, 1_000_000)
        self.first_n.setSpecialValueText(ui_text('No limit'))
        self.playlist_items = QLineEdit()
        self.playlist_items.setPlaceholderText(ui_text('For example: 1-10,15,20:30'))
        form.addRow(ui_text('Collection Handling'), self.collection_mode)
        form.addRow(ui_text('Collection Order'), self.collection_order)
        form.addRow(ui_text('First N Items'), self.first_n)
        form.addRow(ui_text('Item Range'), self.playlist_items)

    def _build_collection_filters(self, form: QFormLayout) -> None:
        self.date_after = QLineEdit()
        self.date_after.setPlaceholderText(ui_text('YYYY-MM-DD'))
        self.date_before = QLineEdit()
        self.date_before.setPlaceholderText(ui_text('YYYY-MM-DD'))
        self.duration_min = QSpinBox()
        self.duration_min.setRange(0, 31_536_000)
        self.duration_min.setSuffix(ui_text(' s'))
        self.duration_max = QSpinBox()
        self.duration_max.setRange(0, 31_536_000)
        self.duration_max.setSuffix(ui_text(' s'))
        self.live_filter = QComboBox()
        self.live_filter.addItem(ui_text('Regular videos only'), 'videos')
        self.live_filter.addItem(ui_text('Include live and upcoming'), 'all')
        self.live_filter.addItem(ui_text('Live and upcoming only'), 'live')
        form.addRow(ui_text('Published After'), self.date_after)
        form.addRow(ui_text('Published Before'), self.date_before)
        form.addRow(ui_text('Minimum Duration'), self.duration_min)
        form.addRow(ui_text('Maximum Duration'), self.duration_max)
        form.addRow(ui_text('Live Filter'), self.live_filter)

    def _build_live_options(self, form: QFormLayout) -> None:
        self.live_from_start = QCheckBox(
            ui_text('Try to download live streams from the beginning'),
        )
        self.wait_for_live = QCheckBox(ui_text('Wait for scheduled live streams'))
        wait_row = QWidget()
        wait_layout = QHBoxLayout(wait_row)
        wait_layout.setContentsMargins(0, 0, 0, 0)
        self.wait_min = QSpinBox()
        self.wait_min.setRange(1, 86400)
        self.wait_min.setSuffix(ui_text(' s'))
        self.wait_max = QSpinBox()
        self.wait_max.setRange(1, 86400)
        self.wait_max.setSuffix(ui_text(' s'))
        wait_layout.addWidget(self.wait_min)
        wait_layout.addWidget(QLabel(ui_text('to')))
        wait_layout.addWidget(self.wait_max)
        form.addRow('', self.live_from_start)
        form.addRow('', self.wait_for_live)
        form.addRow(ui_text('Live Retry Interval'), wait_row)

    def _build_clip_options(self, form: QFormLayout) -> None:
        self.section_start = QLineEdit()
        self.section_start.setPlaceholderText(
            ui_text('Start time, for example 00:01:30'),
        )
        self.section_end = QLineEdit()
        self.section_end.setPlaceholderText(
            ui_text('End time, for example 00:05:00'),
        )
        self.split_chapters = QCheckBox(ui_text('Split files by native chapters'))
        form.addRow(ui_text('Clip Start'), self.section_start)
        form.addRow(ui_text('Clip End'), self.section_end)
        form.addRow('', self.split_chapters)

    def _build_sidecar_options(self, form: QFormLayout) -> None:
        self.subtitle_format = QComboBox()
        for value in ('best', 'vtt', 'srt', 'ass', 'lrc'):
            self.subtitle_format.addItem(value, value)
        self.embed_subtitles = QCheckBox(
            ui_text('Embed subtitles when the container supports them'),
        )
        self.write_thumbnail = QCheckBox(ui_text('Save thumbnail'))
        self.write_description = QCheckBox(ui_text('Save description'))
        self.write_comments = QCheckBox(
            ui_text('Save comments (may significantly slow parsing)'),
        )
        self.write_info_json = QCheckBox(ui_text('Save info.json'))
        form.addRow(ui_text('Subtitle Format'), self.subtitle_format)
        for control in (
            self.embed_subtitles,
            self.write_thumbnail,
            self.write_description,
            self.write_comments,
            self.write_info_json,
        ):
            form.addRow('', control)

    def _build_embedding_options(self, form: QFormLayout) -> None:
        self.embed_metadata = QCheckBox(ui_text('Embed metadata'))
        self.embed_chapters = QCheckBox(ui_text('Embed chapters'))
        self.embed_thumbnail = QCheckBox(ui_text('Embed thumbnail'))
        for control in (
            self.embed_metadata,
            self.embed_chapters,
            self.embed_thumbnail,
        ):
            form.addRow('', control)

    def _build_sponsorblock_options(self, form: QFormLayout) -> None:
        self.sponsorblock_mode = QComboBox()
        self.sponsorblock_mode.addItem(ui_text('Off'), 'off')
        self.sponsorblock_mode.addItem(
            ui_text('Mark SponsorBlock segments as chapters'),
            'mark',
        )
        self.sponsorblock_mode.addItem(
            ui_text('Remove SponsorBlock segments'),
            'remove',
        )
        self.sponsor_categories: dict[str, QCheckBox] = {}
        categories = QWidget()
        categories_layout = QVBoxLayout(categories)
        categories_layout.setContentsMargins(0, 0, 0, 0)
        for category in sorted(SPONSORBLOCK_CATEGORIES):
            checkbox = QCheckBox(category)
            categories_layout.addWidget(checkbox)
            self.sponsor_categories[category] = checkbox
        self.rate_limit = QLineEdit()
        self.rate_limit.setPlaceholderText(
            ui_text('For example: 10M or 500K; blank means unlimited'),
        )
        form.addRow(ui_text('SponsorBlock'), self.sponsorblock_mode)
        form.addRow(ui_text('SponsorBlock Categories'), categories)
        form.addRow(ui_text('Maximum Download Speed'), self.rate_limit)

    def _restore_options(self, current: DownloadOptions) -> None:
        for combo, value in (
            (self.content_mode, current.content_mode),
            (self.audio_format, current.audio_format),
            (self.container, current.container),
            (self.video_fps, current.video_fps),
            (self.source_video_codec, current.source_video_codec),
            (self.vr_mode, current.vr_mode),
            (self.compatibility_target, current.compatibility_target),
            (self.collection_mode, current.collection_mode),
            (self.collection_order, current.collection_order),
            (self.live_filter, current.live_filter),
            (self.subtitle_format, current.subtitle_format),
            (self.sponsorblock_mode, current.sponsorblock_mode),
        ):
            combo.setCurrentIndex(max(0, combo.findData(value)))
        self.first_n.setValue(current.first_n)
        self.playlist_items.setText(current.playlist_items)
        self.date_after.setText(current.date_after)
        self.date_before.setText(current.date_before)
        self.duration_min.setValue(current.duration_min)
        self.duration_max.setValue(current.duration_max)
        self.live_from_start.setChecked(current.live_from_start)
        self.wait_for_live.setChecked(current.wait_for_live)
        self.wait_min.setValue(current.wait_min)
        self.wait_max.setValue(current.wait_max)
        self.section_start.setText(current.section_start)
        self.section_end.setText(current.section_end)
        self.split_chapters.setChecked(current.split_chapters)
        self.embed_subtitles.setChecked(current.embed_subtitles)
        self.write_thumbnail.setChecked(current.write_thumbnail)
        self.write_description.setChecked(current.write_description)
        self.write_comments.setChecked(current.write_comments)
        self.write_info_json.setChecked(current.write_info_json)
        self.embed_metadata.setChecked(current.embed_metadata)
        self.embed_chapters.setChecked(current.embed_chapters)
        self.embed_thumbnail.setChecked(current.embed_thumbnail)
        for category in current.sponsorblock_categories:
            checkbox = self.sponsor_categories.get(category)
            if checkbox is not None:
                checkbox.setChecked(True)
        self.rate_limit.setText(current.rate_limit)

    def _connect_control_state(self) -> None:
        self.content_mode.currentIndexChanged.connect(self._sync_controls)
        self.container.currentIndexChanged.connect(self._sync_controls)
        self.wait_for_live.toggled.connect(self._sync_controls)
        self.sponsorblock_mode.currentIndexChanged.connect(self._sync_controls)
        self._sync_controls()

    def _sync_controls(self, *_args) -> None:
        audio_only = self.content_mode.currentData() == 'audio'
        self.audio_format.setEnabled(audio_only)
        self.video_fps.setEnabled(not audio_only)
        self.source_video_codec.setEnabled(not audio_only)
        self.vr_mode.setEnabled(not audio_only)
        self.compatibility_target.setEnabled(not audio_only)
        wait = self.wait_for_live.isChecked()
        self.wait_min.setEnabled(wait)
        self.wait_max.setEnabled(wait)
        sponsor = self.sponsorblock_mode.currentData() != 'off'
        for checkbox in self.sponsor_categories.values():
            checkbox.setEnabled(sponsor)
        container = self.container.currentData()
        self.embed_thumbnail.setEnabled(container in {'auto', 'mp4', 'mkv'})
        self.embed_subtitles.setEnabled(container in {'auto', 'mp4', 'mkv'})
        self.embed_thumbnail.setToolTip('')

    def options(self) -> dict[str, Any]:
        return DownloadOptions.from_mapping({
            'content_mode': self.content_mode.currentData(),
            'audio_format': self.audio_format.currentData(),
            'audio_track': self._audio_track,
            'container': self.container.currentData(),
            'video_fps': self.video_fps.currentData(),
            'source_video_codec': self.source_video_codec.currentData(),
            'vr_mode': self.vr_mode.currentData(),
            'compatibility_target': self.compatibility_target.currentData(),
            'collection_mode': self.collection_mode.currentData(),
            'collection_order': self.collection_order.currentData(),
            'first_n': self.first_n.value(),
            'playlist_items': self.playlist_items.text(),
            'date_after': self.date_after.text(),
            'date_before': self.date_before.text(),
            'duration_min': self.duration_min.value(),
            'duration_max': self.duration_max.value(),
            'live_filter': self.live_filter.currentData(),
            'live_from_start': self.live_from_start.isChecked(),
            'wait_for_live': self.wait_for_live.isChecked(),
            'wait_min': self.wait_min.value(),
            'wait_max': self.wait_max.value(),
            'section_start': self.section_start.text(),
            'section_end': self.section_end.text(),
            'split_chapters': self.split_chapters.isChecked(),
            'subtitle_format': self.subtitle_format.currentData(),
            'embed_subtitles': self.embed_subtitles.isChecked(),
            'write_thumbnail': self.write_thumbnail.isChecked(),
            'write_description': self.write_description.isChecked(),
            'write_comments': self.write_comments.isChecked(),
            'write_info_json': self.write_info_json.isChecked(),
            'embed_metadata': self.embed_metadata.isChecked(),
            'embed_chapters': self.embed_chapters.isChecked(),
            'embed_thumbnail': self.embed_thumbnail.isChecked(),
            'sponsorblock_mode': self.sponsorblock_mode.currentData(),
            'sponsorblock_categories': [key for key, box in self.sponsor_categories.items() if box.isChecked()],
            'rate_limit': self.rate_limit.text(),
        }).to_dict()


class CollectionEntryModel(QAbstractTableModel):
    selection_changed = Signal()
    HEADERS = (
        'Select', '#', 'Thumbnail', 'Title', 'Author', 'Duration', 'Published', 'Live Status', 'Availability',
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.entries: list[dict[str, Any]] = []
        self._total_count = 0
        self._source_total_count = 0
        self._page_size = 160
        self._page_loader: Callable[[int, int], list[dict[str, Any]]] | None = None
        self._view_loader: Callable[[int, int, Mapping[str, Any]], list[dict[str, Any]]] | None = None
        self._view_counter: Callable[[Mapping[str, Any]], int] | None = None
        self._view_options: dict[str, Any] = {
            'query': '', 'state': 'all', 'date_after': '', 'date_before': '',
            'duration_min': 0, 'duration_max': 0,
            'sort_column': 'collection_index', 'sort_descending': False,
        }
        self._selection_updater: Callable[[int, bool], None] | None = None
        self._selection_setter: Callable[[str], None] | None = None
        self._selected_loader: Callable[[], list[dict[str, Any]]] | None = None
        self._selected_counter: Callable[[], int] | None = None
        self._cache: dict[int, dict[str, Any]] = {}
        self._query_generation = 0
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='collection-view')
        self._count_future: tuple[int, Future[int]] | None = None
        self._page_futures: dict[int, tuple[int, Future[list[dict[str, Any]]]]] = {}
        self._future_timer = QTimer(self)
        self._future_timer.setInterval(30)
        self._future_timer.timeout.connect(self._poll_futures)
        self._network = QNetworkAccessManager(self)
        self._thumbnail_cache: OrderedDict[str, QIcon] = OrderedDict()
        self._thumbnail_pending: set[str] = set()
        self._thumbnail_queue: list[tuple[int, str]] = []
        self._thumbnail_replies: dict[str, Any] = {}
        self._thumbnail_active = 0
        self._thumbnail_cache_limit = 200
        self.destroyed.connect(self._shutdown_workers)

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else (self._total_count if self._page_loader else len(self.entries))

    def set_paged_source(
        self,
        total_count: int,
        loader: Callable[[int, int], list[dict[str, Any]]],
        *,
        selection_updater: Callable[[int, bool], None],
        selection_setter: Callable[[str], None],
        selected_loader: Callable[[], list[dict[str, Any]]],
        selected_counter: Callable[[], int],
        view_loader: Callable[[int, int, Mapping[str, Any]], list[dict[str, Any]]] | None = None,
        view_counter: Callable[[Mapping[str, Any]], int] | None = None,
    ) -> None:
        self._invalidate_async_queries()
        self._clear_thumbnail_requests()
        self.beginResetModel()
        self.entries.clear()
        self._cache.clear()
        self._total_count = max(0, int(total_count))
        self._source_total_count = self._total_count
        self._page_loader = loader
        self._view_loader = view_loader
        self._view_counter = view_counter
        self._view_options = {
            'query': '', 'state': 'all', 'date_after': '', 'date_before': '',
            'duration_min': 0, 'duration_max': 0,
            'sort_column': 'collection_index', 'sort_descending': False,
        }
        self._selection_updater = selection_updater
        self._selection_setter = selection_setter
        self._selected_loader = selected_loader
        self._selected_counter = selected_counter
        self.endResetModel()

    def clear_source(self) -> None:
        self._invalidate_async_queries()
        self._clear_thumbnail_requests()
        self.beginResetModel()
        self.entries.clear()
        self._cache.clear()
        self._total_count = 0
        self._source_total_count = 0
        self._page_loader = None
        self._view_loader = None
        self._view_counter = None
        self._selection_updater = None
        self._selection_setter = None
        self._selected_loader = None
        self._selected_counter = None
        self.endResetModel()

    def entry_at(self, row: int) -> dict[str, Any]:
        if self._page_loader is None:
            return self.entries[row]
        cached = self._cache.get(row)
        if cached is not None:
            return cached
        offset = max(0, (row // self._page_size) * self._page_size)
        if offset not in self._page_futures:
            generation = self._query_generation
            options = dict(self._view_options)
            loader = self._view_loader
            page_loader = self._page_loader

            def load_page() -> list[dict[str, Any]]:
                if loader is not None:
                    return loader(offset, self._page_size, options)
                return page_loader(offset, self._page_size) if page_loader is not None else []

            self._page_futures[offset] = (generation, self._executor.submit(load_page))
            self._future_timer.start()
        return {}

    @property
    def is_paged(self) -> bool:
        return self._page_loader is not None

    def source_count(self) -> int:
        return self._source_total_count if self.is_paged else len(self.entries)

    def set_paged_view(self, **options: Any) -> None:
        if self._view_counter is None:
            return
        self._view_options.update(options)
        self._invalidate_async_queries()
        self.beginResetModel()
        self._cache.clear()
        self._total_count = 0
        self.endResetModel()
        generation = self._query_generation
        counter = self._view_counter
        view_options = dict(self._view_options)
        self._count_future = (
            generation,
            self._executor.submit(lambda: max(0, int(counter(view_options)))),
        )
        self._future_timer.start()

    def _invalidate_async_queries(self) -> None:
        self._query_generation += 1
        if self._count_future is not None:
            self._count_future[1].cancel()
        for _generation, future in self._page_futures.values():
            future.cancel()
        self._count_future = None
        self._page_futures.clear()

    def _poll_futures(self) -> None:
        count_job = self._count_future
        if count_job is not None and count_job[1].done():
            generation, future = count_job
            self._count_future = None
            if generation == self._query_generation and not future.cancelled():
                try:
                    total = max(0, int(future.result()))
                except Exception:
                    total = 0
                self.beginResetModel()
                self._total_count = total
                self._cache.clear()
                self.endResetModel()

        completed_offsets = [
            offset for offset, (_generation, future) in self._page_futures.items()
            if future.done()
        ]
        for offset in completed_offsets:
            generation, future = self._page_futures.pop(offset)
            if generation != self._query_generation or future.cancelled():
                continue
            try:
                loaded = future.result()
            except Exception:
                loaded = []
            for position, entry in enumerate(loaded, start=offset):
                if position >= self._total_count:
                    break
                self._cache[position] = dict(entry)
            if loaded and self._total_count:
                first = self.index(offset, 0)
                last_row = min(offset + len(loaded) - 1, self._total_count - 1)
                self.dataChanged.emit(first, self.index(last_row, self.columnCount() - 1))

        if self._count_future is None and not self._page_futures:
            self._future_timer.stop()

    def _shutdown_workers(self, *_args) -> None:
        self._invalidate_async_queries()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def paged_view_options(self) -> dict[str, Any]:
        return dict(self._view_options)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal and 0 <= section < len(self.HEADERS):
            return ui_text(self.HEADERS[section])
        return None

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.NoItemFlags
        entry = self.entry_at(index.row())
        flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        if index.column() == 0 and entry.get('downloadable'):
            flags |= Qt.ItemIsUserCheckable
        return flags

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        entry = self.entry_at(index.row())
        if index.column() == 0 and role == Qt.CheckStateRole:
            return Qt.Checked if entry.get('selected') else Qt.Unchecked
        if index.column() == 2 and role == Qt.DecorationRole:
            thumbnail = str(entry.get('thumbnail') or '')
            if thumbnail:
                icon = self._thumbnail_cache.get(thumbnail)
                if icon is not None:
                    self._thumbnail_cache.move_to_end(thumbnail)
                    return icon
                self._queue_thumbnail(index.row(), thumbnail)
            return None
        if role == Qt.ToolTipRole and entry.get('disabled_reason'):
            return ui_format('Unavailable: {reason}', reason=entry['disabled_reason'])
        if role != Qt.DisplayRole:
            return None
        duration = float(entry.get('duration') or 0)
        values = (
            '', entry.get('index') or index.row() + 1, '',
            ('📁 ' if entry.get('entry_kind') == 'collection' else '') + str(entry.get('title') or ''),
            entry.get('uploader') or '',
            f"{int(duration) // 60:02d}:{int(duration) % 60:02d}" if duration else '',
            entry.get('upload_date') or '',
            entry.get('live_status') or '',
            ui_text('Completed') if entry.get('completed') else (entry.get('availability') or ui_text('Available')),
        )
        return values[index.column()]

    def _queue_thumbnail(self, row: int, url: str) -> None:
        if url in self._thumbnail_cache or url in self._thumbnail_pending:
            return
        self._thumbnail_pending.add(url)
        self._thumbnail_queue.append((row, url))
        self._start_thumbnail_requests()

    def _start_thumbnail_requests(self) -> None:
        while self._thumbnail_queue and self._thumbnail_active < 4:
            row, url = self._thumbnail_queue.pop(0)
            self._thumbnail_active += 1
            request = QNetworkRequest(QUrl(url))
            request.setRawHeader(b'User-Agent', b'Mozilla/5.0')
            reply = self._network.get(request)
            self._thumbnail_replies[url] = reply

            def finished(reply=reply, row=row, url=url) -> None:
                self._thumbnail_active = max(0, self._thumbnail_active - 1)
                self._thumbnail_pending.discard(url)
                self._thumbnail_replies.pop(url, None)
                data = bytes(reply.readAll())
                reply.deleteLater()
                if len(data) <= 5 * 1024 * 1024:
                    pixmap = QPixmap()
                    if pixmap.loadFromData(data):
                        pixmap = pixmap.scaled(96, 54, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                        self._thumbnail_cache[url] = QIcon(pixmap)
                        self._thumbnail_cache.move_to_end(url)
                        while len(self._thumbnail_cache) > self._thumbnail_cache_limit:
                            self._thumbnail_cache.popitem(last=False)
                        candidates = (
                            list(self._cache.items())
                            if self._page_loader is not None
                            else list(enumerate(self.entries))
                        )
                        for current_row, entry in candidates:
                            if str(entry.get('thumbnail') or '') == url:
                                cell = self.index(current_row, 2)
                                self.dataChanged.emit(cell, cell, [Qt.DecorationRole])
                self._start_thumbnail_requests()

            reply.finished.connect(finished)

    def _clear_thumbnail_requests(self) -> None:
        self._thumbnail_queue.clear()
        self._thumbnail_pending.clear()
        for reply in tuple(self._thumbnail_replies.values()):
            try:
                reply.abort()
            except RuntimeError:
                pass
        self._thumbnail_replies.clear()
        self._thumbnail_active = 0

    def setData(self, index: QModelIndex, value, role=Qt.EditRole) -> bool:
        if not index.isValid() or index.column() != 0 or role != Qt.CheckStateRole:
            return False
        entry = self.entry_at(index.row())
        if not entry.get('downloadable'):
            return False
        entry['selected'] = value == Qt.Checked
        if self._selection_updater is not None:
            self._selection_updater(
                int(entry.get('index') or index.row() + 1),
                bool(entry['selected']),
            )
        self.dataChanged.emit(index, index, [Qt.CheckStateRole])
        self.selection_changed.emit()
        return True

    def append(self, entries: list[Mapping[str, Any]]) -> None:
        if not entries:
            return
        if self._page_loader is not None:
            self._source_total_count += len(entries)
            non_default_view = any((
                self._view_options.get('query'),
                self._view_options.get('state') not in {'', 'all'},
                self._view_options.get('date_after'),
                self._view_options.get('date_before'),
                int(self._view_options.get('duration_min') or 0),
                int(self._view_options.get('duration_max') or 0),
                self._view_options.get('sort_column') != 'collection_index',
                bool(self._view_options.get('sort_descending')),
            ))
            if non_default_view and self._view_counter is not None:
                self.set_paged_view()
                return
            first = self._total_count
            self.beginInsertRows(QModelIndex(), first, first + len(entries) - 1)
            self._total_count += len(entries)
            for offset, entry in enumerate(entries, start=first):
                self._cache[offset] = dict(entry)
            self.endInsertRows()
            return
        first = len(self.entries)
        self.beginInsertRows(QModelIndex(), first, first + len(entries) - 1)
        self.entries.extend(dict(entry) for entry in entries)
        self.endInsertRows()

    def set_selection(self, mode: str) -> None:
        if self._selection_setter is not None:
            self._selection_setter(mode)
            for entry in self._cache.values():
                if not entry.get('downloadable'):
                    continue
                if mode == 'all':
                    entry['selected'] = True
                elif mode == 'none':
                    entry['selected'] = False
                else:
                    entry['selected'] = not bool(entry.get('selected'))
            if self._total_count:
                self.dataChanged.emit(self.index(0, 0), self.index(self._total_count - 1, 0), [Qt.CheckStateRole])
            self.selection_changed.emit()
            return
        for entry in self.entries:
            if not entry.get('downloadable'):
                continue
            if mode == 'all':
                entry['selected'] = True
            elif mode == 'none':
                entry['selected'] = False
            else:
                entry['selected'] = not bool(entry.get('selected'))
        if self.entries:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.entries) - 1, 0), [Qt.CheckStateRole])
        self.selection_changed.emit()

    def selected_entries(self) -> list[dict[str, Any]]:
        if self._selected_loader is not None:
            return self._selected_loader()
        return [dict(entry) for entry in self.entries if entry.get('selected') and entry.get('downloadable')]

    def selected_count(self) -> int:
        if self._selected_counter is not None:
            return self._selected_counter()
        return len(self.selected_entries())


class CollectionFilterProxy(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.query = ''
        self.state = 'all'
        self.date_after = ''
        self.date_before = ''
        self.duration_min = 0
        self.duration_max = 0

    def set_query(self, value: str) -> None:
        query = str(value or '').casefold()
        if query == self.query:
            return
        self.beginFilterChange()
        self.query = query
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def set_state(self, value: str) -> None:
        state = str(value or 'all')
        if state == self.state:
            return
        self.beginFilterChange()
        self.state = state
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def set_limits(self, date_after: str, date_before: str, duration_min: int, duration_max: int) -> None:
        limits = (
            str(date_after or '').replace('-', '')[:8],
            str(date_before or '').replace('-', '')[:8],
            max(0, int(duration_min or 0)),
            max(0, int(duration_max or 0)),
        )
        if limits == (
            self.date_after,
            self.date_before,
            self.duration_min,
            self.duration_max,
        ):
            return
        self.beginFilterChange()
        (
            self.date_after,
            self.date_before,
            self.duration_min,
            self.duration_max,
        ) = limits
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        if not isinstance(model, CollectionEntryModel):
            return True
        if model.is_paged:
            return True
        entry = model.entry_at(source_row)
        if self.query and self.query not in ' '.join(str(entry.get(key) or '') for key in ('title', 'uploader', 'url')).casefold():
            return False
        if self.state == 'available' and not entry.get('downloadable'):
            return False
        if self.state == 'completed' and not entry.get('completed'):
            return False
        if self.state == 'unavailable' and entry.get('downloadable'):
            return False
        if self.state == 'live' and not entry.get('live_status'):
            return False
        upload_date = str(entry.get('upload_date') or '').replace('-', '')[:8]
        if self.date_after and upload_date and upload_date < self.date_after:
            return False
        if self.date_before and upload_date and upload_date > self.date_before:
            return False
        try:
            duration = int(float(entry.get('duration') or 0))
        except (TypeError, ValueError):
            duration = 0
        if self.duration_min and duration and duration < self.duration_min:
            return False
        if self.duration_max and duration and duration > self.duration_max:
            return False
        return True


class CollectionSelectionPage(QWidget):
    download_requested = Signal(object)
    cancel_requested = Signal()
    nested_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.model = CollectionEntryModel(self)
        self.model.selection_changed.connect(self._update_summary)
        self._storage_preview_provider: Callable[[], str] | None = None
        self.proxy = CollectionFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.parsing = True
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(300)
        self._filter_timer.timeout.connect(self._apply_filters)
        root = QVBoxLayout(self)
        header = QHBoxLayout()
        self.title = QLabel(ui_text('Parsing Collection'))
        self.title.setObjectName('pageTitle')
        self.summary = QLabel()
        header.addWidget(self.title)
        header.addWidget(self.summary)
        header.addStretch(1)
        root.addLayout(header)
        filters = QGridLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(ui_text('Search title, author or URL'))
        self.search.textChanged.connect(self._filters_changed)
        self.state = QComboBox()
        for label, value in (
            (ui_text('All'), 'all'), (ui_text('Available'), 'available'),
            (ui_text('Completed'), 'completed'), (ui_text('Unavailable'), 'unavailable'),
            (ui_text('Live'), 'live'),
        ):
            self.state.addItem(label, value)
        self.state.currentIndexChanged.connect(self._filters_changed)
        filters.addWidget(self.search, 0, 0)
        filters.addWidget(self.state, 0, 1)
        filters.setColumnStretch(0, 1)
        root.addLayout(filters)
        limits = QGridLayout()
        self.date_after = QLineEdit()
        self.date_after.setPlaceholderText(ui_text('Published after YYYY-MM-DD'))
        self.date_before = QLineEdit()
        self.date_before.setPlaceholderText(ui_text('Published before YYYY-MM-DD'))
        self.duration_min = QSpinBox()
        self.duration_min.setRange(0, 31_536_000)
        self.duration_min.setPrefix(ui_text('Min ') )
        self.duration_min.setSuffix(ui_text(' s'))
        self.duration_max = QSpinBox()
        self.duration_max.setRange(0, 31_536_000)
        self.duration_max.setPrefix(ui_text('Max ') )
        self.duration_max.setSuffix(ui_text(' s'))
        for column, control in enumerate((self.date_after, self.date_before)):
            control.textChanged.connect(self._update_limits)
            limits.addWidget(control, 0, column)
        for column, control in enumerate((self.duration_min, self.duration_max), start=2):
            control.valueChanged.connect(self._update_limits)
            limits.addWidget(control, 0, column)
        limits.setColumnStretch(0, 1)
        limits.setColumnStretch(1, 1)
        root.addLayout(limits)
        self.storage_preview = QLabel()
        self.storage_preview.setObjectName('mutedText')
        self.storage_preview.setWordWrap(True)
        root.addWidget(self.storage_preview)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setIconSize(QPixmap(96, 54).size())
        self.table.verticalHeader().setDefaultSectionSize(60)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.table.horizontalHeader().sectionClicked.connect(self._sort_requested)
        self.table.doubleClicked.connect(self._activate_row)
        root.addWidget(self.table, 1)
        actions = QGridLayout()
        for column, (label, mode) in enumerate(((ui_text('Select All'), 'all'), (ui_text('Select None'), 'none'), (ui_text('Invert Selection'), 'invert'))):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, value=mode: self.model.set_selection(value))
            actions.addWidget(button, 0, column)
        cancel = QPushButton(ui_text('Cancel'))
        cancel.clicked.connect(self.cancel_requested)
        self.download = QPushButton(ui_text('Stop Parsing and Download Selected'))
        self.download.setObjectName('primaryButton')
        self.download.clicked.connect(self._emit_download_request)
        actions.addWidget(cancel, 1, 1)
        actions.addWidget(self.download, 1, 2)
        actions.setColumnStretch(0, 1)
        actions.setColumnStretch(1, 1)
        actions.setColumnStretch(2, 1)
        root.addLayout(actions)

    def reset(self, title: str = '') -> None:
        self._filter_timer.stop()
        self.model.clear_source()
        self._storage_preview_provider = None
        controls = (
            self.search, self.state, self.date_after, self.date_before,
            self.duration_min, self.duration_max,
        )
        for control in controls:
            control.blockSignals(True)
        try:
            self.search.clear()
            self.state.setCurrentIndex(0)
            self.date_after.clear()
            self.date_before.clear()
            self.duration_min.setValue(0)
            self.duration_max.setValue(0)
        finally:
            for control in controls:
                control.blockSignals(False)
        self.proxy.set_query('')
        self.proxy.set_state('all')
        self.proxy.set_limits('', '', 0, 0)
        if self.proxy.sourceModel() is not self.model:
            self.proxy.setSourceModel(self.model)
        if self.table.model() is not self.proxy:
            self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.title.setText(title or ui_text('Parsing Collection'))
        self.parsing = True
        self._update_summary()
        self.download.setText(ui_text('Stop Parsing and Download Selected'))

    def append_entries(self, entries: list[Mapping[str, Any]]) -> None:
        self.model.append(entries)
        self._update_summary()

    def set_paged_entries(
        self,
        total_count: int,
        loader: Callable[[int, int], list[dict[str, Any]]],
        *,
        selection_updater: Callable[[int, bool], None],
        selection_setter: Callable[[str], None],
        selected_loader: Callable[[], list[dict[str, Any]]],
        selected_counter: Callable[[], int],
        view_loader: Callable[[int, int, Mapping[str, Any]], list[dict[str, Any]]] | None = None,
        view_counter: Callable[[Mapping[str, Any]], int] | None = None,
    ) -> None:
        self.table.setSortingEnabled(False)
        self.proxy.setSourceModel(None)
        self.model.set_paged_source(
            total_count,
            loader,
            selection_updater=selection_updater,
            selection_setter=selection_setter,
            selected_loader=selected_loader,
            selected_counter=selected_counter,
            view_loader=view_loader,
            view_counter=view_counter,
        )
        self.table.setModel(self.model)
        self.table.horizontalHeader().setSortIndicatorShown(True)
        self.table.horizontalHeader().setSortIndicator(1, Qt.AscendingOrder)
        self._update_summary()

    def set_storage_preview_provider(self, provider: Callable[[], str] | None) -> None:
        self._storage_preview_provider = provider
        self._update_summary()

    def set_metadata(self, metadata: Mapping[str, Any]) -> None:
        if metadata.get('title'):
            self.title.setText(str(metadata['title']))

    def set_finished(self) -> None:
        self.parsing = False
        self.download.setText(ui_text('Download Selected'))
        self._update_summary()

    def _emit_download_request(self) -> None:
        self.download_requested.emit(None if self.model.is_paged else self.model.selected_entries())

    def _update_summary(self) -> None:
        selected = self.model.selected_count()
        self.summary.setText(ui_format(
            '{parsed} parsed · {selected} selected',
            parsed=self.model.source_count(),
            selected=selected,
        ))
        self.storage_preview.setText(
            self._storage_preview_provider() if self._storage_preview_provider else ''
        )

    def _update_limits(self, *_args) -> None:
        self._filters_changed()

    def _filters_changed(self, *_args) -> None:
        self._filter_timer.start()

    def _apply_filters(self) -> None:
        if self.model.is_paged:
            self.model.set_paged_view(
                query=self.search.text(),
                state=self.state.currentData() or 'all',
                date_after=self.date_after.text(),
                date_before=self.date_before.text(),
                duration_min=self.duration_min.value(),
                duration_max=self.duration_max.value(),
            )
            return
        self.proxy.set_query(self.search.text())
        self.proxy.set_state(self.state.currentData() or 'all')
        self.proxy.set_limits(
            self.date_after.text(), self.date_before.text(),
            self.duration_min.value(), self.duration_max.value(),
        )

    def _sort_requested(self, section: int) -> None:
        if not self.model.is_paged:
            return
        columns = {
            1: 'collection_index', 3: 'title', 4: 'uploader', 5: 'duration',
            6: 'upload_date', 7: 'live_status', 8: 'availability',
        }
        column = columns.get(int(section))
        if not column:
            return
        current = self.model.paged_view_options()
        descending = (
            not bool(current.get('sort_descending'))
            if current.get('sort_column') == column else False
        )
        self.model.set_paged_view(sort_column=column, sort_descending=descending)
        self.table.horizontalHeader().setSortIndicator(
            section, Qt.DescendingOrder if descending else Qt.AscendingOrder,
        )

    def _activate_row(self, proxy_index: QModelIndex) -> None:
        source = (
            proxy_index if self.table.model() is self.model
            else self.proxy.mapToSource(proxy_index)
        )
        if source.isValid():
            entry = self.model.entry_at(source.row())
            if entry.get('entry_kind') == 'collection':
                self.nested_requested.emit(dict(entry))


class CollectionTaskModel(QAbstractTableModel):
    HEADERS = ('#', 'Title', 'Status', 'Progress', 'Speed', 'ETA', 'File')

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tasks: list[DownloadTask] = []
        self._row_by_id: dict[str, int] = {}

    @staticmethod
    def _sort_key(task: DownloadTask) -> tuple[int, str]:
        return task.collection_index, task.id

    def set_tasks(self, tasks: list[DownloadTask]) -> None:
        self.beginResetModel()
        self.tasks = sorted(tasks, key=self._sort_key)
        self._reindex()
        self.endResetModel()

    def upsert_task(self, task: DownloadTask) -> None:
        row = self._row_by_id.get(task.id)
        if row is not None:
            self.tasks[row] = task
            self.dataChanged.emit(
                self.index(row, 0),
                self.index(row, self.columnCount() - 1),
                [Qt.DisplayRole],
            )
            return
        sort_key = self._sort_key(task)
        row = len(self.tasks)
        for position, current in enumerate(self.tasks):
            if sort_key < self._sort_key(current):
                row = position
                break
        self.beginInsertRows(QModelIndex(), row, row)
        self.tasks.insert(row, task)
        self._reindex()
        self.endInsertRows()

    def remove_task(self, task_id: str) -> None:
        row = self._row_by_id.get(str(task_id or ''))
        if row is None:
            return
        self.beginRemoveRows(QModelIndex(), row, row)
        self.tasks.pop(row)
        self._reindex()
        self.endRemoveRows()

    def _reindex(self) -> None:
        self._row_by_id = {
            task.id: row
            for row, task in enumerate(self.tasks)
        }

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.tasks)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal and 0 <= section < len(self.HEADERS):
            return ui_text(self.HEADERS[section])
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid() or role != Qt.DisplayRole:
            return None
        task = self.tasks[index.row()]
        values = (
            task.collection_index,
            task.title,
            task.status,
            f"{float(task.progress or 0):.1f}%",
            task.speed,
            task.eta,
            task.media_path or task.current_filename,
        )
        return values[index.column()]


class CollectionTaskFilterProxy(QSortFilterProxyModel):
    STATUS_GROUPS = {
        'active': frozenset({
            'downloading', 'canceling', 'waiting_selection', 'parsing_collection',
        }),
        'queued': frozenset({'queued'}),
        'paused': frozenset({'paused', '暂停中'}),
        'completed': frozenset({'completed'}),
        'failed': frozenset({'failed', 'partial_failed', 'canceled'}),
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.query = ''
        self.state = 'all'

    def set_query(self, query: str) -> None:
        query = str(query or '').casefold()
        if query == self.query:
            return
        self.beginFilterChange()
        self.query = query
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def set_state(self, state: str) -> None:
        state = str(state or 'all')
        if state == self.state:
            return
        self.beginFilterChange()
        self.state = state
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:
        model = self.sourceModel()
        if not isinstance(model, CollectionTaskModel):
            return True
        task = model.tasks[source_row]
        if self.query and self.query not in ' '.join((
            str(task.title or ''),
            str(task.url or ''),
            str(task.id or ''),
        )).casefold():
            return False
        return (
            self.state == 'all'
            or task.status in self.STATUS_GROUPS.get(self.state, ())
        )


class CollectionDetailPage(QWidget):
    back_requested = Signal()
    action_requested = Signal(str, str)
    nested_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.parent_task_id = ''
        self.model = CollectionTaskModel(self)
        self.proxy = CollectionTaskFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        root = QVBoxLayout(self)
        header = QHBoxLayout()
        back = QPushButton(ui_text('Back to Task List'))
        back.clicked.connect(self.back_requested)
        self.title = QLabel(ui_text('Collection Details'))
        self.title.setObjectName('pageTitle')
        self.summary = QLabel()
        header.addWidget(back)
        header.addWidget(self.title)
        header.addWidget(self.summary)
        header.addStretch(1)
        root.addLayout(header)
        filters = QGridLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(ui_text('Search child tasks'))
        self.search.textChanged.connect(self.proxy.set_query)
        self.state = QComboBox()
        for label, value in (
            (ui_text('All'), 'all'), (ui_text('Active'), 'active'),
            (ui_text('Queued'), 'queued'), (ui_text('Paused'), 'paused'),
            (ui_text('Completed'), 'completed'), (ui_text('Failed'), 'failed'),
        ):
            self.state.addItem(label, value)
        self.state.currentIndexChanged.connect(lambda: self.proxy.set_state(self.state.currentData()))
        filters.addWidget(self.search, 0, 0)
        filters.addWidget(self.state, 0, 1)
        filters.setColumnStretch(0, 1)
        root.addLayout(filters)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setSelectionMode(QTableView.SingleSelection)
        self.table.setSortingEnabled(True)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.doubleClicked.connect(self._open_nested)
        root.addWidget(self.table, 1)
        actions = QGridLayout()
        for column, (label, action) in enumerate((
            (ui_text('Open File'), 'open'), (ui_text('View Logs'), 'log'),
            (ui_text('Pause'), 'pause'), (ui_text('Resume'), 'resume'),
            (ui_text('Retry'), 'retry'), (ui_text('Delete Record'), 'delete'),
        )):
            button = QPushButton(label)
            button.clicked.connect(lambda _checked=False, value=action: self._emit_action(value))
            actions.addWidget(button, column // 3, column % 3)
        for column in range(3):
            actions.setColumnStretch(column, 1)
        root.addLayout(actions)

    def set_collection(
        self,
        parent_task: DownloadTask,
        children: list[DownloadTask],
    ) -> None:
        self.parent_task_id = parent_task.id
        self.title.setText(parent_task.title or ui_text('Collection Details'))
        self.model.set_tasks(children)
        self._update_summary()

    def upsert_task(self, task: DownloadTask) -> None:
        if task.parent_task_id != self.parent_task_id:
            return
        self.model.upsert_task(task)
        self._update_summary()

    def remove_task(self, task_id: str) -> None:
        self.model.remove_task(task_id)
        self._update_summary()

    def _update_summary(self) -> None:
        children = self.model.tasks
        completed = sum(task.status == 'completed' for task in children)
        failed = sum(
            task.status in {'failed', 'partial_failed'}
            for task in children
        )
        self.summary.setText(ui_format(
            '{total} items · {completed} completed · {failed} failed',
            total=len(children), completed=completed, failed=failed,
        ))

    def _selected_task(self) -> DownloadTask | None:
        indexes = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not indexes:
            return None
        source = self.proxy.mapToSource(indexes[0])
        if source.isValid() and 0 <= source.row() < len(self.model.tasks):
            return self.model.tasks[source.row()]
        return None

    def selected_task_id(self) -> str:
        task = self._selected_task()
        return task.id if task is not None else ''

    def _emit_action(self, action: str) -> None:
        task_id = self.selected_task_id()
        if task_id:
            self.action_requested.emit(action, task_id)

    def _open_nested(self, _index: QModelIndex) -> None:
        task = self._selected_task()
        if task is not None and task.task_kind == 'collection':
            self.nested_requested.emit(task.id)
