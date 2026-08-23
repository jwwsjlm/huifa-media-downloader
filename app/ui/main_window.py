from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QSize
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
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
)

from app.core.app_settings import AppSettings
from app.core.download_service import DownloadService, DownloadTask, bundled_ffmpeg_path
from app.core.publish_service import PublishService
from app.storage.database import Database
from app.storage.models import MediaItem


STATUS_TEXT = {
    "queued": "排队中",
    "downloading": "下载中",
    "canceling": "取消中",
    "completed": "已完成",
    "failed": "失败",
    "canceled": "已取消",
}


class DownloadTaskCard(QFrame):
    cancel_requested = Signal(str)
    retry_requested = Signal(str)
    open_requested = Signal(str)

    def __init__(self, task: DownloadTask):
        super().__init__()
        self.task_id = task.id
        self.setObjectName("taskCard")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.thumbnail = QLabel("VIDEO")
        self.thumbnail.setFixedSize(116, 68)
        self.thumbnail.setAlignment(Qt.AlignCenter)
        self.thumbnail.setObjectName("taskThumbnail")

        self.title = QLabel()
        self.title.setWordWrap(True)
        self.title.setMinimumWidth(260)
        self.url = QLabel()
        self.url.setObjectName("mutedText")
        self.url.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.status = QLabel()
        self.status.setObjectName("taskStatus")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(True)
        self.progress.setFixedHeight(18)
        self.details = QLabel()
        self.details.setObjectName("mutedText")

        self.action = QPushButton()
        self.action.setFixedWidth(76)
        self.action.clicked.connect(self._action_clicked)

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
        layout.addLayout(text_layout, 1)
        layout.addWidget(self.status)
        layout.addWidget(self.action)
        self.update_task(task)

    def _action_clicked(self) -> None:
        status = getattr(self, "_status", "")
        if status in {"queued", "downloading", "canceling"}:
            self.cancel_requested.emit(self.task_id)
        elif status in {"failed", "canceled"}:
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
        self.title.setText(task.title or task.url)
        self.url.setText(task.url)
        self.progress.setValue(int(task.progress))
        self.status.setText(STATUS_TEXT.get(task.status, task.status))
        if task.status == "failed":
            self.status.setToolTip(task.error)
        details = "  ".join(x for x in [task.size, task.speed, f"剩余 {task.eta}" if task.eta else ""] if x)
        self.details.setText(details or "等待任务开始")
        if task.status in {"queued", "downloading", "canceling"}:
            self.action.setText("取消")
            self.action.setEnabled(task.status != "canceling")
        elif task.status in {"failed", "canceled"}:
            self.action.setText("重试")
            self.action.setEnabled(True)
        else:
            self.action.setText("打开文件夹")
            self.action.setEnabled(task.status == "completed")


