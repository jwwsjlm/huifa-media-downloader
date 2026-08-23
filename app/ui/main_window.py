from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMainWindow, QMessageBox, QPushButton, QTabWidget, QTextEdit,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget, QProgressBar, QMenu,
)

from app.core.download_service import DownloadService, bundled_ffmpeg_path
from app.core.publish_service import PublishService
from app.core.app_settings import AppSettings
from app.storage.database import Database
from app.storage.models import MediaItem


class DashboardPage(QWidget):
    def __init__(self, window: "MainWindow"):
        super().__init__(); self.window = window
        form = QFormLayout(self)
        self.quality = QComboBox(); self.quality.addItem("最高画质", "best"); self.quality.addItem("1080p", "1080p"); self.quality.addItem("720p", "720p")
        saved_quality = window.app_settings.get("quality")
        index = max(0, self.quality.findData(saved_quality)); self.quality.setCurrentIndex(index)
        self.quality.currentIndexChanged.connect(lambda: window.app_settings.set("quality", self.quality.currentData()))
        self.url = QLineEdit(); self.url.setPlaceholderText("YouTube URL / playlist URL")
        self.output = QLineEdit(window.app_settings.get("download_dir")); browse = QPushButton("选择")
        browse.clicked.connect(self.choose_dir)
        row = QHBoxLayout(); row.addWidget(self.output); row.addWidget(browse)
        self.proxy = QLineEdit(window.app_settings.get("proxy")); self.proxy.setPlaceholderText("可选，例如 http://127.0.0.1:7890")
        self.progress = QProgressBar(); self.progress.setRange(0, 100)
        self.status = QLabel("就绪")
        start = QPushButton("开始下载"); start.clicked.connect(self.start)
        cancel = QPushButton("取消"); cancel.clicked.connect(window.download_service.cancel)
        form.addRow("画质", self.quality); form.addRow("URL", self.url); form.addRow("保存目录", row); form.addRow("代理", self.proxy)
        form.addRow(self.progress); form.addRow(self.status)
        buttons = QHBoxLayout(); buttons.addWidget(start); buttons.addWidget(cancel); form.addRow(buttons)

    def choose_dir(self):
        path = QFileDialog.getExistingDirectory(self, "选择保存目录", self.output.text())
        if path:
            self.output.setText(path)
            self.window.app_settings.set("download_dir", path)
            self.window.app_settings.sync()

    def start(self):
        if not self.url.text().strip():
            QMessageBox.warning(self, "提示", "请输入 YouTube URL")
            return
        self.progress.setValue(0); self.status.setText("正在启动...")
        self.window.app_settings.set("download_dir", self.output.text().strip())
        self.window.app_settings.set("proxy", self.proxy.text().strip())
        self.window.app_settings.set("quality", self.quality.currentData())
        self.window.app_settings.sync()
        self.window.download_service.start(
            self.url.text().strip(), self.output.text().strip(), self.proxy.text().strip(),
            quality=self.quality.currentData(),
            filename_template=self.window.app_settings.get("filename_template"),
            ffmpeg_path=self.window.app_settings.get("ffmpeg_path"),
        )


class BrowserPage(QWidget):
    def __init__(self):
        super().__init__(); layout = QVBoxLayout(self)
        selector = QComboBox(); selector.addItems(["YouTube", "抖音", "Bilibili", "视频号", "快手", "今日头条"])
        layout.addWidget(selector)
        try:
            from PySide6.QtWebEngineWidgets import QWebEngineView
            from PySide6.QtCore import QUrl
            self.browser = QWebEngineView(); layout.addWidget(self.browser)
            urls = {"YouTube": "https://accounts.google.com/", "抖音": "https://creator.douyin.com/",
                    "Bilibili": "https://member.bilibili.com/", "视频号": "https://channels.weixin.qq.com/",
                    "快手": "https://cp.kuaishou.com/", "今日头条": "https://mp.toutiao.com/"}
            selector.currentTextChanged.connect(lambda x: self.browser.setUrl(QUrl(urls[x])))
            self.browser.setUrl(QUrl(urls[selector.currentText()]))
        except ImportError:
            layout.addWidget(QLabel("QtWebEngine 未安装；请安装 PySide6 完整组件。"))


