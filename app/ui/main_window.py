from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QSize, QEvent, QItemSelectionModel
from PySide6.QtGui import QFontMetrics, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QMenu,
    QSizePolicy,
    QProgressBar,
    QDialog,
    QDialogButtonBox,
    QCheckBox,
    QAbstractItemView,
    QSpinBox,
)

from app.core.app_settings import AppSettings
from app.core.download_service import DownloadService, DownloadTask, bundled_ffmpeg_path
from app.core.paths import initialize_data_layout
from app.core.publish_service import PublishService
from app.storage.database import Database
from app.storage.models import MediaItem


STATUS_TEXT = {
    "queued": "排队中",
    "downloading": "下载中",
    "canceling": "取消中",
    "暂停中": "暂停中",
    "paused": "已暂停",
    "waiting_selection": "等待选择分辨率",
    "deleted": "文件已删除",
    "completed": "已完成",
    "failed": "失败",
    "canceled": "已取消",
}
PLATFORM_TEXT = {
    "douyin": "抖音",
    "bilibili": "哔哩哔哩",
    "tencent": "视频号",
    "kuaishou": "快手",
    "toutiao": "今日头条",
}
PUBLISH_STATUS_TEXT = {
    "pending": "待发布",
    "uploading": "发布中",
    "success": "已成功",
    "failed": "失败",
}