class DashboardPage(QWidget):
    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.window = window
        self.cards: dict[str, DownloadTaskCard] = {}
        self.items: dict[str, QListWidgetItem] = {}

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

        input_row = QHBoxLayout()
        self.url = QLineEdit()
        self.url.setPlaceholderText("粘贴 YouTube 视频或播放列表链接，回车即可添加任务")
        self.url.returnPressed.connect(self.start)
        add_button = QPushButton("添加并下载")
        add_button.setObjectName("primaryButton")
        add_button.clicked.connect(self.start)
        input_row.addWidget(self.url, 1)
        input_row.addWidget(add_button)
        root.addLayout(input_row)

        options = QHBoxLayout()
        options.addWidget(QLabel("画质"))
        self.quality = QComboBox()
        self.quality.addItem("最高画质", "best")
        self.quality.addItem("1080p", "1080p")
        self.quality.addItem("720p", "720p")
        saved_quality = window.app_settings.get("quality")
        self.quality.setCurrentIndex(max(0, self.quality.findData(saved_quality)))
        self.quality.currentIndexChanged.connect(lambda: self._save("quality", self.quality.currentData()))
        options.addWidget(self.quality)
        options.addSpacing(12)
        options.addWidget(QLabel("保存到"))
        self.output = QLineEdit(window.app_settings.get("download_dir"))
        browse = QPushButton("选择目录")
        browse.clicked.connect(self.choose_dir)
        options.addWidget(self.output, 1)
        options.addWidget(browse)
        options.addSpacing(12)
        options.addWidget(QLabel("代理"))
        self.proxy = QLineEdit(window.app_settings.get("proxy"))
        self.proxy.setPlaceholderText("可选")
        self.proxy.setMaximumWidth(220)
        options.addWidget(self.proxy)
        root.addLayout(options)

        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("任务列表"))
        self.filter_box = QComboBox()
        self.filter_box.addItems(["全部", "下载中", "排队中", "已完成", "失败"])
        self.filter_box.currentTextChanged.connect(self.apply_filter)
        toolbar.addWidget(self.filter_box)
        toolbar.addStretch(1)
        open_dir = QPushButton("打开下载目录")
        open_dir.clicked.connect(self.open_download_dir)
        toolbar.addWidget(open_dir)
        root.addLayout(toolbar)

        self.task_list = QListWidget()
        self.task_list.setSpacing(8)
        self.task_list.setFrameShape(QFrame.NoFrame)
        self.task_list.setObjectName("taskList")
        root.addWidget(self.task_list, 1)
        self.empty_label = QLabel("还没有下载任务\n粘贴链接后点击“添加并下载”")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setObjectName("emptyState")
        root.addWidget(self.empty_label)
        self.status = QLabel("就绪")
        self.status.setObjectName("mutedText")
        root.addWidget(self.status)

    def _save(self, key: str, value: str) -> None:
        self.window.app_settings.set(key, str(value))
        self.window.app_settings.sync()

    def choose_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择保存目录", self.output.text())
        if path:
            self.output.setText(path)
            self._save("download_dir", path)

    def open_download_dir(self) -> None:
        path = Path(self.output.text().strip() or self.window.app_settings.get("download_dir"))
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(str(path))

    def start(self) -> None:
        url = self.url.text().strip()
        if not url:
            QMessageBox.warning(self, "提示", "请输入 YouTube URL")
            return
        output_dir = self.output.text().strip() or self.window.app_settings.get("download_dir")
        self._save("download_dir", output_dir)
        self._save("proxy", self.proxy.text().strip())
        self._save("quality", self.quality.currentData())
        self.window.download_service.enqueue(
            url,
            output_dir,
            self.proxy.text().strip(),
            quality=self.quality.currentData(),
            filename_template=self.window.app_settings.get("filename_template"),
            ffmpeg_path=self.window.app_settings.get("ffmpeg_path"),
        )
        self.url.clear()
        self.status.setText("任务已加入队列，可以继续添加其他链接")

    def add_task(self, task: DownloadTask) -> None:
        card = DownloadTaskCard(task)
        card.cancel_requested.connect(self.window.download_service.cancel)
        card.retry_requested.connect(self.window.download_service.retry)
        card.open_requested.connect(self.open_task_folder)
        item = QListWidgetItem()
        item.setSizeHint(QSize(0, 108))
        self.task_list.addItem(item)
        self.task_list.setItemWidget(item, card)
        self.cards[task.id] = card
        self.items[task.id] = item
        self._update_empty_state()
        self._update_count()

    def update_task(self, task: DownloadTask) -> None:
        card = self.cards.get(task.id)
        if card:
            card.update_task(task)
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

    def open_task_folder(self, task_id: str) -> None:
        task = self.window.download_service.tasks.get(task_id)
        if task:
            path = Path(task.media_path).parent if task.media_path else Path(task.output_dir)
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))

    def apply_filter(self, _value: str = "") -> None:
        selected = self.filter_box.currentText()
        allowed = {
            "全部": None,
            "下载中": {"downloading", "canceling"},
            "排队中": {"queued"},
            "已完成": {"completed"},
            "失败": {"failed", "canceled"},
        }[selected]
        for task_id, item in self.items.items():
            task = self.window.download_service.tasks.get(task_id)
            item.setHidden(bool(allowed and task and task.status not in allowed))

    def _update_empty_state(self) -> None:
        self.empty_label.setVisible(self.task_list.count() == 0)

    def _update_count(self) -> None:
        count = self.task_list.count()
        active = sum(1 for task in self.window.download_service.tasks.values() if task.status == "downloading")
        self.count_label.setText(f"{count} 个任务 · {active} 个进行中")