class CompletedPage(QWidget):
    def __init__(self, window: "MainWindow"):
        super().__init__(); self.window = window
        layout = QVBoxLayout(self); self.list = QListWidget(); self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self.menu); layout.addWidget(self.list)
        self.refresh()

    def refresh(self):
        self.list.clear()
        for media in self.window.db.list_media():
            item = QListWidgetItem(f"{media.title} | {media.uploader} | {media.video_path}"); item.setData(Qt.UserRole, media.id); self.list.addItem(item)

    def menu(self, pos):
        item = self.list.itemAt(pos)
        if not item: return
        menu = QMenu(self); publish = menu.addAction("创建发布任务"); open_folder = menu.addAction("打开文件夹")
        action = menu.exec(self.list.mapToGlobal(pos))
        media = self.window.db.get_media(item.data(Qt.UserRole))
        if not media: return
        if action == publish: self.window.open_publish(media)
        elif action == open_folder: os.startfile(str(Path(media.video_path).parent))


class PublishPage(QWidget):
    def __init__(self, window: "MainWindow", media: MediaItem):
        super().__init__(); self.window, self.media = window, media
        layout = QVBoxLayout(self); layout.addWidget(QLabel(f"媒体：{media.title}"))
        self.title = QLineEdit(media.title); self.description = QTextEdit(media.description)
        self.topics = QLineEdit(" ".join(f"#{x}" for x in media.tags))
        form = QFormLayout(); form.addRow("标题", self.title); form.addRow("内容", self.description); form.addRow("话题", self.topics); layout.addLayout(form)
        self.platforms = QListWidget()
        for name in ["douyin", "bilibili", "tencent", "kuaishou", "toutiao"]:
            it = QListWidgetItem(name); it.setFlags(it.flags() | Qt.ItemIsUserCheckable); it.setCheckState(Qt.Unchecked); self.platforms.addItem(it)
        layout.addWidget(QLabel("平台")); layout.addWidget(self.platforms)
        button = QPushButton("保存并加入发布队列"); button.clicked.connect(self.submit); layout.addWidget(button)

    def submit(self):
        platforms = [self.platforms.item(i).text() for i in range(self.platforms.count()) if self.platforms.item(i).checkState() == Qt.Checked]
        if not platforms: QMessageBox.warning(self, "提示", "至少选择一个平台"); return
        tags = [x.lstrip("#") for x in self.topics.text().split() if x.strip()]
        self.window.publish_service.create_tasks(self.media, platforms, {"title": self.title.text(), "description": self.description.toPlainText(), "tags": tags}, {})
        QMessageBox.information(self, "完成", "已加入发布队列"); self.window.tabs.setCurrentWidget(self.window.publish_queue)


class PublishQueuePage(QWidget):
    def __init__(self, window: "MainWindow"):
        super().__init__(); self.window = window; layout = QVBoxLayout(self); self.tree = QTreeWidget(); self.tree.setHeaderLabels(["ID", "平台", "状态", "标题", "结果"]); layout.addWidget(self.tree); refresh = QPushButton("刷新"); refresh.clicked.connect(self.refresh); run = QPushButton("执行选中任务"); run.clicked.connect(self.run_selected); layout.addWidget(refresh); layout.addWidget(run); self.refresh()
    def refresh(self):
        self.tree.clear()
        for row in self.window.db.list_publish_tasks(): self.tree.addTopLevelItem(QTreeWidgetItem([str(row["id"]), row["platform"], row["status"], row["title"], row["result"] or ""]))
    def run_selected(self):
        item = self.tree.currentItem()
        if item: self.window.publish_service.run_task(int(item.text(0))); self.refresh()