class DownloadTaskCard(QFrame):
    cancel_requested = Signal(str)
    pause_requested = Signal(str)
    resume_requested = Signal(str)
    retry_requested = Signal(str)
    open_requested = Signal(str)
    context_requested = Signal(str, object)
    selection_requested = Signal(str, object)
    checked_changed = Signal(str, bool)

    def __init__(self, task: DownloadTask):
        super().__init__()
        self.task_id = task.id
        self._thumbnail_loaded_path = ""
        self._title_text = ""
        self._url_text = ""
        self.setObjectName("taskCard")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.thumbnail = QLabel("视频")
        self.thumbnail.setFixedSize(116, 68)
        self.thumbnail.setAlignment(Qt.AlignCenter)
        self.thumbnail.setObjectName("taskThumbnail")
        self.check_box = QCheckBox()
        self.check_box.setFixedWidth(22)
        self.check_box.toggled.connect(lambda checked: self.checked_changed.emit(self.task_id, checked))
        self.check_box.setContextMenuPolicy(Qt.CustomContextMenu)
        self.check_box.customContextMenuRequested.connect(
            lambda pos: self.context_requested.emit(self.task_id, self.check_box.mapToGlobal(pos))
        )

        self.title = QLabel()
        self.title.setWordWrap(False)
        self.title.setMinimumWidth(0)
        self.title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.url = QLabel()
        self.url.setObjectName("mutedText")
        self.url.setMinimumWidth(0)
        self.url.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.url.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.status = QLabel()
        self.status.setObjectName("taskStatus")
        self.status.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(True)
        self.progress.setFixedHeight(18)
        self.details = QLabel()
        self.details.setObjectName("mutedText")

        self.action = QPushButton()
        self.action.setFixedWidth(76)
        self.action.clicked.connect(self._action_clicked)
        for widget in (self, self.thumbnail, self.title, self.url, self.status, self.progress, self.details):
            widget.setContextMenuPolicy(Qt.CustomContextMenu)
            widget.customContextMenuRequested.connect(
                lambda pos, source=widget: self.context_requested.emit(self.task_id, source.mapToGlobal(pos))
            )
            widget.installEventFilter(self)
        self.check_box.installEventFilter(self)

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(4)
        text_layout.addWidget(self.title)
        text_layout.addWidget(self.url)
        text_layout.addWidget(self.progress)
        text_layout.addWidget(self.details)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(12)
        layout.addWidget(self.thumbnail)
        layout.insertWidget(0, self.check_box)
        layout.addLayout(text_layout, 1)
        layout.addWidget(self.status)
        layout.addWidget(self.action)
        self.update_task(task)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            self.selection_requested.emit(self.task_id, event.modifiers())
            # The task card owns selection state.  Intercept the checkbox
            # click as well so Shift/Ctrl work consistently on every part of
            # the row instead of only on the labels.
            if watched is self.check_box:
                return True
        return super().eventFilter(watched, event)

    def set_selected(self, selected: bool) -> None:
        self.check_box.blockSignals(True)
        self.check_box.setChecked(selected)
        self.check_box.blockSignals(False)
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)

    def _action_clicked(self) -> None:
        status = getattr(self, "_status", "")
        if status == "downloading":
            self.pause_requested.emit(self.task_id)
        elif status == "paused":
            self.resume_requested.emit(self.task_id)
        elif status in {"queued", "canceling", "暂停中", "waiting_selection"}:
            self.cancel_requested.emit(self.task_id)
        elif status in {"failed", "canceled", "deleted"}:
            self.retry_requested.emit(self.task_id)
        elif status == "completed":
            self.open_requested.emit(self.task_id)

    def update_media(self, media: MediaItem) -> None:
        if media.thumbnail_path and Path(media.thumbnail_path).exists():
            pixmap = QPixmap(media.thumbnail_path)
            if not pixmap.isNull():
                self.thumbnail.setText("")
                self.thumbnail.setPixmap(pixmap.scaled(116, 68, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        self.title.setText(media.title or self.title.text())
        self.url.setText(media.uploader or media.source_url)

    def update_task(self, task: DownloadTask) -> None:
        self._status = task.status
        self._title_text = task.title or task.url
        self._url_text = task.url
        self._refresh_elided_text()
        # Restore the cover from the persisted path after an application
        # restart.  The image itself is kept on disk, never embedded in
        # SQLite, and a missing path simply falls back to the placeholder.
        if task.thumbnail_path and Path(task.thumbnail_path).exists() and task.thumbnail_path != self._thumbnail_loaded_path:
            pixmap = QPixmap(task.thumbnail_path)
            if not pixmap.isNull():
                self.thumbnail.setText("")
                self.thumbnail.setPixmap(pixmap.scaled(116, 68, Qt.KeepAspectRatio, Qt.SmoothTransformation))
                self._thumbnail_loaded_path = task.thumbnail_path
        elif not task.thumbnail_path or not Path(task.thumbnail_path).exists():
            self.thumbnail.setPixmap(QPixmap())
            self.thumbnail.setText("视频")
            self._thumbnail_loaded_path = ""
        self.progress.setValue(int(task.progress))
        self.status.setText(STATUS_TEXT.get(task.status, task.status))
        status_color = {
            "downloading": "#2f7bdc",
            "queued": "#8b96a6",
            "paused": "#d48716",
            "暂停中": "#d48716",
            "completed": "#20a35a",
            "failed": "#d64444",
            "canceled": "#8b96a6",
            "deleted": "#d48716",
        }.get(task.status, "#2f7bdc")
        self.status.setStyleSheet(f"color: {status_color}; font-weight: 600;")
        if task.status == "failed":
            self.status.setToolTip(task.error)
        details = "  ".join(x for x in [task.size, task.speed, f"剩余 {task.eta}" if task.eta else ""] if x)
        self.details.setText(details or "等待任务开始")
        if task.status == "downloading":
            self.action.setText("暂停")
            self.action.setEnabled(True)
        elif task.status == "paused":
            self.action.setText("继续")
            self.action.setEnabled(True)
        elif task.status == "waiting_selection":
            self.action.setText("取消")
            self.action.setEnabled(True)
        elif task.status in {"queued", "canceling", "暂停中"}:
            self.action.setText("取消")
            self.action.setEnabled(task.status == "queued")
        elif task.status in {"failed", "canceled"}:
            self.action.setText("重试")
            self.action.setEnabled(True)
        elif task.status == "deleted":
            self.action.setText("重新下载")
            self.action.setEnabled(True)
        else:
            self.action.setText("打开文件夹")
            self.action.setEnabled(task.status == "completed")

    def _refresh_elided_text(self) -> None:
        """Keep long titles and URLs readable without growing every card."""
        title_width = max(160, self.title.width())
        url_width = max(160, self.url.width())
        metrics = QFontMetrics(self.title.font())
        self.title.setText(metrics.elidedText(self._title_text, Qt.ElideRight, title_width))
        self.url.setText(metrics.elidedText(self._url_text, Qt.ElideMiddle, url_width))
        self.title.setToolTip(self._title_text)
        self.url.setToolTip(self._url_text)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh_elided_text()


class FormatSelectionDialog(QDialog):
    """Compact format picker with a cover preview and a five-row viewport."""

    def __init__(self, title: str, thumbnail_path: str, choices: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("选择视频分辨率")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        header = QHBoxLayout()
        cover = QLabel("封面")
        cover.setFixedSize(148, 84)
        cover.setAlignment(Qt.AlignCenter)
        cover.setObjectName("formatCover")
        if thumbnail_path and Path(thumbnail_path).exists():
            pixmap = QPixmap(thumbnail_path)
            if not pixmap.isNull():
                cover.setText("")
                cover.setPixmap(pixmap.scaled(148, 84, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        heading = QLabel(title or "请选择下载格式")
        heading.setWordWrap(True)
        heading.setMaximumHeight(58)
        heading.setToolTip(title or "")
        header.addWidget(cover)
        header.addWidget(heading, 1)
        layout.addLayout(header)

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setSpacing(3)
        rows = min(5, max(1, len(choices)))
        self.list.setFixedHeight(rows * 52 + 12)
        for choice in choices:
            item = QListWidgetItem()
            item.setData(Qt.UserRole, choice)
            item.setSizeHint(QSize(0, 48))
            row = QWidget()
            row.setObjectName("formatRow")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(10, 3, 10, 3)
            row_layout.setSpacing(8)
            text = QLabel()
            text.setWordWrap(False)
            note = choice.get('format_note') or '视频 + 音频'
            text.setText(f"{choice.get('height', '?')}p  ·  {choice.get('ext', '?')}  ·  {choice.get('fps', '')}帧/秒  ·  {note}")
            text.setToolTip(choice.get("label", ""))
            row_layout.addWidget(text, 1)
            self.list.addItem(item)
            self.list.setItemWidget(item, row)
        if self.list.count():
            self.list.setCurrentRow(0)
        layout.addWidget(self.list)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("确定")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def selected_choice(self) -> dict | None:
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item else None


class DashboardPage(QWidget):
    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.window = window
        self.cards: dict[str, DownloadTaskCard] = {}
        self.items: dict[str, QListWidgetItem] = {}
        self._selection_anchor_row = -1

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        title_row = QHBoxLayout()
        title = QLabel("下载任务")
        title.setObjectName("pageTitle")
        self.count_label = QLabel("0 个任务")
        self.count_label.setObjectName("mutedText")
        title_row.addWidget(title)
        title_row.addWidget(self.count_label)
        title_row.addStretch(1)
        root.addLayout(title_row)

        input_group = QGroupBox("新建下载任务")
        input_row = QHBoxLayout(input_group)
        input_row.setContentsMargins(10, 8, 10, 8)
        self.url = QLineEdit()
        self.url.setPlaceholderText("粘贴视频或播放列表链接，回车即可添加任务")
        self.url.returnPressed.connect(self.start)
        add_button = QPushButton("添加并下载")
        add_button.setObjectName("primaryButton")
        add_button.clicked.connect(self.start)
        input_row.addWidget(self.url, 1)
        input_row.addWidget(add_button)
        root.addWidget(input_group)

        options_group = QGroupBox("下载参数")
        options = QHBoxLayout(options_group)
        options.setContentsMargins(10, 8, 10, 8)
        options.addWidget(QLabel("画质"))
        self.quality = QComboBox()
        self.quality.addItem("最高画质", "best")
        self.quality.addItem("1080p", "1080p")
        self.quality.addItem("720p", "720p")
        self.quality.addItem("自定义（解析后选择）", "custom")
        saved_quality = window.app_settings.get("quality")
        self.quality.setCurrentIndex(max(0, self.quality.findData(saved_quality)))
        self.quality.currentIndexChanged.connect(lambda: self._save("quality", self.quality.currentData()))
        options.addWidget(self.quality)
        options.addWidget(QLabel("列表处理"))
        self.playlist_mode = QComboBox()
        self.playlist_mode.addItem("自动识别（推荐）", "auto")
        self.playlist_mode.addItem("仅下载单个视频", "single")
        self.playlist_mode.addItem("下载整个专辑/播放列表", "playlist")
        saved_mode = window.app_settings.get("playlist_mode")
        if saved_mode not in {"auto", "single", "playlist"}:
            saved_mode = "playlist" if window.app_settings.get("download_album") == "1" else "auto"
        self.playlist_mode.setCurrentIndex(max(0, self.playlist_mode.findData(saved_mode)))
        self.playlist_mode.currentIndexChanged.connect(lambda: self._save("playlist_mode", self.playlist_mode.currentData()))
        self.playlist_mode.setToolTip("自动识别 YouTube 视频、专辑或播放列表，并显示视频数量")
        options.addWidget(self.playlist_mode)
        options.addSpacing(12)
        options.addWidget(QLabel("代理"))
        self.proxy = QLineEdit(window.app_settings.get("proxy"))
        self.proxy.setPlaceholderText("可选")
        self.proxy.setMaximumWidth(220)
        options.addWidget(self.proxy)
        root.addWidget(options_group)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("任务列表"))
        self.filter_box = QComboBox()
        self.filter_box.addItems(["全部", "下载中", "排队中", "已完成", "文件已删除", "失败"])
        self.filter_box.currentTextChanged.connect(self.apply_filter)
        toolbar.addWidget(self.filter_box)
        toolbar.addStretch(1)
        self.download_dir_hint = QLabel()
        self.download_dir_hint.setObjectName("mutedText")
        self.download_dir_hint.setToolTip("下载目录请在“设置”页面统一修改")
        toolbar.addWidget(self.download_dir_hint, 1)
        open_dir = QPushButton("打开下载目录")
        open_dir.clicked.connect(self.open_download_dir)
        toolbar.addWidget(open_dir)
        settings_button = QPushButton("目录设置")
        settings_button.clicked.connect(lambda: self.window.tabs.setCurrentWidget(self.window.settings))
        toolbar.addWidget(settings_button)
        root.addLayout(toolbar)

        self.task_list = QListWidget()
        self.task_list.setSpacing(8)
        self.task_list.setFrameShape(QFrame.NoFrame)
        self.task_list.setObjectName("taskList")
        self.task_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.task_list.itemSelectionChanged.connect(self.sync_selection)
        self.task_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.task_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.task_list.customContextMenuRequested.connect(self.task_context_menu)
        root.addWidget(self.task_list, 1)
        self.empty_label = QLabel("还没有下载任务\n粘贴视频或播放列表链接后点击“添加并下载”\n下载目录可在“设置”页面修改")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setObjectName("emptyState")
        root.addWidget(self.empty_label)
        self.status = QLabel("就绪")
        self.status.setObjectName("mutedText")
        root.addWidget(self.status)
        self.refresh_settings()

    def _save(self, key: str, value: str) -> None:
        self.window.app_settings.set(key, str(value))
        self.window.app_settings.sync()

    def refresh_settings(self) -> None:
        path = self.window.app_settings.get("download_dir")
        self.download_dir_hint.setText(f"当前下载目录：{path}")

    def open_download_dir(self) -> None:
        path = self._ensure_download_dir()
        if path is not None:
            os.startfile(str(path))

    def _ensure_download_dir(self) -> Path | None:
        raw_path = self.window.app_settings.get("download_dir").strip()
        if not raw_path:
            QMessageBox.warning(self, "目录不可用", "请先在“设置”页面配置下载保存目录")
            return None
        path = Path(raw_path).expanduser()
        try:
            if path.exists() and not path.is_dir():
                raise OSError("目标路径已存在但不是目录")
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "目录不可用", f"无法使用当前下载目录：\n{path}\n\n{exc}")
            return None
        return path

    def start(self) -> None:
        url = self.url.text().strip()
        if not url:
            QMessageBox.warning(self, "提示", "请输入视频或播放列表链接")
            return
        output_path = self._ensure_download_dir()
        if output_path is None:
            return
        output_dir = str(output_path)
        self._save("proxy", self.proxy.text().strip())
        self._save("quality", self.quality.currentData())
        self.window.download_service.enqueue(
            url,
            output_dir,
            self.proxy.text().strip(),
            quality=self.quality.currentData(),
            filename_template=self.window.app_settings.get("filename_template"),
            ffmpeg_path=self.window.app_settings.get("ffmpeg_path"),
            download_album=self.playlist_mode.currentData() == "playlist",
            playlist_mode=self.playlist_mode.currentData(),
        )
        self.url.clear()
        self.status.setText("任务已加入队列，可以继续添加其他链接")

    def add_task(self, task: DownloadTask) -> None:
        card = DownloadTaskCard(task)
        card.cancel_requested.connect(self.window.download_service.cancel)
        card.pause_requested.connect(self.window.download_service.pause)
        card.resume_requested.connect(self.window.download_service.resume)
        card.retry_requested.connect(self.window.download_service.retry)
        card.open_requested.connect(self.open_task_folder)
        card.context_requested.connect(self.task_context_menu_for_task)
        card.selection_requested.connect(self.select_task_from_card)
        card.checked_changed.connect(self.check_task)
        item = QListWidgetItem()
        item.setSizeHint(QSize(0, 108))
        self.task_list.addItem(item)
        self.task_list.setItemWidget(item, card)
        self.cards[task.id] = card
        self.items[task.id] = item
        self._update_empty_state()
        self._update_count()

    def select_task_from_card(self, task_id: str, modifiers) -> None:
        item = self.items.get(task_id)
        if item is None:
            return
        row = self.task_list.row(item)
        anchor = self._selection_anchor_row
        if anchor < 0:
            anchor = self.task_list.currentRow()
        if modifiers & Qt.ShiftModifier and anchor >= 0:
            start, end = sorted((anchor, row))
            self.task_list.clearSelection()
            for index in range(start, end + 1):
                self.task_list.item(index).setSelected(True)
        elif modifiers & Qt.ControlModifier:
            item.setSelected(not item.isSelected())
            self._selection_anchor_row = row
        else:
            self.task_list.clearSelection()
            item.setSelected(True)
            self._selection_anchor_row = row
        # Keep the range selected; the default setCurrentItem behavior may
        # clear the other selected rows in ExtendedSelection mode.
        self.task_list.setCurrentItem(item, QItemSelectionModel.NoUpdate)
        self.sync_selection()

    def check_task(self, task_id: str, checked: bool) -> None:
        item = self.items.get(task_id)
        if item is not None and item.isSelected() != checked:
            item.setSelected(checked)
        self.sync_selection()

    def sync_selection(self) -> None:
        selected = {id(item) for item in self.task_list.selectedItems()}
        for task_id, item in self.items.items():
            card = self.cards.get(task_id)
            if card:
                card.set_selected(id(item) in selected)

    def update_task(self, task: DownloadTask) -> None:
        card = self.cards.get(task.id)
        if card:
            card.update_task(task)
            if task.thumbnail_path and Path(task.thumbnail_path).exists():
                pixmap = QPixmap(task.thumbnail_path)
                if not pixmap.isNull():
                    card.thumbnail.setText("")
                    card.thumbnail.setPixmap(pixmap.scaled(116, 68, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.items[task.id].setSizeHint(card.sizeHint())
        self.apply_filter()
        self._update_count()

    def update_progress(self, task_id: str, _data: dict) -> None:
        task = self.window.download_service.tasks.get(task_id)
        if task:
            self.update_task(task)

    def media_completed(self, task_id: str, media: MediaItem) -> None:
        task = self.window.download_service.tasks.get(task_id)
        card = self.cards.get(task_id)
        if task:
            task.title = media.title or task.title
            task.media_path = media.video_path
            task.thumbnail_path = media.thumbnail_path
        if card:
            card.update_media(media)
            if task:
                card.update_task(task)
        self.window.completed.refresh()

    def finished(self, task_id: str, status: str, error: str) -> None:
        task = self.window.download_service.tasks.get(task_id)
        if task:
            self.update_task(task)
        if status == "failed":
            self.status.setText(f"任务失败：{error[:180]}")
        elif status == "completed":
            self.status.setText("下载完成，媒体已加入完成列表")
        elif status == "canceled":
            self.status.setText("任务已取消")

    def playlist_info(self, task_id: str, payload: dict) -> None:
        if payload.get("is_playlist"):
            count = int(payload.get("count") or 0)
            self.status.setText(f"已解析播放列表：共 {count} 个视频" if count else "已识别为播放列表，正在准备下载")
        else:
            self.status.setText("已识别为单个视频")

    def choose_format(self, task_id: str, payload: dict) -> None:
        choices = payload.get("choices") or []
        task = self.window.download_service.tasks.get(task_id)
        if not task:
            return
        if not choices:
            QMessageBox.warning(self, "无法选择分辨率", "没有解析到可用的视频分辨率。")
            self.window.download_service.set_format_selector(task_id, "")
            return
        dialog = FormatSelectionDialog(
            task.title or payload.get("title", "请选择下载格式"),
            payload.get("thumbnail_path") or task.thumbnail_path,
            choices,
            self,
        )
        if dialog.exec() == QDialog.Accepted and dialog.selected_choice():
            choice = dialog.selected_choice()
            self.window.download_service.set_format_selector(task_id, choice["selector"])
            self.status.setText(f"已选择 {choice.get('height', '')}p，开始下载")
        else:
            # The picker is only a preview step. Closing/cancelling it means
            # abandon this request entirely, not create a visible "已取消"
            # download task.
            self.window.download_service.discard_task(task_id)
            self.status.setText("已关闭画质预览，未创建下载任务")

    def open_task_folder(self, task_id: str) -> None:
        task = self.window.download_service.tasks.get(task_id)
        if task:
            path = Path(task.media_path).parent if task.media_path else Path(task.output_dir)
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))

    def task_context_menu(self, pos) -> None:
        item = self.task_list.itemAt(pos)
        if not item:
            return
        task_id = next((task_id for task_id, list_item in self.items.items() if list_item is item), None)
        self.show_task_menu(task_id or "", self.task_list.mapToGlobal(pos))

    def task_context_menu_for_task(self, task_id: str, global_pos) -> None:
        self.show_task_menu(task_id, global_pos)

    def show_task_menu(self, task_id: str, global_pos) -> None:
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
        menu = QMenu(self)
        copy_link_action = menu.addAction("复制视频链接")
        copy_folder_action = menu.addAction("复制视频文件夹路径")
        menu.addSeparator()
        if task.status == "downloading":
            pause_action = menu.addAction("暂停下载")
        elif task.status == "paused":
            pause_action = menu.addAction("继续下载")
        else:
            pause_action = None
        cancel_action = menu.addAction("取消任务") if task.status in {"queued", "downloading", "canceling", "暂停中", "waiting_selection"} else None
        redownload_action = menu.addAction("重新下载") if task.status in {"failed", "canceled", "completed", "paused", "deleted"} else None
        custom_action = menu.addAction("选择分辨率并重新下载") if task.status in {"failed", "canceled", "completed", "paused", "deleted"} else None
        menu.addSeparator()
        open_action = menu.addAction("打开视频文件夹")
        delete_action = menu.addAction("删除任务")
        chosen = menu.exec(global_pos)
        if chosen == copy_link_action:
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(task.url)
            self.status.setText("视频链接已复制")
        elif chosen == copy_folder_action:
            from PySide6.QtWidgets import QApplication
            folder = str(Path(task.media_path).parent if task.media_path else Path(task.output_dir))
            QApplication.clipboard().setText(folder)
            self.status.setText("视频文件夹路径已复制")
        elif chosen == pause_action:
            if task.status == "downloading":
                self.window.download_service.pause(task.id)
            else:
                self.window.download_service.resume(task.id)
        elif chosen == cancel_action:
            self.window.download_service.cancel(task.id)
        elif chosen == redownload_action:
            self.window.download_service.redownload(task.id)
        elif chosen == custom_action:
            self.window.download_service.redownload(task.id, quality_override="custom")
        elif chosen == open_action:
            self.open_task_folder(task.id)
        elif chosen == delete_action:
            self.delete_tasks_with_prompt([task.id])

    def selected_task_ids(self) -> list[str]:
        selected_items = {id(item) for item in self.task_list.selectedItems()}
        return [task_id for task_id, item in self.items.items() if id(item) in selected_items]

    def show_batch_menu(self, task_ids: list[str], global_pos) -> None:
        menu = QMenu(self)
        pause_action = menu.addAction(f"批量暂停（{len(task_ids)}）")
        start_action = menu.addAction(f"批量开始（{len(task_ids)}）")
        menu.addSeparator()
        delete_action = menu.addAction(f"批量删除（{len(task_ids)}）")
        chosen = menu.exec(global_pos)
        if chosen == pause_action:
            for task_id in task_ids:
                self.window.download_service.pause(task_id)
        elif chosen == start_action:
            for task_id in task_ids:
                self.window.download_service.start_task(task_id)
        elif chosen == delete_action:
            self.delete_tasks_with_prompt(task_ids)

    def delete_tasks_with_prompt(self, task_ids: list[str]) -> None:
        eligible = [task_id for task_id in task_ids if task_id in self.window.download_service.tasks]
        if not eligible:
            return
        box = QMessageBox(self)
        box.setWindowTitle("删除任务")
        box.setText(f"将删除 {len(eligible)} 个任务记录。正在下载的任务会自动取消后删除。是否同时删除对应的视频文件？")
        keep_button = box.addButton("只删任务记录", QMessageBox.AcceptRole)
        file_button = box.addButton("任务和视频文件都删", QMessageBox.DestructiveRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked not in {keep_button, file_button}:
            return
        delete_files = clicked is file_button
        for task_id in eligible:
            self.window.download_service.delete_task(task_id, delete_files=delete_files)

    def remove_task(self, task_id: str) -> None:
        item = self.items.pop(task_id, None)
        card = self.cards.pop(task_id, None)
        if item is not None:
            row = self.task_list.row(item)
            removed = self.task_list.takeItem(row)
            del removed
        if card is not None:
            card.deleteLater()
        self._update_empty_state()
        self._update_count()
        self.status.setText("任务已删除")

    def apply_filter(self, _value: str = "") -> None:
        selected = self.filter_box.currentText()
        allowed = {
            "全部": None,
            "下载中": {"downloading", "canceling"},
            "排队中": {"queued"},
            "已完成": {"completed"},
            "文件已删除": {"deleted"},
            "失败": {"failed", "canceled"},
        }[selected]
        for task_id, item in self.items.items():
            task = self.window.download_service.tasks.get(task_id)
            item.setHidden(bool(allowed and task and task.status not in allowed))

    def _update_empty_state(self) -> None:
        self.empty_label.setVisible(self.task_list.count() == 0)

    def _update_count(self) -> None:
        count = self.task_list.count()
        active = len(self.window.download_service.workers)
        self.count_label.setText(f"{count} 个任务 · {active} 个进行中")

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        card_width = max(320, self.task_list.viewport().width() - 8)
        for card in self.cards.values():
            card.setFixedWidth(card_width)


class BrowserPage(QWidget):
    def __init__(self, storage_dir: Path):
        super().__init__()
        layout = QVBoxLayout(self)
        selector = QComboBox()
        selector.addItems(["空白页", "YouTube", "抖音", "哔哩哔哩", "视频号", "快手", "今日头条"])
        layout.addWidget(selector)
        try:
            from PySide6.QtWebEngineWidgets import QWebEngineView
            from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
            from PySide6.QtCore import QUrl

            self.browser = QWebEngineView()
            profile_dir = storage_dir / "browser"
            cache_dir = profile_dir / "cache"
            profile_dir.mkdir(parents=True, exist_ok=True)
            cache_dir.mkdir(parents=True, exist_ok=True)
            self.profile = QWebEngineProfile("huifa", self)
            self.profile.setPersistentStoragePath(str(profile_dir / "profile"))
            self.profile.setCachePath(str(cache_dir))
            self.profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
            self.browser.setPage(QWebEnginePage(self.profile, self.browser))
            layout.addWidget(self.browser)
            urls = {
                "空白页": "about:blank",
                "YouTube": "https://accounts.google.com/",
                "抖音": "https://creator.douyin.com/",
                "哔哩哔哩": "https://member.bilibili.com/",
                "视频号": "https://channels.weixin.qq.com/",
                "快手": "https://cp.kuaishou.com/",
                "今日头条": "https://mp.toutiao.com/",
            }
            selector.currentTextChanged.connect(lambda value: self.browser.setUrl(QUrl(urls[value])))
            self.browser.setUrl(QUrl("about:blank"))
        except ImportError:
            layout.addWidget(QLabel("QtWebEngine 未安装；请安装 PySide6 完整组件。"))


class CompletedPage(QWidget):
    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.window = window
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(QLabel("已完成的视频"))
        header.addStretch(1)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        header.addWidget(refresh)
        layout.addLayout(header)
        self.list = QListWidget()
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self.menu)
        layout.addWidget(self.list)
        self.refresh()

    def refresh(self) -> None:
        self.list.clear()
        for media in self.window.db.list_media():
            item = QListWidgetItem(f"{media.title}  |  {media.uploader}  |  {media.video_path}")
            item.setData(Qt.UserRole, media.id)
            self.list.addItem(item)

    def menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        if not item:
            return
        menu = QMenu(self)
        publish = menu.addAction("创建发布任务")
        open_folder = menu.addAction("打开文件夹")
        action = menu.exec(self.list.mapToGlobal(pos))
        media = self.window.db.get_media(item.data(Qt.UserRole))
        if not media:
            return
        if action == publish:
            self.window.open_publish(media)
        elif action == open_folder:
            os.startfile(str(Path(media.video_path).parent))


class PublishPage(QWidget):
    def __init__(self, window: "MainWindow", media: MediaItem):
        super().__init__()
        self.window, self.media = window, media
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"媒体：{media.title}"))
        self.title = QLineEdit(media.title)
        self.description = QTextEdit(media.description)
        self.topics = QLineEdit(" ".join(f"#{x}" for x in media.tags))
        form = QFormLayout()
        form.addRow("标题", self.title)
        form.addRow("内容", self.description)
        form.addRow("话题", self.topics)
        layout.addLayout(form)
        self.platforms = QListWidget()
        platform_names = {
            "douyin": "抖音",
            "bilibili": "哔哩哔哩",
            "tencent": "视频号",
            "kuaishou": "快手",
            "toutiao": "今日头条",
        }
        for name, label in platform_names.items():
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.platforms.addItem(item)
        layout.addWidget(QLabel("平台"))
        layout.addWidget(self.platforms)
        button = QPushButton("保存并加入发布队列")
        button.clicked.connect(self.submit)
        layout.addWidget(button)

    def submit(self) -> None:
        platforms = [self.platforms.item(i).data(Qt.UserRole) for i in range(self.platforms.count()) if self.platforms.item(i).checkState() == Qt.Checked]
        if not platforms:
            QMessageBox.warning(self, "提示", "至少选择一个平台")
            return
        tags = [x.lstrip("#") for x in self.topics.text().split() if x.strip()]
        self.window.publish_service.create_tasks(
            self.media,
            platforms,
            {"title": self.title.text(), "description": self.description.toPlainText(), "tags": tags},
            {},
        )
        QMessageBox.information(self, "完成", "已加入发布队列")
        self.window.tabs.setCurrentWidget(self.window.publish_queue)


class PublishQueuePage(QWidget):
    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.window = window
        layout = QVBoxLayout(self)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["编号", "平台", "状态", "标题", "结果"])
        layout.addWidget(self.tree)
        controls = QHBoxLayout()
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self.refresh)
        run = QPushButton("执行选中任务")
        run.clicked.connect(self.run_selected)
        controls.addWidget(refresh)
        controls.addWidget(run)
        controls.addStretch(1)
        layout.addLayout(controls)
        self.refresh()

    def refresh(self) -> None:
        self.tree.clear()
        for row in self.window.db.list_publish_tasks():
            self.tree.addTopLevelItem(QTreeWidgetItem([
                str(row["id"]),
                PLATFORM_TEXT.get(row["platform"], row["platform"]),
                PUBLISH_STATUS_TEXT.get(row["status"], row["status"]),
                row["title"],
                row["result"] or "",
            ]))

    def run_selected(self) -> None:
        item = self.tree.currentItem()
        if item:
            self.window.publish_service.run_task(int(item.text(0)))
            self.refresh()


