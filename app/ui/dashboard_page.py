from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.core.download_service import DownloadTask
from app.core.paths import resolve_portable_path
from app.storage.models import MediaItem
from app.ui.collection_workflow import CollectionWorkflowController
from app.ui.dashboard_responsive_layout import (
    DashboardResponsiveControls,
    DashboardResponsiveLayoutController,
)
from app.ui.download_control_presentation import transcode_encoder_label
from app.ui.download_dialogs import (
    DownloadLogDialog,
    DownloadReadinessDialog,
)
from app.ui.download_options import CollectionDetailPage, CollectionSelectionPage
from app.ui.download_submission_workflow import DownloadSubmissionWorkflow
from app.ui.i18n import format_text as ui_format
from app.ui.i18n import runtime_text, text as ui_text
from app.ui.metric_card import TaskMetricCard
from app.ui.quick_download_settings import (
    QuickDownloadControls,
    QuickDownloadSettingsController,
)
from app.ui.quick_download_controls import (
    InsetComboBox,
    InsetMenuButton,
    build_quick_content_selector,
    build_quick_quality_selector,
)
from app.ui.supported_sites_dialog import SupportedSitesDialog
from app.ui.task_card import DownloadTaskCard
from app.ui.task_auth_actions import (
    TaskAuthActionController,
)
from app.ui.task_context_menu import (
    TaskContextMenuController,
    read_download_task_ids,
)
from app.ui.task_list import (
    TaskListPagingState,
)
from app.ui.task_list_presentation import (
    TaskListPresentationController,
)
from app.ui.task_list_restore import (
    TaskListRestoreController,
)
from app.ui.task_rows import TaskRowController
from app.ui.task_format_selection import (
    TaskFormatSelectionController,
)

if TYPE_CHECKING:
    from app.ui.main_window import MainWindow


TASK_PAGE_SIZE = 50
TASK_RENDER_BATCH_SIZE = 8


def reveal_file_or_folder(file_path: str | Path, fallback_folder: str | Path) -> None:
    """Open a task location, selecting its finished file when possible."""
    target = Path(file_path) if file_path else None
    if target is not None and target.is_file() and sys.platform == "win32":
        subprocess.Popen(["explorer.exe", "/select,", str(target)])
        return

    folder = target.parent if target is not None and target.is_file() else Path(fallback_folder)
    folder.mkdir(parents=True, exist_ok=True)
    os.startfile(str(folder))