class SettingsPage(QWidget):
    def __init__(self, window: "MainWindow"):
        super().__init__(); form = QFormLayout(self)
        bundled = bundled_ffmpeg_path()
        self.download_dir = QLineEdit(window.app_settings.get("download_dir")); choose = QPushButton("选择")
        choose.clicked.connect(lambda: self.choose_dir())
        dir_row = QHBoxLayout(); dir_row.addWidget(self.download_dir); dir_row.addWidget(choose)
        self.proxy = QLineEdit(window.app_settings.get("proxy"))
        self.template = QLineEdit(window.app_settings.get("filename_template"))
        self.sau = QLineEdit(window.app_settings.get("sau_path"))
        self.ffmpeg = QLineEdit(window.app_settings.get("ffmpeg_path") or (str(bundled) if bundled.exists() else "ffmpeg"))
        save = QPushButton("保存配置"); save.clicked.connect(lambda: window.save_settings(self))
        form.addRow("默认下载目录", dir_row); form.addRow("默认代理", self.proxy); form.addRow("文件名模板", self.template)
        form.addRow("sau 可执行文件", self.sau); form.addRow("FFmpeg", self.ffmpeg); form.addRow(save)

    def choose_dir(self):
        path = QFileDialog.getExistingDirectory(self, "选择默认下载目录", self.download_dir.text())
        if path: self.download_dir.setText(path)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__(); self.setWindowTitle("YouTube Release Studio"); self.resize(1200, 800)
        data_dir = Path.home() / ".youtube-release-studio"; self.app_settings = AppSettings(); self.download_dir = Path(self.app_settings.get("download_dir")); self.db = Database(data_dir / "app.db")
        self.download_service = DownloadService(self.db); self.publish_service = PublishService(self.db)
        self.tabs = QTabWidget(); self.setCentralWidget(self.tabs)
        self.dashboard = DashboardPage(self); self.browser_page = BrowserPage(); self.completed = CompletedPage(self); self.publish_queue = PublishQueuePage(self); self.settings = SettingsPage(self)
        for name, page in [("下载", self.dashboard), ("浏览器/账号", self.browser_page), ("完成列表", self.completed), ("发布队列", self.publish_queue), ("设置", self.settings)]: self.tabs.addTab(page, name)
        self.download_service.progress.connect(self.on_progress); self.download_service.completed.connect(lambda _: self.completed.refresh()); self.download_service.failed.connect(lambda e: QMessageBox.critical(self, "下载失败", e)); self.publish_service.status.connect(lambda *_: self.publish_queue.refresh())
    def on_progress(self, data):
        total, done = data.get("total_bytes") or data.get("total_bytes_estimate") or 0, data.get("downloaded_bytes") or 0
        if total: self.dashboard.progress.setValue(int(done * 100 / total))
        self.dashboard.status.setText(f"{data.get('status','')} {data.get('_percent_str','')} {data.get('_speed_str','')} ETA {data.get('_eta_str','')}")
    def open_publish(self, media):
        page = PublishPage(self, media); self.tabs.addTab(page, "发布编辑"); self.tabs.setCurrentWidget(page)
    def save_settings(self, page: SettingsPage):
        self.app_settings.set("download_dir", page.download_dir.text().strip())
        self.app_settings.set("proxy", page.proxy.text().strip())
        self.app_settings.set("filename_template", page.template.text().strip())
        self.app_settings.set("sau_path", page.sau.text().strip())
        self.app_settings.set("ffmpeg_path", page.ffmpeg.text().strip())
        self.app_settings.sync()
        self.download_dir = Path(self.app_settings.get("download_dir"))
        self.dashboard.output.setText(str(self.download_dir)); self.dashboard.proxy.setText(self.app_settings.get("proxy"))
        QMessageBox.information(self, "已保存", "下载目录、画质、代理和工具路径已保存")