class SettingsPage(QWidget):
    def __init__(self, window: "MainWindow"):
        super().__init__()
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 20, 24, 20)
        root.setSpacing(14)
        title = QLabel("应用设置")
        title.setObjectName("pageTitle")
        subtitle = QLabel("下载目录只在这里配置，下载页面会自动使用当前设置。")
        subtitle.setObjectName("mutedText")
        root.addWidget(title)
        root.addWidget(subtitle)

        bundled = bundled_ffmpeg_path()
        self.download_dir = QLineEdit(window.app_settings.get("download_dir"))
        self.proxy = QLineEdit(window.app_settings.get("proxy"))
        self.proxy.setPlaceholderText("可选，例如 http://127.0.0.1:7890")
        self.template = QLineEdit(window.app_settings.get("filename_template"))
        self.sau = QLineEdit(window.app_settings.get("sau_path"))
        self.ffmpeg = QLineEdit(window.app_settings.get("ffmpeg_path") or (str(bundled) if bundled.exists() else "ffmpeg"))
        self.max_concurrent = QSpinBox()
        self.max_concurrent.setRange(1, 8)
        self.max_concurrent.setValue(max(1, min(8, int(window.app_settings.get("max_concurrent") or 3))))

        download_group = QGroupBox("下载设置")
        download_form = QFormLayout(download_group)
        download_form.setContentsMargins(14, 14, 14, 14)
        download_form.setVerticalSpacing(10)
        download_form.addRow("下载保存目录", self._path_row(self.download_dir, "选择目录", self.choose_download_dir))
        download_form.addRow("文件名模板", self.template)
        download_form.addRow("并行下载数", self.max_concurrent)
        root.addWidget(download_group)

        network_group = QGroupBox("网络设置")
        network_form = QFormLayout(network_group)
        network_form.setContentsMargins(14, 14, 14, 14)
        network_form.addRow("默认代理", self.proxy)
        root.addWidget(network_group)

        tools_group = QGroupBox("工具设置")
        tools_form = QFormLayout(tools_group)
        tools_form.setContentsMargins(14, 14, 14, 14)
        tools_form.setVerticalSpacing(10)
        tools_form.addRow("上传工具路径", self._path_row(self.sau, "浏览", self.choose_sau))
        tools_form.addRow("FFmpeg 路径", self._path_row(self.ffmpeg, "浏览", self.choose_ffmpeg))
        root.addWidget(tools_group)

        save = QPushButton("保存配置")
        save.setObjectName("primaryButton")
        save.setMinimumWidth(120)
        save.clicked.connect(lambda: window.save_settings(self))
        save_row = QHBoxLayout()
        save_row.addStretch(1)
        save_row.addWidget(save)
        root.addLayout(save_row)
        root.addStretch(1)

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

    def choose_download_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择下载保存目录", self.download_dir.text())
        if path:
            self.download_dir.setText(path)

    def choose_sau(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择上传工具", "", "可执行文件 (*.exe);;所有文件 (*)")
        if path:
            self.sau.setText(path)

    def choose_ffmpeg(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 FFmpeg", "", "可执行文件 (*.exe);;所有文件 (*)")
        if path:
            self.ffmpeg.setText(path)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("汇发")
        self.setMinimumSize(820, 560)
        self.resize(1080, 700)
        self.setStyleSheet(
            """
            QWidget { font-family: "Microsoft YaHei"; font-size: 13px; }
            QLineEdit, QComboBox { min-height: 30px; border: 1px solid #d9dee7; border-radius: 6px; padding: 0 8px; background: white; }
            QPushButton { min-height: 30px; border: 1px solid #d9dee7; border-radius: 6px; padding: 0 12px; background: white; }
            QPushButton:hover { background: #f0f5ff; }
            QPushButton#primaryButton { color: white; background: #18a957; border: none; font-weight: 600; }
            QPushButton#primaryButton:hover { background: #128945; }
            QGroupBox { border: 1px solid #e3e8ef; border-radius: 10px; margin-top: 8px; padding-top: 10px; background: #ffffff; font-weight: 600; }
            QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #354052; background: #ffffff; }
            QLabel#pageTitle { font-size: 22px; font-weight: 700; color: #172033; }
            QLabel#mutedText { color: #87909f; }
            QLabel#emptyState { color: #9aa3b2; padding: 40px; }
            QListWidget#taskList { background: #f6f8fb; border: none; }
            QFrame#taskCard { background: white; border: 1px solid #e3e8ef; border-radius: 10px; }
            QFrame#taskCard[selected="true"] { background: #edf7ff; border: 1px solid #2b8cff; }
            QCheckBox { spacing: 0; }
            QLabel#taskThumbnail { background: #e9eef5; color: #8090a6; border-radius: 6px; font-weight: 700; }
            QLabel#taskStatus { color: #3f6fca; min-width: 54px; }
            QProgressBar { border: none; background: #edf1f5; border-radius: 7px; text-align: center; color: #4d5968; }
            QProgressBar::chunk { background: #39b86a; border-radius: 7px; }
            QLabel#formatCover, QLabel#formatRowCover { background: #e9eef5; color: #8090a6; border-radius: 6px; }
            QListWidget { border: 1px solid #d9dee7; border-radius: 6px; background: #fbfcfe; }
            QListWidget::item { border-radius: 6px; }
            QListWidget::item:selected { background: #e8f3ff; border: 1px solid #2b8cff; }
            QWidget#formatRow { border-bottom: 1px solid #e0e5ec; }
            QTabWidget::pane { border: none; }
            """
        )
        data_dir = initialize_data_layout()
        self.app_settings = AppSettings()
        self.db = Database(data_dir / "app.db")
        self.download_service = DownloadService(self.db, max_concurrent=int(self.app_settings.get("max_concurrent") or 3))
        self.publish_service = PublishService(self.db)
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.dashboard = DashboardPage(self)
        self.browser_page = BrowserPage(data_dir)
        self.completed = CompletedPage(self)
        self.publish_queue = PublishQueuePage(self)
        self.settings = SettingsPage(self)
        for name, page in [
            ("下载任务", self.dashboard),
            ("浏览器/账号", self.browser_page),
            ("完成列表", self.completed),
            ("发布队列", self.publish_queue),
            ("设置", self.settings),
        ]:
            self.tabs.addTab(page, name)

        self.download_service.task_added.connect(self.dashboard.add_task)
        self.download_service.task_updated.connect(self.dashboard.update_task)
        self.download_service.task_progress.connect(self.dashboard.update_progress)
        self.download_service.formats_ready.connect(self.dashboard.choose_format)
        self.download_service.playlist_info.connect(self.dashboard.playlist_info)
        self.download_service.task_media_completed.connect(self.dashboard.media_completed)
        self.download_service.task_finished.connect(self.dashboard.finished)
        self.download_service.task_deleted.connect(self.dashboard.remove_task)
        self.publish_service.status.connect(lambda *_: self.publish_queue.refresh())

        # Restore tasks saved in SQLite after a previous application run.
        for task in self.download_service.restore_tasks():
            self.dashboard.add_task(task)

    def open_publish(self, media: MediaItem) -> None:
        page = PublishPage(self, media)
        self.tabs.addTab(page, "发布编辑")
        self.tabs.setCurrentWidget(page)

    def save_settings(self, page: SettingsPage) -> None:
        download_dir = page.download_dir.text().strip()
        if not download_dir:
            QMessageBox.warning(self, "提示", "下载保存目录不能为空")
            return
        download_path = Path(download_dir).expanduser()
        try:
            if download_path.exists() and not download_path.is_dir():
                raise OSError("目标路径已存在但不是目录")
            download_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "目录不可用", f"无法使用下载保存目录：\n{download_dir}\n\n{exc}")
            return
        self.app_settings.set("download_dir", str(download_path))
        self.app_settings.set("proxy", page.proxy.text().strip())
        self.app_settings.set("filename_template", page.template.text().strip())
        self.app_settings.set("sau_path", page.sau.text().strip())
        self.app_settings.set("ffmpeg_path", page.ffmpeg.text().strip())
        self.app_settings.set("max_concurrent", str(page.max_concurrent.value()))
        self.app_settings.sync()
        self.download_service.max_concurrent = page.max_concurrent.value()
        self.download_service._start_next()
        self.dashboard.proxy.setText(self.app_settings.get("proxy"))
        self.dashboard.refresh_settings()
        QMessageBox.information(
            self,
            "配置已保存",
            "下载设置、网络设置和工具路径已保存。\n\n"
            f"当前下载目录：{download_path}",
        )

    def closeEvent(self, event) -> None:
        # Persist the latest task state before the window exits. Active tasks
        # are restored as paused on the next launch instead of disappearing.
        for task_id, worker in list(self.download_service.workers.items()):
            active = self.download_service.tasks.get(task_id)
            if active:
                active.pause_requested = True
                active.cancel_requested = False
                active.status = "暂停中"
                self.download_service.db.upsert_download_task(active)
            worker.cancel()
        for task in self.download_service.tasks.values():
            if task.status == "downloading":
                task.status = "paused"
            self.download_service.db.upsert_download_task(task)
        event.accept()