class DashboardPage(QWidget):
    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.window = window
        self._initialize_task_state()
        root = self._build_overview_shell()

        self._build_overview_header(root)
        root.addWidget(self._build_download_input_group())
        root.addWidget(self._build_smart_download_bar())

        self.focus_url_shortcut = QShortcut(QKeySequence("Ctrl+L"), self)
        self.focus_url_shortcut.activated.connect(self.focus_url_input)

        # Filtering and task actions stay on separate rows so controls remain
        # usable at the application's minimum supported width.
        root.addLayout(self._build_task_filter_toolbar())
        root.addLayout(self._build_task_action_toolbar())
        self._build_task_content(root)
        self._build_collection_pages()
        self._initialize_quick_download_settings()
        self._initialize_responsive_layout()
        self._initialize_task_controllers()
        self._initialize_download_workflows()
        self._finalize_initial_state()

    def _initialize_task_state(self) -> None:
        self.cards: dict[str, DownloadTaskCard] = {}
        self.items: dict[str, QListWidgetItem] = {}
        self.task_paging = TaskListPagingState()
        self._filter_materialized_key = ""
        self._task_render_timer = QTimer(self)
        self._task_render_timer.setInterval(0)
        self._search_filter_timer = QTimer(self)
        self._search_filter_timer.setSingleShot(True)
        self._search_filter_timer.setInterval(300)
        self._search_filter_timer.timeout.connect(self.apply_filter)
        self._collection_detail_history: list[str] = []

    def _build_overview_shell(self) -> QVBoxLayout:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.page_stack = QStackedWidget()
        outer.addWidget(self.page_stack)
        self.overview_page = QWidget()
        self.page_stack.addWidget(self.overview_page)
        root = QVBoxLayout(self.overview_page)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)
        return root

    def _build_smart_download_bar(self) -> QFrame:
        smart_mode = QFrame()
        smart_mode.setObjectName("smartModeBar")
        # The toolbar can reflow into additional rows. Without a minimum
        # vertical policy, Qt may compress those rows until controls overlap.
        smart_mode.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        self._smart_mode_bar = smart_mode

        smart_layout = QGridLayout(smart_mode)
        smart_layout.setContentsMargins(12, 8, 10, 8)
        smart_layout.setHorizontalSpacing(8)
        smart_layout.setVerticalSpacing(5)

        self.smart_mode_badge = QLabel(ui_text('Smart Download'))
        self.smart_mode_badge.setObjectName("smartModeBadge")
        smart_layout.addWidget(self.smart_mode_badge, 0, 0)
        self.smart_mode_summary = QLabel()
        self.smart_mode_summary.setObjectName("smartModeSummary")
        self.smart_mode_summary.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        smart_layout.addWidget(self.smart_mode_summary, 0, 1)

        self._smart_content_label = QLabel(ui_text('Download Content'))
        smart_layout.addWidget(self._smart_content_label, 1, 0)
        self.task_download_menu = self._build_quick_content_selector(smart_mode)
        smart_layout.addWidget(self.task_download_menu, 1, 1)
        self._smart_quality_label = QLabel(ui_text('Download Quality'))
        smart_layout.addWidget(self._smart_quality_label, 1, 2)
        self.task_quality_menu = self._build_quick_quality_selector(smart_mode)
        smart_layout.addWidget(self.task_quality_menu, 1, 3)
        self._smart_format_label = QLabel(ui_text('Download Format'))
        smart_layout.addWidget(self._smart_format_label, 1, 4)

        self.task_container = InsetComboBox()
        self.task_container.setCompactIcon("format")
        self.task_container.setObjectName('quickDownloadCombo')
        for label, value in (
            (ui_text('Automatic'), 'auto'),
            ('MP4', 'mp4'),
            ('MKV', 'mkv'),
        ):
            self.task_container.addItem(label, value)
        self.task_container.setMinimumWidth(104)
        self.task_container.setMaximumWidth(150)
        self.task_container.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon,
        )
        self.task_container.setToolTip(ui_text(
            'Changes here are saved immediately and stay synchronized with Download Settings.',
        ))
        self.task_container.setAccessibleName(ui_text('Download Format'))
        smart_layout.addWidget(self.task_container, 1, 5)

        smart_layout.setColumnStretch(1, 1)
        self._smart_layout = smart_layout
        return smart_mode

    def _initialize_quick_download_settings(self) -> None:
        self.quick_download_settings = QuickDownloadSettingsController(
            window=self.window,
            controls=QuickDownloadControls(
                download_dir_hint=self.download_dir_hint,
                mode_badge=self.smart_mode_badge,
                summary=self.smart_mode_summary,
                content_menu=self.task_download_menu,
                quality_menu=self.task_quality_menu,
                container=self.task_container,
                content_mode=self.task_content_mode,
                quality=self.task_quality,
                video_fps=self.task_video_fps,
                source_codec=self.task_source_codec,
                vr_mode=self.task_vr_mode,
                subtitle_language=self.task_subtitle_language,
                audio_track=self.task_audio_track,
                content_actions=self._download_content_actions,
                subtitle_actions=self._download_subtitle_actions,
                audio_track_actions=self._download_audio_track_actions,
                quality_actions=self._quality_actions,
                fps_actions=self._quality_fps_actions,
                codec_actions=self._quality_codec_actions,
                vr_actions=self._quality_vr_actions,
                fps_menu=self._quality_fps_menu,
                codec_menu=self._quality_codec_menu,
                vr_menu=self._quality_vr_menu,
            ),
        )
        for combo in (
            self.task_content_mode,
            self.task_quality,
            self.task_video_fps,
            self.task_source_codec,
            self.task_vr_mode,
            self.task_container,
            self.task_subtitle_language,
            self.task_audio_track,
        ):
            combo.currentIndexChanged.connect(self.quick_download_settings.persist)

    def _initialize_responsive_layout(self) -> None:
        self.responsive_layout = DashboardResponsiveLayoutController(
            DashboardResponsiveControls(
                input_layout=self._input_layout,
                url=self.url,
                paste_button=self._input_paste_button,
                add_button=self._input_add_button,
                smart_layout=self._smart_layout,
                smart_bar=self._smart_mode_bar,
                smart_badge=self.smart_mode_badge,
                smart_summary=self.smart_mode_summary,
                content_label=self._smart_content_label,
                content_menu=self.task_download_menu,
                quality_label=self._smart_quality_label,
                quality_menu=self.task_quality_menu,
                format_label=self._smart_format_label,
                container=self.task_container,
                filter_layout=self._filter_layout,
                tasks_label=self._filter_tasks_label,
                search_box=self.search_box,
                sort_label=self._filter_sort_label,
                sort_box=self.sort_box,
                filter_box=self.filter_box,
                action_layout=self._action_layout,
                download_dir_hint=self.download_dir_hint,
                open_download_dir_button=self._open_download_dir_button,
                action_separator=self._action_separator,
                pause_all_button=self.pause_all_button,
                resume_all_button=self.resume_all_button,
                log_button=self.log_button,
                cleanup_button=self.cleanup_button,
            )
        )

    def _initialize_task_controllers(self) -> None:
        self.task_presentation = TaskListPresentationController(self)
        self.task_auth_actions = TaskAuthActionController(
            self.window.download_service,
            self.window.app_settings,
            lambda: self.window.db,
            self.resume_collection_probes,
        )
        self.task_format_selection = TaskFormatSelectionController(
            self,
            self.window.download_service,
            self.status,
        )
        self.task_rows = TaskRowController(self, page_size=TASK_PAGE_SIZE)
        self.task_restore = TaskListRestoreController(
            self,
            page_size=TASK_PAGE_SIZE,
            batch_size=TASK_RENDER_BATCH_SIZE,
        )
        self._task_render_timer.timeout.connect(self.task_restore.render_batch)
        self.load_more_button.clicked.connect(self.task_restore.load_more)

    def _initialize_download_workflows(self) -> None:
        self.submission_workflow = DownloadSubmissionWorkflow(
            parent=self,
            window=self.window,
            url_input=self.url,
            add_button=self.add_download_button,
            paste_button=self.paste_download_button,
            status_label=self.status,
            options_provider=self.quick_download_settings.global_options,
            acknowledge_submission=self._show_task_list_for_submission,
            start_collection=self.collection_workflow.start_probe,
        )
        self.task_menu_controller = TaskContextMenuController(
            parent=self,
            window=self.window,
            status_label=self.status,
            cancel_task=self.cancel_task,
            resume_task=self.resume_task_with_current_auth,
            retry_task=self.retry_task_with_current_auth,
            redownload_task=self.redownload_task_with_current_auth,
            open_collection=self._open_collection_from_menu,
            open_folder=self.open_task_folder,
            delete_tasks=self.delete_tasks_with_prompt,
        )

    def _finalize_initial_state(self) -> None:
        # An empty task list must not look like it contains a selectable row.
        # This is especially important after the last task is deleted or a
        # database is cleared while the window is open.
        self.task_presentation.refresh()
        self.quick_download_settings.sync_format_controls()
        self.quick_download_settings.sync_download_menu()
        self.quick_download_settings.sync_quality_menu()
        self._adapt_responsive_layout()
        self.refresh_settings()

    def _build_quick_content_selector(self, parent: QWidget) -> InsetMenuButton:
        selector = build_quick_content_selector(parent)
        self.task_content_mode = selector.content_mode
        self.task_subtitle_language = selector.subtitle_language
        self.task_audio_track = selector.audio_track
        self._download_content_actions = selector.content_actions
        self._download_subtitle_actions = selector.subtitle_actions
        self._download_audio_track_actions = selector.audio_track_actions
        return selector.button

    def _build_quick_quality_selector(self, parent: QWidget) -> InsetMenuButton:
        selector = build_quick_quality_selector(parent)
        self.task_quality = selector.quality
        self.task_video_fps = selector.video_fps
        self.task_source_codec = selector.source_codec
        self.task_vr_mode = selector.vr_mode
        self._quality_actions = selector.quality_actions
        self._quality_fps_actions = selector.fps_actions
        self._quality_codec_actions = selector.codec_actions
        self._quality_vr_actions = selector.vr_actions
        self._quality_fps_menu = selector.fps_menu
        self._quality_codec_menu = selector.codec_menu
        self._quality_vr_menu = selector.vr_menu
        return selector.button

    def _build_overview_header(self, root: QVBoxLayout) -> None:
        title_row = QHBoxLayout()
        title = QLabel(ui_text('Download Tasks'))
        title.setObjectName("pageTitle")
        self.count_label = QLabel(ui_format(
            '{count} tasks',
            context="task.count",
            count=0,
        ))
        self.count_label.setObjectName("mutedText")
        title_row.addWidget(title)
        title_row.addWidget(self.count_label)
        title_row.addStretch(1)
        root.addLayout(title_row)

        # Task counters are rendered in the bottom status bar. Preserve the
        # metric objects because filter activation and existing update paths
        # still use them, but do not allocate visible dashboard height.
        self.metric_cards: dict[str, TaskMetricCard] = {}
        for caption, filter_name, tone in (
            (ui_text('All Tasks'), "全部", "neutral"),
            (ui_text('Active'), "下载中", "active"),
            (ui_text('Queued Tasks'), "排队中", "queued"),
            (ui_text('Paused'), "已暂停", "paused"),
            (ui_text('Completed'), "已完成", "success"),
            (ui_text('Needs Attention'), "失败", "danger"),
        ):
            metric = TaskMetricCard(caption, filter_name, tone, self)
            metric.activated.connect(self._activate_metric_filter)
            metric.hide()
            self.metric_cards[filter_name] = metric

    def _build_download_input_group(self) -> QGroupBox:
        group = QGroupBox(ui_text('New Download Task'))
        layout = QGridLayout(group)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(6)

        self.url = QLineEdit()
        self.url.setClearButtonEnabled(True)
        self.url.setPlaceholderText(ui_text(
            'Paste a video or playlist URL, then press Enter',
        ))
        self.url.setAccessibleName(ui_text('Video or playlist URL'))
        self.url.setAccessibleDescription(ui_text(
            'Enter one URL or paste text containing multiple HTTP/HTTPS URLs',
        ))
        self.url.returnPressed.connect(self.start)

        paste_button = QPushButton(ui_text('Paste & Download'))
        paste_button.setObjectName("pasteDownloadButton")
        paste_button.setToolTip(ui_text(
            'Read one or more URLs from the clipboard and add them to the queue immediately (Ctrl+Shift+V)',
        ))
        paste_button.setAccessibleName(ui_text(
            'Paste clipboard URLs and download',
        ))
        paste_button.setShortcut(QKeySequence("Ctrl+Shift+V"))
        paste_button.clicked.connect(self.paste_and_download)
        self.paste_download_button = paste_button

        add_button = QPushButton(ui_text('Add & Download'))
        add_button.setObjectName("primaryButton")
        add_button.setAccessibleName(ui_text(
            'Add the entered URL and download',
        ))
        add_button.clicked.connect(self.start)
        self.add_download_button = add_button

        sites_button = QPushButton(ui_text('Supported Sites'))
        sites_button.setToolTip(ui_text(
            'View the extractor list provided by the current yt-dlp version',
        ))
        sites_button.clicked.connect(self.show_supported_sites)
        self._input_sites_button = sites_button
        self._input_paste_button = paste_button
        self._input_add_button = add_button
        for button in (sites_button, paste_button, add_button):
            button.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        # Supported-site discovery is available under About, so the primary
        # submit row remains focused on adding URLs.
        sites_button.hide()

        layout.addWidget(self.url, 0, 0)
        layout.addWidget(paste_button, 0, 1)
        layout.addWidget(add_button, 0, 2)
        layout.setColumnStretch(0, 1)
        self._input_layout = layout
        return group

    def _build_task_filter_toolbar(self) -> QGridLayout:
        layout = QGridLayout()
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(5)
        self._filter_tasks_label = QLabel(ui_text('Tasks'))
        layout.addWidget(self._filter_tasks_label, 0, 0)

        self.search_box = QLineEdit()
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setPlaceholderText(ui_text('Search title, URL or ID'))
        self.search_box.setMinimumWidth(180)
        self.search_box.textChanged.connect(self._schedule_task_filter)
        layout.addWidget(self.search_box, 0, 1)

        self._filter_sort_label = QLabel(ui_text('Sort'))
        layout.addWidget(self._filter_sort_label, 0, 2)
        self.sort_box = QComboBox()
        self.sort_box.addItem(ui_text('Added (newest first)'), "newest")
        self.sort_box.addItem(ui_text('Added (oldest first)'), "oldest")
        self.sort_box.addItem(ui_text('Title'), "title")
        self.sort_box.addItem(ui_text('Status'), "status")
        self.sort_box.currentIndexChanged.connect(self.sort_tasks)
        self.sort_box.setMinimumWidth(150)
        layout.addWidget(self.sort_box, 0, 3)

        self.filter_box = QComboBox()
        for label, key in (
            (ui_text('All'), "全部"),
            (ui_text('Downloading'), "下载中"),
            (ui_text('Queued'), "排队中"),
            (ui_text('Paused'), "已暂停"),
            (ui_text('Processing'), "处理中"),
            (ui_text('Completed'), "已完成"),
            (ui_text('File Missing'), "文件已删除"),
            (ui_text('Failed'), "失败"),
        ):
            self.filter_box.addItem(label, key)
        self.filter_box.currentTextChanged.connect(self.apply_filter)
        self.filter_box.setMinimumWidth(110)
        layout.addWidget(self.filter_box, 0, 4)
        layout.setColumnStretch(1, 1)
        self._filter_layout = layout
        return layout

    def _build_task_action_toolbar(self) -> QGridLayout:
        layout = QGridLayout()
        layout.setHorizontalSpacing(8)
        layout.setVerticalSpacing(5)
        self.download_dir_hint = QLabel()
        self.download_dir_hint.setObjectName("mutedText")
        self.download_dir_hint.setToolTip(ui_text(
            'Change the download folder from Settings',
        ))
        self.download_dir_hint.setMinimumWidth(0)
        self.download_dir_hint.setSizePolicy(
            QSizePolicy.Ignored,
            QSizePolicy.Preferred,
        )
        layout.addWidget(self.download_dir_hint, 0, 0)

        open_dir = QPushButton(ui_text('Open Download Folder'))
        open_dir.clicked.connect(self.open_download_dir)
        open_dir.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._open_download_dir_button = open_dir
        layout.addWidget(open_dir, 0, 1)

        separator = QFrame()
        separator.setFrameShape(QFrame.VLine)
        separator.setFrameShadow(QFrame.Sunken)
        separator.setObjectName("toolbarSeparator")
        self._action_separator = separator
        layout.addWidget(separator, 0, 2)

        pause_all = QPushButton(ui_text('Pause All'))
        pause_all.clicked.connect(self.pause_all)
        self.pause_all_button = pause_all
        resume_all = QPushButton(ui_text('Resume All'))
        resume_all.clicked.connect(self.resume_all)
        self.resume_all_button = resume_all
        self.log_button = QPushButton(ui_text('View Logs'))
        self.log_button.setEnabled(False)
        self.log_button.clicked.connect(self.show_selected_log)
        cleanup_button = QPushButton(ui_text('Clean Completed'))
        cleanup_button.clicked.connect(self.cleanup_completed)
        self.cleanup_button = cleanup_button
        for button in (pause_all, resume_all, self.log_button, cleanup_button):
            button.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        layout.addWidget(pause_all, 0, 3)
        layout.addWidget(resume_all, 0, 4)
        layout.addWidget(self.log_button, 0, 5)
        layout.addWidget(cleanup_button, 0, 6)
        layout.setColumnStretch(0, 1)
        self._action_layout = layout
        return layout

    def _build_task_content(self, root: QVBoxLayout) -> None:
        self.task_list = QListWidget()
        self.task_list.setSpacing(8)
        self.task_list.setFrameShape(QFrame.NoFrame)
        self.task_list.setObjectName("taskList")
        self.task_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.task_list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.task_list.setUniformItemSizes(True)
        self.task_list.itemSelectionChanged.connect(self.sync_selection)
        self.task_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.task_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.task_list.customContextMenuRequested.connect(self.task_context_menu)
        self.task_list.itemDoubleClicked.connect(self._task_double_clicked)

        self.load_more_button = QPushButton(ui_text('Load More History'))
        self.load_more_button.setObjectName("secondaryButton")
        self.load_more_button.hide()
        root.addWidget(self.load_more_button, 0, Qt.AlignHCenter)

        self.empty_label = QLabel(ui_text(
            'No download tasks yet\nPaste a video or playlist URL and click Add & Download\nChange the download folder in Settings',
        ))
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setObjectName("emptyState")
        self.task_content_stack = QStackedWidget()
        self.task_content_stack.addWidget(self.task_list)
        self.task_content_stack.addWidget(self.empty_label)
        root.addWidget(self.task_content_stack, 1)

        # The main status bar displays this text. Keep a hidden label as the
        # existing in-memory message source for task handlers.
        self.status = QLabel(ui_text('Ready'))
        self.status.setObjectName("mutedText")
        self.status.hide()

    def _build_collection_pages(self) -> None:
        self.collection_selection = CollectionSelectionPage()
        self.collection_workflow = CollectionWorkflowController(
            window=self.window,
            selection_view=self.collection_selection,
            page_stack=self.page_stack,
            overview_page=self.overview_page,
            status_label=self.status,
            parent=self,
        )
        self.collection_selection.download_requested.connect(
            self.collection_workflow.confirm_selection
        )
        self.collection_selection.cancel_requested.connect(
            self.collection_workflow.cancel_selection
        )
        self.collection_selection.nested_requested.connect(
            self.collection_workflow.parse_nested
        )
        self.page_stack.addWidget(self.collection_selection)

        self.collection_detail = CollectionDetailPage()
        self.collection_detail.back_requested.connect(self._collection_detail_back)
        self.collection_detail.action_requested.connect(
            self._collection_detail_action
        )
        self.collection_detail.nested_requested.connect(
            self._open_collection_detail
        )
        self.page_stack.addWidget(self.collection_detail)

    def refresh_settings(self) -> None:
        self.quick_download_settings.refresh()

    def request_shutdown(self) -> None:
        self.collection_workflow.request_shutdown()

    @property
    def collection_probe_running(self) -> bool:
        return self.collection_workflow.running

    def resume_collection_probes(self) -> None:
        self.collection_workflow.resume()

    def _adapt_responsive_layout(self) -> None:
        self.responsive_layout.apply(self.width())

    def _task_double_clicked(self, item: QListWidgetItem) -> None:
        task_id = str(item.data(Qt.UserRole) or "") if item is not None else ""
        task = self.window.download_service.tasks.get(task_id)
        if task and task.task_kind == 'collection':
            if task.status == 'waiting_selection' and not self.window.download_service.collection_children(task_id):
                request_id = self.collection_workflow.request_id_for_parent(task_id)
                if request_id:
                    self.collection_workflow.show_selection(request_id)
                    self.collection_selection.set_finished()
                    return
            self._collection_detail_history.clear()
            self._open_collection_detail(task_id)

    @Slot(str)
    def _open_collection_detail(self, task_id: str) -> None:
        task = self.window.download_service.tasks.get(task_id)
        if not task or task.task_kind != 'collection':
            return
        current = self.collection_detail.parent_task_id
        if current and current != task_id:
            self._collection_detail_history.append(current)
        self.collection_detail.set_collection(
            task,
            self.window.download_service.collection_children(task_id),
        )
        self.page_stack.setCurrentWidget(self.collection_detail)

    def _open_collection_from_menu(self, task_id: str) -> None:
        self._collection_detail_history.clear()
        self._open_collection_detail(task_id)

    @Slot()
    def _collection_detail_back(self) -> None:
        if self._collection_detail_history:
            previous = self._collection_detail_history.pop()
            task = self.window.download_service.tasks.get(previous)
            if task:
                self.collection_detail.set_collection(
                    task,
                    self.window.download_service.collection_children(previous),
                )
                return
        self.collection_detail.parent_task_id = ''
        self.page_stack.setCurrentWidget(self.overview_page)

    @Slot(str, str)
    def _collection_detail_action(self, action: str, task_id: str) -> None:
        service = self.window.download_service
        if action == 'pause':
            service.pause(task_id)
        elif action == 'resume':
            service.resume(task_id)
        elif action == 'retry':
            service.retry(task_id)
        elif action == 'delete':
            service.delete_task(task_id, False)
        elif action == 'log':
            task = service.tasks.get(task_id)
            if task:
                DownloadLogDialog(task, service.logs, self).exec()
        elif action == 'open':
            self.open_task_folder(task_id)
        parent = service.tasks.get(self.collection_detail.parent_task_id)
        if parent:
            self.collection_detail.set_collection(parent, service.collection_children(parent.id))

    def focus_url_input(self) -> None:
        self.url.setFocus(Qt.ShortcutFocusReason)
        self.url.selectAll()

    def open_download_dir(self) -> None:
        path = self._ensure_download_dir()
        if path is not None:
            try:
                os.startfile(str(path))
            except OSError as exc:
                QMessageBox.warning(
                    self,
                    ui_text('Cannot Open Folder'),
                    ui_format(
                        'The download folder could not be opened:\n{path}\n\n{error}',
                        path=path,
                        error=runtime_text(exc),
                    ),
                )

    def _ensure_download_dir(self) -> Path | None:
        raw_path = self.window.app_settings.get("download_dir").strip()
        if not raw_path:
            QMessageBox.warning(
                self,
                ui_text('Folder Unavailable'),
                ui_text(
                    'Configure the download folder on the Settings page first.',
                ),
            )
            return None
        path = resolve_portable_path(raw_path)
        try:
            if path.exists() and not path.is_dir():
                raise OSError("目标路径已存在但不是目录")
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(
                self,
                ui_text('Folder Unavailable'),
                ui_format(
                    'The current download folder cannot be used:\n{path}\n\n{error}',
                    path=path,
                    error=runtime_text(exc),
                ),
            )
            return None
        return path

    def _show_task_list_for_submission(self) -> None:
        """Make the acknowledgement row visible before background parsing."""

        self.page_stack.setCurrentWidget(self.overview_page)
        self.search_box.clear()
        self.filter_box.setCurrentIndex(0)

    def start(self) -> None:
        self.submission_workflow.submit_input()

    def paste_and_download(self) -> None:
        self.submission_workflow.paste_and_submit()

    def show_supported_sites(self) -> None:
        SupportedSitesDialog(self).exec()

    def show_download_readiness(self) -> None:
        DownloadReadinessDialog(self.window, self).exec()

    def add_task(self, task: DownloadTask, *, defer_refresh: bool = False) -> None:
        if task.parent_task_id:
            if self.collection_detail.parent_task_id == task.parent_task_id:
                self.collection_detail.upsert_task(task)
            return
        card = self.cards.get(task.id)
        if card is not None:
            card.update_task(task)
            if not defer_refresh:
                self.task_presentation.refresh()
        else:
            self.task_rows.insert_new(task, refresh=not defer_refresh)

    @Slot(object)
    def add_tasks(self, tasks: object) -> None:
        batch = [task for task in (tasks or []) if isinstance(task, DownloadTask)]
        top_level = [task for task in batch if not task.parent_task_id]
        child_tasks = [task for task in batch if task.parent_task_id]
        new_top_level: list[DownloadTask] = []
        for task in top_level:
            card = self.cards.get(task.id)
            if card is None:
                new_top_level.append(task)
            else:
                card.update_task(task)
        if new_top_level:
            self.task_rows.insert_many(new_top_level, refresh=False)
        elif top_level:
            self.task_rows.refresh_ordered_ids()
        if self.collection_detail.parent_task_id:
            for task in child_tasks:
                if task.parent_task_id == self.collection_detail.parent_task_id:
                    self.collection_detail.upsert_task(task)
        if top_level:
            self.apply_filter()
        else:
            self.task_presentation.refresh()

    def _new_task_card(self, task: DownloadTask) -> DownloadTaskCard:
        """Create and wire a task card in one place.

        Qt can destroy an item widget while QListWidget is relayouting.  A
        single factory lets the repair path replace a stale wrapper with a
        fully connected card instead of trying to reuse a deleted C++ object.
        """
        card = DownloadTaskCard(task)
        card.cancel_requested.connect(self.cancel_task)
        card.pause_requested.connect(self.window.download_service.pause)
        card.resume_requested.connect(self.resume_task_with_current_auth)
        card.retry_requested.connect(self.retry_task_with_current_auth)
        card.open_requested.connect(self.open_task_folder)
        card.context_requested.connect(self.task_context_menu_for_task)
        card.selection_requested.connect(self.task_presentation.select_from_card)
        return card

    def cancel_task(self, task_id: str) -> None:
        task = self.window.download_service.tasks.get(task_id)
        if task and task.task_kind == 'collection' and task.status in {
            'parsing_collection', 'waiting_selection'
        }:
            self.collection_workflow.cancel_parent(task_id)
            self.window.download_service.delete_task(task_id, False)
            return
        self.window.download_service.cancel(task_id)

    def resume_task_with_current_auth(self, task_id: str) -> None:
        self.task_auth_actions.resume(task_id)

    def retry_task_with_current_auth(self, task_id: str) -> None:
        self.task_auth_actions.retry(task_id)

    def start_task_with_current_auth(self, task_id: str) -> None:
        """Start an existing task without bypassing current authentication."""
        self.task_auth_actions.start(task_id)

    def redownload_task_with_current_auth(
        self,
        task_id: str,
        quality_override: str | None = None,
    ) -> str | None:
        return self.task_auth_actions.redownload(task_id, quality_override)

    def begin_task_restore(self, tasks: list[DownloadTask]) -> None:
        self.task_restore.begin(tasks)

    def sync_selection(self, *_args) -> None:
        self.task_presentation.sync_selection()

    def show_selected_log(self) -> None:
        task_ids = self.selected_task_ids()
        if len(task_ids) != 1:
            return
        task = self.window.download_service.tasks.get(task_ids[0])
        if task:
            DownloadLogDialog(task, self.window.download_service.logs, self).exec()

    def sort_tasks(self, _index: int = -1) -> None:
        selected_ids = set(self.selected_task_ids())
        self.task_rows.sort(selected_ids)
        self.task_presentation.reset_selection_anchor()

    def update_task(self, task: DownloadTask) -> None:
        if task.parent_task_id:
            if self.collection_detail.parent_task_id == task.parent_task_id:
                self.collection_detail.upsert_task(task)
            return
        card = self.cards.get(task.id)
        if card is None:
            self.add_task(task, defer_refresh=True)
        else:
            card.update_task(task)
        self.task_presentation.refresh()

    def update_progress(self, task_id: str, _data: dict) -> None:
        task = self.window.download_service.tasks.get(task_id)
        if not task:
            return
        if task.parent_task_id:
            if self.collection_detail.parent_task_id == task.parent_task_id:
                self.collection_detail.upsert_task(task)
        else:
            card = self.cards.get(task.id)
            if card is None:
                self.add_task(task, defer_refresh=True)
            else:
                card.update_task(task)

    def media_completed(self, task_id: str, media: MediaItem) -> None:
        task = self.window.download_service.tasks.get(task_id)
        if task:
            task.title = media.title or task.title
            task.media_path = media.video_path
            task.thumbnail_path = media.thumbnail_path
            task.uploader = media.uploader or ""
            task.downloaded_at = media.downloaded_at or ""
            if task.parent_task_id:
                if self.collection_detail.parent_task_id == task.parent_task_id:
                    self.collection_detail.upsert_task(task)
            else:
                card = self.cards.get(task.id)
                if card is None:
                    self.add_task(task, defer_refresh=True)
                    card = self.cards.get(task.id)
                if card is not None:
                    card.update_task(task)
                    card.update_media(media)
        self.window.completed.mark_dirty()

    def finished(self, task_id: str, status: str, error: str) -> None:
        task = self.window.download_service.tasks.get(task_id)
        if task:
            self.update_task(task)
        if status == "failed":
            self.status.setText(ui_format(
                'Task failed: {error}',
                error=runtime_text(error)[:180],
            ))
        elif status == "completed":
            self.status.setText(ui_text('Download complete; media added to Completed'))
        elif status == "canceled":
            self.status.setText(ui_text('Task canceled'))

    def playlist_info(self, task_id: str, payload: dict) -> None:
        if payload.get("is_playlist"):
            count = int(payload.get("count") or 0)
            self.status.setText(
                ui_format(
                    'Playlist parsed: {count} videos',
                    count=count,
                )
                if count
                else ui_text(
                    'Playlist recognized; preparing the download',
                )
            )
        else:
            self.status.setText(ui_text('Recognized as a single video'))

    def choose_format(self, task_id: str, payload: dict) -> None:
        self.task_format_selection.choose(task_id, payload)

    def open_task_folder(self, task_id: str) -> None:
        task = self.window.download_service.tasks.get(task_id)
        if task:
            try:
                reveal_file_or_folder(task.media_path, task.output_dir)
            except OSError as exc:
                path = Path(task.media_path) if task.media_path else Path(task.output_dir)
                QMessageBox.warning(
                    self,
                    ui_text('Cannot Open Folder'),
                    ui_format(
                        'The task folder could not be opened:\n{path}\n\n{error}',
                        path=path,
                        error=runtime_text(exc),
                    ),
                )

    def task_context_menu(self, pos) -> None:
        if self.task_list.count() == 0 or self.task_list.selectionMode() == QAbstractItemView.NoSelection:
            return
        item = self.task_list.itemAt(pos)
        task_id = str(item.data(Qt.UserRole) or "") if item is not None else ""
        if not task_id:
            return
        self.show_task_menu(task_id, self.task_list.viewport().mapToGlobal(pos))

    def task_context_menu_for_task(self, task_id: str, global_pos) -> None:
        if not task_id or task_id not in self.items or self.task_list.count() == 0:
            return
        self.show_task_menu(task_id, global_pos)

    def show_task_menu(self, task_id: str, global_pos) -> None:
        # The task card can outlive an externally deleted/recreated database
        # until the lightweight watcher runs.  Never show a destructive task
        # menu for a record that is no longer present in the current DB.
        live_ids = self._database_task_ids()
        if live_ids is None:
            self.status.setText(ui_text(
                'Task records could not be verified; no action was performed',
            ))
            return
        if task_id not in live_ids:
            if not live_ids:
                self._drop_all_stale_tasks()
            else:
                self._drop_stale_task(task_id)
            return
        task = self.window.download_service.tasks.get(task_id)
        if not task:
            return
        selected_ids = self.selected_task_ids()
        if task_id not in selected_ids:
            self.task_list.clearSelection()
            item = self.items.get(task_id)
            if item:
                item.setSelected(True)
            selected_ids = [task_id]
            self.sync_selection()
        if len(selected_ids) > 1:
            self.show_batch_menu(selected_ids, global_pos)
            return
        self.task_menu_controller.show(task, global_pos)

    @Slot(str, str, bool)
    def conversion_finished(self, task_id: str, result: str, skipped: bool) -> None:
        task = self.window.download_service.tasks.get(task_id)
        if task is not None:
            self.update_task(task)
        if skipped:
            reason, _, codec = str(result or "").partition(":")
            if reason == "keep_original":
                self.status.setText(ui_text(
                    'The encoder setting keeps the original format; no conversion is needed.',
                ))
            else:
                self.status.setText(ui_format(
                    'The video is already in the target {codec} format; conversion was skipped.',
                    codec=(codec or "target").upper(),
                ))
            return
        self.status.setText(ui_format(
            'Format conversion completed with {encoder}.',
            encoder=transcode_encoder_label(result),
        ))

    @Slot(str, str)
    def conversion_failed(self, task_id: str, error: str) -> None:
        task = self.window.download_service.tasks.get(task_id)
        if task is not None:
            self.update_task(task)
        QMessageBox.warning(
            self,
            ui_text('Format Conversion Failed'),
            ui_format(
                'The original file was kept unchanged.\n\n{error}',
                error=runtime_text(error),
            ),
        )

    def selected_task_ids(self) -> list[str]:
        return self.task_presentation.selected_ids()

    def show_batch_menu(self, task_ids: list[str], global_pos) -> None:
        menu = QMenu(self)
        pause_action = menu.addAction(ui_format('Pause Selected ({count})', count=len(task_ids)))
        start_action = menu.addAction(ui_format('Start Selected ({count})', count=len(task_ids)))
        menu.addSeparator()
        delete_action = menu.addAction(ui_format('Delete Selected ({count})', count=len(task_ids)))
        chosen = menu.exec(global_pos)
        if chosen == pause_action:
            for task_id in task_ids:
                self.window.download_service.pause(task_id)
        elif chosen == start_action:
            for task_id in task_ids:
                self.start_task_with_current_auth(task_id)
        elif chosen == delete_action:
            self.delete_tasks_with_prompt(task_ids)

    def delete_tasks_with_prompt(self, task_ids: list[str]) -> None:
        eligible = [task_id for task_id in task_ids if task_id in self.window.download_service.tasks]
        if not eligible:
            return
        box = QMessageBox(self)
        box.setWindowTitle(ui_text('Delete Tasks'))
        box.setText(ui_format(
            'Delete {count} task records. Active downloads will be canceled first. Also delete the corresponding video files?',
            count=len(eligible),
        ))
        keep_button = box.addButton(ui_text('Delete Task Records Only'), QMessageBox.AcceptRole)
        file_button = box.addButton(ui_text('Delete Tasks and Video Files'), QMessageBox.DestructiveRole)
        box.addButton(ui_text('Cancel'), QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked not in {keep_button, file_button}:
            return
        delete_files = clicked is file_button
        for task_id in eligible:
            self.window.download_service.delete_task(task_id, delete_files=delete_files)

    def _database_task_ids(self) -> set[str] | None:
        # A fresh read-only connection detects externally replaced databases
        # without relying on the long-lived application connection's view.
        return read_download_task_ids(self.window.db.path)

    def _drop_stale_task(self, task_id: str) -> None:
        self.window.download_service._unregister_task(task_id)
        self.window.download_service.queue = type(self.window.download_service.queue)(
            queued_id for queued_id in self.window.download_service.queue if queued_id != task_id
        )
        self.remove_task(task_id)
        self.status.setText(ui_text(
            'The task record no longer exists and was removed from the list',
        ))

    def _drop_all_stale_tasks(self) -> None:
        self.window.download_service.reset_task_cache()
        self.clear_tasks()
        self.status.setText(ui_text(
            'The database contains no task records; the list was cleared',
        ))

    def remove_task(self, task_id: str) -> None:
        self.collection_detail.remove_task(task_id)
        self.task_rows.remove_materialized(task_id)
        self.task_paging.remove(task_id)
        self.task_presentation.refresh()
        self.task_restore.update_load_more_button()
        self.status.setText(ui_text('Task deleted'))

    def clear_tasks(self) -> None:
        """Remove all task rows after an external database reset."""
        self.task_rows.clear()
        self.task_paging.clear()
        self._task_render_timer.stop()
        self.task_presentation.reset_selection_anchor()
        self.task_presentation.refresh()
        self.task_restore.update_load_more_button()

    def _schedule_task_filter(self, _value: str = "") -> None:
        self._search_filter_timer.start()

    def apply_filter(self, _value: str = "") -> None:
        self.task_presentation.apply_filter()

    def _activate_metric_filter(self, filter_name: str) -> None:
        self.task_presentation.activate_metric_filter(filter_name)

    def _prioritize_pending_matches(self) -> int:
        return self.task_restore.prioritize_pending_matches()

    def pause_all(self) -> None:
        for task_id, task in list(self.window.download_service.tasks.items()):
            if not task.parent_task_id and task.status in {"downloading", "queued"}:
                self.window.download_service.pause(task_id)

    def resume_all(self) -> None:
        for task_id, task in list(self.window.download_service.tasks.items()):
            if (
                not task.parent_task_id
                and task.status in {"paused", "failed", "partial_failed", "canceled"}
            ):
                self.start_task_with_current_auth(task_id)

    def cleanup_completed(self) -> None:
        task_ids = [
            task_id for task_id, task in self.window.download_service.tasks.items()
            if not task.parent_task_id and task.status == "completed"
        ]
        if not task_ids:
            self.status.setText(ui_text('There are no completed tasks to clean'))
            return
        box = QMessageBox(self)
        box.setWindowTitle(ui_text('Clean Completed Tasks'))
        box.setText(ui_format(
            'Clean {count} completed task records. Video files and the Completed catalog will not be deleted.',
            count=len(task_ids),
        ))
        clean_button = box.addButton(ui_text('Clean Records'), QMessageBox.AcceptRole)
        box.addButton(ui_text('Cancel'), QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is clean_button:
            for task_id in task_ids:
                self.window.download_service.delete_task(task_id, delete_files=False)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._adapt_responsive_layout()
        self.task_rows.sync_widths()
        # Child layouts can settle after the page resize event (for example
        # when the sidebar first appears). Recheck once on the next event-loop
        # turn so cards use the final viewport width instead of the old one.
        self.task_rows.schedule_width_sync()
        self.quick_download_settings.refresh_elided_text()