class BrowserPage(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        selector = QComboBox()
        selector.addItems(["空白页", "YouTube", "抖音", "Bilibili", "视频号", "快手", "今日头条"])
        layout.addWidget(selector)
        try:
            from PySide6.QtWebEngineWidgets import QWebEngineView
            from PySide6.QtCore import QUrl

            self.browser = QWebEngineView()
            layout.addWidget(self.browser)
            urls = {
                "空白页": "about:blank",
                "YouTube": "https://accounts.google.com/",
                "抖音": "https://creator.douyin.com/",
                "Bilibili": "https://member.bilibili.com/",
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
        for name in ["douyin", "bilibili", "tencent", "kuaishou", "toutiao"]:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.platforms.addItem(item)
        layout.addWidget(QLabel("平台"))
        layout.addWidget(self.platforms)
        button = QPushButton("保存并加入发布队列")
        button.clicked.connect(self.submit)
        layout.addWidget(button)

    def submit(self) -> None:
        platforms = [self.platforms.item(i).text() for i in range(self.platforms.count()) if self.platforms.item(i).checkState() == Qt.Checked]
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
        self.tree.setHeaderLabels(["ID", "平台", "状态", "标题", "结果"])
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
            self.tree.addTopLevelItem(QTreeWidgetItem([str(row["id"]), row["platform"], row["status"], row["title"], row["result"] or ""]))

    def run_selected(self) -> None:
        item = self.tree.currentItem()
        if item:
            self.window.publish_service.run_task(int(item.text(0)))
            self.refresh()


class SettingsPage(QWidget):
    def __init__(self, window: "MainWindow"):
        super().__init__()
        form = QFormLayout(self)
        bundled = bundled_ffmpeg_path()
        self.download_dir = QLineEdit(window.app_settings.get("download_dir"))
        choose = QPushButton("选择")
        choose.clicked.connect(self.choose_dir)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.download_dir)
        dir_row.addWidget(choose)
        self.proxy = QLineEdit(window.app_settings.get("proxy"))
        self.template = QLineEdit(window.app_settings.get("filename_template"))
        self.sau = QLineEdit(window.app_settings.get("sau_path"))
        self.ffmpeg = QLineEdit(window.app_settings.get("ffmpeg_path") or (str(bundled) if bundled.exists() else "ffmpeg"))
        save = QPushButton("保存配置")
        save.clicked.connect(lambda: window.save_settings(self))
        form.addRow("默认下载目录", dir_row)
        form.addRow("默认代理", self.proxy)
        form.addRow("文件名模板", self.template)
        form.addRow("sau 可执行文件", self.sau)
        form.addRow("FFmpeg", self.ffmpeg)
        form.addRow(save)

    def choose_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择默认下载目录", self.download_dir.text())
        if path:
            self.download_dir.setText(path)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("源流矩阵 · SourceFlow Studio")
        self.resize(1280, 820)
        self.setStyleSheet(
            """
            QWidget { font-family: "Microsoft YaHei"; font-size: 13px; }
            QLineEdit, QComboBox { min-height: 30px; border: 1px solid #d9dee7; border-radius: 6px; padding: 0 8px; background: white; }
            QPushButton { min-height: 30px; border: 1px solid #d9dee7; border-radius: 6px; padding: 0 12px; background: white; }
            QPushButton:hover { background: #f0f5ff; }
            QPushButton#primaryButton { color: white; background: #18a957; border: none; font-weight: 600; }
            QPushButton#primaryButton:hover { background: #128945; }
            QLabel#pageTitle { font-size: 22px; font-weight: 700; color: #172033; }
            QLabel#mutedText { color: #87909f; }
            QLabel#emptyState { color: #9aa3b2; padding: 40px; }
            QListWidget#taskList { background: #f6f8fb; border: none; }
            QFrame#taskCard { background: white; border: 1px solid #e3e8ef; border-radius: 10px; }
            QLabel#taskThumbnail { background: #e9eef5; color: #8090a6; border-radius: 6px; font-weight: 700; }
            QLabel#taskStatus { color: #3f6fca; min-width: 54px; }
            QProgressBar { border: none; background: #edf1f5; border-radius: 7px; text-align: center; color: #4d5968; }
            QProgressBar::chunk { background: #39b86a; border-radius: 7px; }
            QTabWidget::pane { border: none; }
            """
        )
        data_dir = Path.home() / ".youtube-release-studio"
        self.app_settings = AppSettings()
        self.db = Database(data_dir / "app.db")
        self.download_service = DownloadService(self.db)
        self.publish_service = PublishService(self.db)
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.dashboard = DashboardPage(self)
        self.browser_page = BrowserPage()
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
        self.download_service.task_media_completed.connect(self.dashboard.media_completed)
        self.download_service.task_finished.connect(self.dashboard.finished)
        self.publish_service.status.connect(lambda *_: self.publish_queue.refresh())

    def open_publish(self, media: MediaItem) -> None:
        page = PublishPage(self, media)
        self.tabs.addTab(page, "发布编辑")
        self.tabs.setCurrentWidget(page)

    def save_settings(self, page: SettingsPage) -> None:
        self.app_settings.set("download_dir", page.download_dir.text().strip())
        self.app_settings.set("proxy", page.proxy.text().strip())
        self.app_settings.set("filename_template", page.template.text().strip())
        self.app_settings.set("sau_path", page.sau.text().strip())
        self.app_settings.set("ffmpeg_path", page.ffmpeg.text().strip())
        self.app_settings.sync()
        self.dashboard.output.setText(self.app_settings.get("download_dir"))
        self.dashboard.proxy.setText(self.app_settings.get("proxy"))
        QMessageBox.information(self, "已保存", "下载目录、代理和工具路径已保存")
