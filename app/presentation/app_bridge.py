from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Property, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QGuiApplication

from app.core.download_links import extract_download_links
from app.core.download_options import DownloadOptions
from app.core.download_progress import format_speed
from app.core.cookie_sources import (
    COOKIE_SOURCE_BROWSER,
    COOKIE_SOURCE_EMBEDDED,
    COOKIE_SOURCE_FILE,
    COOKIE_SOURCE_NONE,
    EMBEDDED_DOWNLOAD_PROFILE,
    browser_cookie_count,
    normalize_cookie_source,
)
from app.core.paths import resolve_portable_path
from app.core.download_submission import (
    DownloadSubmissionDebouncer,
    DownloadSubmissionSettingsError,
    build_download_request_context,
    service_task_arguments,
    submission_playlist_mode,
)
from app.core.download_service import DownloadTask
from app.core.diagnostics import collect_diagnostic_summary, normalized_diagnostics_target
from app.core.language_packs import language_pack_directory
from app.presentation.collection_entry_model import CollectionEntryModel
from app.presentation.collection_workflow import QmlCollectionWorkflow
from app.presentation.cover_studio_model import CoverStudioModel
from app.presentation.completed_media_model import CompletedMediaModel
from app.presentation.database_lifecycle import QmlDatabaseLifecycle
from app.presentation.desktop_notification_controller import (
    QmlDesktopNotificationController,
)
from app.presentation.account_model import AccountModel
from app.presentation.format_choice_model import FormatChoiceModel
from app.presentation.publish_queue_model import PublishQueueModel
from app.presentation.publish_draft import PublishDraft
from app.presentation.task_list_model import TaskListModel
from app.presentation.settings_model import SettingsModel
from app.presentation.qml_shutdown_controller import QmlShutdownController
from app.presentation.supported_site_model import SupportedSiteModel
from app.presentation.update_model import UpdateModel
from app.core.version import APP_NAME, APP_VERSION
from app.presentation.task_auth_actions import TaskAuthActionController


class AppBridge(QObject):
    """The only QML Interface over the private application runtime.

    Slots are user commands; QObject properties and list models are display
    state. QML never receives the runtime, database, worker or tool adapters.
    """

    messageChanged = Signal()
    summaryChanged = Signal()
    collectionSelectionRequested = Signal(str, str)
    collectionDetailsRequested = Signal(str, str)
    formatSelectionRequested = Signal()
    navigationRequested = Signal(int)
    publishEditorRequested = Signal()
    publishEditorCompleted = Signal()
    coverStudioRequested = Signal()
    shutdownChanged = Signal()
    shutdownCompleted = Signal()
    localHistoryImportRequested = Signal(str, int)
    databaseRecoveryRequested = Signal(str, str, str)
    windowActivationRequested = Signal()
    taskFocusRequested = Signal(str)
    publishTaskFocusRequested = Signal(int)

    _MAX_BATCH_DOWNLOAD_LINKS = 100

    def __init__(self, runtime: Any, parent: QObject | None = None, *, desktop_notifications_factory=QmlDesktopNotificationController) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._task_model = TaskListModel(runtime.download_service, self)
        self._account_model = AccountModel(
            runtime.app_settings,
            runtime.publish_service,
            self,
        )
        self._collection_model = CollectionEntryModel(
            runtime.db,
            runtime.download_service,
            self,
        )
        self._completed_media_model = CompletedMediaModel(
            runtime.db,
            runtime.app_settings,
            self,
        )
        self._publish_queue_model = PublishQueueModel(runtime.db, self)
        self._publish_draft = PublishDraft(
            runtime.db,
            runtime.app_settings,
            runtime.publish_service,
            self,
        )
        self._settings_model = SettingsModel(runtime, self)
        self._supported_site_model = SupportedSiteModel(self)
        self._format_model = FormatChoiceModel(self)
        self._collection_workflow = QmlCollectionWorkflow(runtime, self)
        self._cover_studio = CoverStudioModel(runtime, self._settings_model, self)
        self._task_auth_actions = TaskAuthActionController(
            runtime.download_service,
            runtime.app_settings,
            self.resumeCollections,
        )
        self._update_model = UpdateModel(runtime, self)
        self._database_lifecycle = QmlDatabaseLifecycle(
            runtime,
            self._refresh_after_database_reset,
            extra_background_users_active=lambda: self._collection_workflow.running,
            parent=self,
        )
        self._desktop_notifications = desktop_notifications_factory(
            runtime,
            self.requestShutdown,
            self,
        )
        self._shutdown_controller = QmlShutdownController(
            runtime,
            self._collection_workflow,
            self._set_message,
            persistence_available=lambda: self._database_lifecycle.persistenceAvailable,
            cover_studio=self._cover_studio,
            parent=self,
        )
        self._startup_restored = False
        self._local_history_folder = ""
        self._local_history_candidates: list[Path] = []
        self._submission_debouncer = DownloadSubmissionDebouncer()
        self._message = ""
        self._summary = ""
        self._summary_timer = QTimer(self)
        self._summary_timer.setInterval(500)
        self._summary_timer.timeout.connect(self._refresh_summary)
        self._summary_timer.start()
        self._collection_refresh_timer = QTimer(self)
        self._collection_refresh_timer.setSingleShot(True)
        self._collection_refresh_timer.setInterval(180)
        self._collection_refresh_timer.timeout.connect(self._refresh_collection_entries)
        self._completed_media_refresh_timer = QTimer(self)
        self._completed_media_refresh_timer.setSingleShot(True)
        self._completed_media_refresh_timer.setInterval(180)
        self._completed_media_refresh_timer.timeout.connect(
            self._refresh_completed_media_if_loaded
        )
        for signal in (
            runtime.download_service.task_added,
            runtime.download_service.tasks_added,
            runtime.download_service.task_updated,
            runtime.download_service.task_deleted,
        ):
            signal.connect(self._refresh_summary)
        runtime.download_service.conversion_failed.connect(self._conversion_failed)
        runtime.download_service.task_finished.connect(
            self._desktop_notifications.downloadFinished,
            Qt.QueuedConnection,
        )
        runtime.download_service.formats_ready.connect(
            self._formats_ready,
            Qt.QueuedConnection,
        )
        runtime.download_service.task_media_completed.connect(
            self._completed_media_changed,
            Qt.QueuedConnection,
        )
        runtime.publish_service.status.connect(
            self._publish_status_changed,
            Qt.QueuedConnection,
        )
        runtime.publish_service.status.connect(
            self._desktop_notifications.publishFinished,
            Qt.QueuedConnection,
        )
        runtime.publish_service.account_status.connect(
            self._download_cookie_login_result,
            Qt.QueuedConnection,
        )
        self._collection_workflow.statusChanged.connect(self._set_message)
        self._account_model.message.connect(self._set_message)
        self._account_model.saved.connect(self._refresh_completed_media_if_loaded)
        self._settings_model.message.connect(self._set_message)
        self._settings_model.downloadDirectorySaved.connect(self.scanLocalHistory)
        self._settings_model.changed.connect(self._desktop_notifications.syncVisibility)
        self._settings_model.changed.connect(
            self._completed_media_model.refreshDistributionPlan
        )
        self._update_model.message.connect(self._set_message)
        self._update_model.restartRequested.connect(self.requestShutdown)
        self._database_lifecycle.message.connect(self._set_message)
        self._database_lifecycle.recoveryNotice.connect(
            self._database_recovery_notice
        )
        self._desktop_notifications.activateRequested.connect(
            self.windowActivationRequested
        )
        self._desktop_notifications.downloadRequested.connect(
            self.taskFocusRequested
        )
        self._desktop_notifications.publishRequested.connect(
            self.publishTaskFocusRequested
        )
        self._shutdown_controller.stateChanged.connect(self.shutdownChanged)
        self._shutdown_controller.finished.connect(self._shutdown_completed)
        self._collection_workflow.selectionRequested.connect(
            self._collection_selection_requested
        )
        self._collection_workflow.entriesChanged.connect(self._collection_entries_changed)
        self._refresh_summary()

    @Property(QObject, constant=True)
    def taskModel(self) -> QObject:
        return self._task_model

    @Property(QObject, constant=True)
    def accountModel(self) -> QObject:
        return self._account_model

    @Property(QObject, constant=True)
    def collectionModel(self) -> QObject:
        return self._collection_model

    @Property(QObject, constant=True)
    def formatModel(self) -> QObject:
        return self._format_model

    @Property(QObject, constant=True)
    def completedMediaModel(self) -> QObject:
        return self._completed_media_model

    @Property(QObject, constant=True)
    def publishQueueModel(self) -> QObject:
        return self._publish_queue_model

    @Property(QObject, constant=True)
    def publishDraft(self) -> QObject:
        return self._publish_draft

    @Property(QObject, constant=True)
    def settingsModel(self) -> QObject:
        return self._settings_model

    @Property(QObject, constant=True)
    def coverStudio(self) -> QObject:
        return self._cover_studio

    @Property(QObject, constant=True)
    def supportedSiteModel(self) -> QObject:
        return self._supported_site_model

    @Property(QObject, constant=True)
    def updateModel(self) -> QObject:
        return self._update_model

    @Property(bool, notify=shutdownChanged)
    def shutdownReady(self) -> bool:
        return self._shutdown_controller.complete

    @Property(bool, notify=shutdownChanged)
    def shutdownStarted(self) -> bool:
        return self._shutdown_controller.started

    @Property(str, constant=True)
    def appName(self) -> str:
        return APP_NAME

    @Property(str, constant=True)
    def appVersion(self) -> str:
        return APP_VERSION

    @Property(str, notify=messageChanged)
    def message(self) -> str:
        return self._message

    @Property(str, notify=summaryChanged)
    def statusSummary(self) -> str:
        return self._summary

    @Slot(str)
    def startTask(self, task_id: str) -> None:
        self._task_auth_actions.start(str(task_id or ""))

    @Slot(str)
    def pauseTask(self, task_id: str) -> None:
        self._runtime.download_service.pause(str(task_id or ""))

    @Slot(str)
    def cancelTask(self, task_id: str) -> None:
        task = self._task(task_id)
        if (
            task is not None
            and task.task_kind == "collection"
            and task.status in {"parsing_collection", "waiting_selection"}
        ):
            if self._collection_workflow.cancel_selection(task.id):
                self._set_message("已取消合集解析")
            return
        self._runtime.download_service.cancel(str(task_id or ""))

    @Slot(str)
    def retryTask(self, task_id: str) -> None:
        self._task_auth_actions.retry(str(task_id or ""))

    @Slot(str)
    def redownloadTask(self, task_id: str) -> None:
        task = self._task(task_id)
        if task is None:
            return
        options = task.options_json
        if bool(options.get("_local_history") if hasattr(options, "get") else False):
            self._set_message("本地历史媒体没有原始链接，无法重新下载")
            return
        new_task_id = self._task_auth_actions.redownload(task.id)
        self._set_message(
            "已创建或定位到重新下载任务"
            if new_task_id
            else "无法创建重新下载任务"
        )

    @Slot(str, bool, result=bool)
    def deleteTask(self, task_id: str, delete_files: bool = False) -> bool:
        task = self._task(task_id)
        if task is None:
            self._set_message("任务不存在或已删除")
            return False
        try:
            if (
                task.task_kind == "collection"
                and task.status in {"parsing_collection", "waiting_selection"}
            ):
                deleted = self._collection_workflow.cancel_selection(task.id)
            else:
                deleted = self._runtime.download_service.delete_task(
                    task.id,
                    bool(delete_files),
                )
        except Exception as exc:
            self._set_message(f"无法删除任务：{exc}")
            return False
        if not deleted:
            self._set_message("任务不存在或已删除")
            return False
        self._set_message("已请求删除任务")
        return True

    @Slot(str)
    def copyTaskSource(self, task_id: str) -> None:
        task = self._task(task_id)
        if task is None:
            return
        options = task.options_json
        local_history = bool(
            options.get("_local_history") if hasattr(options, "get") else False
        )
        self._copy_text(
            task.media_path if local_history else task.url,
            "已复制本地文件路径" if local_history else "已复制视频链接",
        )

    @Slot(str)
    def copyTaskFolder(self, task_id: str) -> None:
        task = self._task(task_id)
        if task is None:
            return
        folder = (
            Path(task.media_path).parent
            if str(task.media_path or "").strip()
            else Path(task.output_dir)
        )
        self._copy_text(str(folder), "已复制任务文件夹路径")

    @Slot(str, result=str)
    def taskLogText(self, task_id: str) -> str:
        task = self._task(task_id)
        if task is None:
            return "任务不存在或已删除。"
        try:
            return self._runtime.download_service.logs.render(task.id)
        except Exception as exc:
            return f"无法读取任务日志：{exc}"

    @Slot(str)
    def clearTaskLog(self, task_id: str) -> None:
        task = self._task(task_id)
        if task is None:
            return
        try:
            self._runtime.download_service.logs.clear(task.id)
        except Exception as exc:
            self._set_message(f"无法清除任务日志：{exc}")
            return
        self._set_message("已清除任务日志")

    @Slot(str)
    def openTaskLogLocation(self, task_id: str) -> None:
        try:
            folder = self._runtime.download_service.logs.path_for(
                str(task_id or "")
            ).parent
        except Exception as exc:
            self._set_message(f"无法定位任务日志：{exc}")
            return
        self._open_local_directory(folder, "任务日志目录")

    @Slot()
    def openDownloadDirectory(self) -> None:
        self._open_local_directory(
            resolve_portable_path(self._runtime.app_settings.get("download_dir")),
            "下载目录",
        )

    @Slot()
    def pauseAllTasks(self) -> None:
        task_ids = [
            task.id
            for task in self._runtime.download_service.tasks.values()
            if not task.parent_task_id and task.status in {"downloading", "queued", "processing"}
        ]
        for task_id in task_ids:
            self._runtime.download_service.pause(task_id)
        self._set_message(
            f"已请求暂停 {len(task_ids)} 个任务"
            if task_ids else "没有可暂停的任务"
        )

    @Slot()
    def resumeAllTasks(self) -> None:
        task_ids = [
            task.id
            for task in self._runtime.download_service.tasks.values()
            if not task.parent_task_id
            and task.status in {"paused", "failed", "partial_failed", "processing_failed", "canceled"}
        ]
        for task_id in task_ids:
            self._task_auth_actions.start(task_id)
        self._set_message(
            f"已请求继续 {len(task_ids)} 个任务"
            if task_ids else "没有可继续的任务"
        )

    @Slot()
    def cleanupCompletedTasks(self) -> None:
        task_ids = [
            task.id
            for task in self._runtime.download_service.tasks.values()
            if not task.parent_task_id and task.status == "completed"
        ]
        for task_id in task_ids:
            self._runtime.download_service.delete_task(task_id, delete_files=False)
        self._set_message(
            f"已清理 {len(task_ids)} 个完成任务记录"
            if task_ids else "没有已完成任务可清理"
        )

    @Slot(str)
    def openTaskLocation(self, task_id: str) -> None:
        task = self._task(task_id)
        if task is None:
            return
        target = Path(task.media_path) if task.media_path else None
        try:
            if target is not None and target.is_file() and os.name == "nt":
                subprocess.Popen(["explorer.exe", "/select,", str(target)])
                return
            folder = target.parent if target is not None and target.is_file() else Path(task.output_dir)
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))
        except OSError as exc:
            self._set_message(f"无法打开文件夹：{exc}")

    @Slot()
    def refreshCompletedMedia(self) -> None:
        self._completed_media_refresh_timer.stop()
        self._completed_media_model.markDirty()
        self._completed_media_model.refresh()

    @Slot()
    def refreshPublishQueue(self) -> None:
        self._publish_queue_model.refresh()

    @Slot(int)
    def showPublishQueueForMedia(self, media_id: int) -> None:
        self._publish_queue_model.setMediaId(int(media_id or 0))
        self.navigationRequested.emit(3)

    @Slot(int)
    def runPublishTask(self, task_id: int) -> None:
        self._runtime.publish_service.run_task(int(task_id or 0))

    @Slot(int)
    def retryPublishTask(self, task_id: int) -> None:
        self._runtime.publish_service.retry_task(int(task_id or 0))

    @Slot(int)
    def openPublishEditor(self, media_id: int) -> None:
        if self._publish_draft.openMedia(int(media_id or 0)):
            self.publishEditorRequested.emit()
        else:
            self._set_message(self._publish_draft.lastMessage)

    @Slot()
    def submitPublishDraft(self) -> None:
        if not self._publish_draft.submit():
            self._set_message(self._publish_draft.lastMessage)
            return
        self._account_model.reloadSettings()
        self._refresh_completed_media_if_loaded()
        if self._publish_queue_model.loaded:
            self._publish_queue_model.refresh()
        else:
            self._publish_queue_model.markDirty()
        self._set_message(self._publish_draft.lastMessage)
        self.publishEditorCompleted.emit()
        self.navigationRequested.emit(3)

    @Slot(int)
    def openMediaLocation(self, media_id: int) -> None:
        media = self._completed_media_model.media(media_id) or self._runtime.db.get_media(
            int(media_id or 0)
        )
        if media is None or not str(media.video_path or "").strip():
            self._set_message("没有可打开的视频文件")
            return
        try:
            os.startfile(str(Path(media.video_path).parent))
        except OSError as exc:
            self._set_message(f"无法打开文件夹：{exc}")

    @Slot(int)
    def openCoverStudio(self, media_id: int) -> None:
        if self._cover_studio.openMedia(int(media_id or 0)):
            self.coverStudioRequested.emit()
        else:
            self._set_message(self._cover_studio.status)

    @Slot(str, str)
    def convertTask(self, task_id: str, encoder: str) -> None:
        task = self._task(task_id)
        if task is None or task.task_kind != "video":
            self._set_message("视频任务不存在或已删除")
            return
        # Download-time "保持原格式" must not turn the explicit manual
        # conversion action into a silent no-op.  libx264 is present in the
        # bundled compatibility FFmpeg and converts WebM/other sources to the
        # task's MP4-compatible output when no encoder was selected.
        selected_encoder = str(encoder or "").strip()
        if selected_encoder.casefold() in {"", "original"}:
            selected_encoder = "libx264"
        started = self._runtime.download_service.convert_completed_task(
            str(task_id or ""),
            selected_encoder,
            ffmpeg_path=self._runtime.app_settings.get("ffmpeg_path"),
            ffprobe_path=self._runtime.app_settings.get("ffprobe_path"),
        )
        if started:
            self._set_message("已开始格式转换")

    @Slot(str)
    def showCollectionEntries(self, task_id: str) -> None:
        self._collection_model.setParentTaskId(str(task_id or ""))

    @Slot(str)
    def openCollectionSelection(self, task_id: str) -> None:
        task = self._task(task_id)
        if task is None or task.task_kind != "collection":
            return
        self._collection_selection_requested(task.id, task.title)

    @Slot(str)
    def openCollectionDetails(self, task_id: str) -> None:
        task = self._task(task_id)
        if task is None or task.task_kind != "collection":
            return
        self._collection_model.setParentTaskId(task.id)
        self.collectionDetailsRequested.emit(task.id, task.title)

    @Slot(str)
    def openFormatSelection(self, task_id: str) -> None:
        if self._format_model.taskId != str(task_id or ""):
            self._set_message("该格式选择已过期，请重新添加链接")
            return
        self.formatSelectionRequested.emit()

    @Slot(int)
    def confirmFormatSelection(self, choice_index: int) -> None:
        task_id = self._format_model.taskId
        choice = self._format_model.selected_choice(choice_index)
        if not task_id or choice is None:
            self._set_message("请选择一个可用格式")
            return
        try:
            applied = self._runtime.download_service.set_format_selection(task_id, choice)
        except Exception as exc:
            self._set_message(f"无法应用格式选择：{exc}")
            return
        if not applied:
            self._set_message("该格式选择已过期，请重新添加链接")
            return
        self._format_model.clear()
        self._set_message("已选择格式，开始下载")

    @Slot()
    def cancelFormatSelection(self) -> None:
        task_id = self._format_model.taskId
        if task_id:
            try:
                self._runtime.download_service.discard_task(task_id)
            except Exception as exc:
                self._set_message(f"无法取消格式选择：{exc}")
                return
        self._format_model.clear()
        self._set_message("已取消格式选择，未创建下载任务")

    @Slot(str, result=bool)
    def confirmCollectionSelection(self, task_id: str) -> bool:
        return self._collection_workflow.confirm_selection(str(task_id or ""))

    @Slot(str, result=bool)
    def cancelCollectionSelection(self, task_id: str) -> bool:
        cancelled = self._collection_workflow.cancel_selection(str(task_id or ""))
        if cancelled:
            self._set_message("已取消合集解析")
        return cancelled

    @Slot()
    def resumeCollections(self) -> None:
        self._collection_workflow.resume()

    @Slot()
    def restoreStartupState(self) -> None:
        if self._startup_restored:
            return
        self._startup_restored = True
        restored: list[DownloadTask] = []
        restore_failed = False
        try:
            self._database_lifecycle.start()
            self._desktop_notifications.setup()
            restored = self._runtime.download_service.restore_tasks()
        except Exception as exc:
            # Surface recovery failures instead of letting them escape the slot.
            restore_failed = True
            self._set_message(f"启动任务恢复失败：{exc}")
        if restored:
            # The model is created before startup restoration. Deferred child
            # rows arrive through tasks_added; synchronous top-level rows need
            # one authoritative refresh after the service has registered them.
            self._task_model.refresh()
            self._refresh_summary()
        if not restore_failed:
            QTimer.singleShot(0, self.resumeCollections)
        self._update_model.initialize()

    @Slot()
    def requestShutdown(self) -> None:
        self._summary_timer.stop()
        self._collection_refresh_timer.stop()
        self._completed_media_refresh_timer.stop()
        self._database_lifecycle.requestShutdown()
        self._desktop_notifications.requestShutdown()
        self._update_model.requestShutdown()
        self._shutdown_controller.begin()

    def finish_shutdown_blocking(self) -> bool:
        """Finish the same shutdown flow after the QML event loop is gone."""
        self.requestShutdown()
        return self._shutdown_controller.finish_blocking()

    @Slot()
    def checkDownloadCookies(self) -> None:
        settings = self._runtime.app_settings
        source = normalize_cookie_source(settings.get("download_cookie_source"))
        if source == COOKIE_SOURCE_NONE:
            self._set_message("下载不会发送 Cookie")
            return
        if source == COOKIE_SOURCE_FILE:
            path = resolve_portable_path(settings.get("download_cookie_file"))
            self._set_message(
                "Cookie 文件可用，内容不会写入日志"
                if path.is_file()
                else "请选择有效的 Netscape Cookie 文件"
            )
            return
        if source == COOKIE_SOURCE_EMBEDDED:
            try:
                from app.core.browser_cookies import CookieVault

                count = CookieVault().count(EMBEDDED_DOWNLOAD_PROFILE)
            except Exception as exc:
                self._set_message(f"无法读取内置 Cookie：{exc}")
                return
            self._set_message(
                f"内置浏览器已有 {count} 个加密 Cookie"
                if count > 0
                else "内置浏览器尚未登录，请先打开登录页"
            )
            return
        try:
            count = browser_cookie_count(
                settings.get("download_cookie_browser"),
                settings.get("download_cookie_profile"),
                settings.get("download_cookie_keyring"),
                settings.get("download_cookie_container"),
            )
        except Exception as exc:
            self._set_message(f"无法读取浏览器 Cookie：{exc}")
            return
        self._set_message(f"已读取 {count} 个浏览器 Cookie，内容不会显示或保存")

    @Slot()
    def openDownloadCookieLogin(self) -> None:
        try:
            started = self._runtime.publish_service.run_account_action(
                "browser",
                "download",
                "login",
                vault_profile_id=EMBEDDED_DOWNLOAD_PROFILE,
            )
        except Exception as exc:
            self._set_message(f"无法打开内置登录页：{exc}")
            return
        self._set_message(
            "正在打开应用内置浏览器登录页…"
            if started
            else "登录操作已在进行，或发布服务正在关闭"
        )

    @Slot(str, bool, result="QVariantList")
    def embeddedCookies(self, query: str, include_values: bool) -> list[dict[str, str]]:
        try:
            from app.core.browser_cookies import CookieVault, deduplicate_cookies

            cookies = deduplicate_cookies(CookieVault().load(EMBEDDED_DOWNLOAD_PROFILE))
        except Exception as exc:
            self._set_message(f"无法读取内置 Cookie：{exc}")
            return []
        needle = str(query or "").strip().casefold()
        entries = [
            {
                "domain": str(cookie.domain or ""),
                "name": str(cookie.name or ""),
                "value": str(cookie.value or "") if include_values else "••••••••",
                "path": str(cookie.path or "/"),
                "expires": self._cookie_expiry_text(cookie.expires),
                "secure": "是" if cookie.secure else "否",
                "httpOnly": "是" if cookie.http_only else "否",
                "sameSite": str(cookie.same_site or "—"),
            }
            for cookie in cookies
            if not needle
            or needle in str(cookie.domain or "").casefold()
            or needle in str(cookie.name or "").casefold()
            or needle in str(cookie.path or "").casefold()
        ]
        return sorted(
            entries,
            key=lambda entry: (
                entry["domain"].lstrip(".").casefold(),
                entry["name"].casefold(),
                entry["path"],
            ),
        )

    @Slot()
    def openLogDirectory(self) -> None:
        self._open_local_directory(
            self._runtime.download_service.logs.root,
            "日志目录",
        )

    @Slot()
    def openLanguagePackDirectory(self) -> None:
        self._open_local_directory(language_pack_directory(), "语言包目录")

    @Slot(result=str)
    def diagnosticsDefaultPath(self) -> str:
        return str(self._runtime.download_service.logs.root.parent / "diagnostics.zip")

    @Slot(str, result=str)
    def exportDiagnostics(self, target: str) -> str:
        requested = str(target or "").strip() or self.diagnosticsDefaultPath()
        try:
            destination = normalized_diagnostics_target(requested)
            destination.parent.mkdir(parents=True, exist_ok=True)
            summary = collect_diagnostic_summary(
                self._runtime.app_settings,
                lambda: self._runtime.db,
            )
            bundle = self._runtime.download_service.logs.export_bundle(
                destination,
                summary,
            )
        except Exception as exc:
            self._set_message(f"无法导出诊断包：{exc}")
            return ""
        self._set_message(f"诊断包已导出：{bundle}")
        return str(bundle)

    @Slot(str)
    def openDiagnosticsLocation(self, target: str) -> None:
        path = Path(str(target or "").strip() or self.diagnosticsDefaultPath())
        self._open_local_directory(path.parent, "诊断包目录")

    @Slot(str, result=int)
    def scanLocalHistory(self, folder: str) -> int:
        target = resolve_portable_path(
            str(folder or self._runtime.app_settings.get("download_dir") or "")
        )
        try:
            candidates = self._runtime.download_service.scan_local_history(target)
        except Exception as exc:
            self._set_message(f"扫描本地历史失败：{exc}")
            return 0
        self._local_history_folder = str(target)
        self._local_history_candidates = list(candidates)
        if not candidates:
            self._set_message("当前下载目录没有可导入的遗留媒体")
            return 0
        self.localHistoryImportRequested.emit(self._local_history_folder, len(candidates))
        return len(candidates)

    @Slot(result=int)
    def importScannedLocalHistory(self) -> int:
        if not self._local_history_candidates or not self._local_history_folder:
            return 0
        try:
            imported = self._runtime.download_service.import_local_history(
                self._local_history_candidates,
                self._local_history_folder,
            )
        except Exception as exc:
            self._set_message(f"导入本地历史失败：{exc}")
            return 0
        self._local_history_candidates = []
        count = len(imported)
        if count:
            self._refresh_completed_media_if_loaded()
            self._set_message(f"已导入 {count} 个本地媒体到任务历史")
        else:
            self._set_message("没有可导入的本地媒体")
        return count

    @Slot(str, result=bool)
    def submitUrls(self, raw_text: str) -> bool:
        text = str(raw_text or "")
        links = extract_download_links(text)
        if not links:
            if not text.strip() and self._submission_debouncer.suppresses_empty_followup():
                return False
            self._set_message("请输入有效的视频或合集链接")
            return False
        if len(links) > self._MAX_BATCH_DOWNLOAD_LINKS:
            self._set_message(f"一次最多添加 {self._MAX_BATCH_DOWNLOAD_LINKS} 个链接")
            return False
        try:
            context = build_download_request_context(
                self._runtime.app_settings,
                options_json=self._download_options(),
            )
        except DownloadSubmissionSettingsError as exc:
            self._set_message(
                "请先设置下载目录" if exc.code == "missing_download_dir" else "代理地址无效"
            )
            return False
        if self._submission_debouncer.rejects(links):
            self._set_message("刚刚已提交过这组链接，已忽略重复点击")
            return False

        advanced = DownloadOptions.from_mapping(context.get("options_json"))
        playlist_mode = submission_playlist_mode(
            context,
            collection_mode=advanced.collection_mode,
        )
        created = 0
        skipped = 0
        for url in links:
            duplicate_id = self._runtime.download_service.find_active_duplicate(
                url,
                str(context["output_dir"]),
                str(context["quality"]),
                playlist_mode,
                transcode_codec=str(context.get("transcode_codec") or "original"),
                transcode_device=str(context.get("transcode_device") or "auto"),
                transcode_encoder=str(context.get("transcode_encoder") or "original"),
                subtitle_language=str(context.get("subtitle_language") or "none"),
            )
            if duplicate_id:
                skipped += 1
                continue
            try:
                if playlist_mode == "single":
                    self._runtime.download_service.enqueue(
                        url,
                        str(context["output_dir"]),
                        **service_task_arguments(context, playlist_mode=playlist_mode),
                    )
                elif not self._collection_workflow.start(url, context):
                    raise RuntimeError("合集解析服务正在关闭")
            except Exception as exc:
                self._set_message(f"添加任务失败：{exc}")
                return bool(created)
            created += 1
        if created:
            self._set_message(
                f"已添加 {created} 个任务"
                + (f"，跳过 {skipped} 个重复任务" if skipped else "")
            )
            return True
        self._set_message(f"未添加新任务：{skipped} 个链接已有未完成任务")
        return False

    def _task(self, task_id: str) -> DownloadTask | None:
        return self._runtime.download_service.tasks.get(str(task_id or ""))

    def _download_options(self) -> dict[str, object]:
        try:
            stored = json.loads(
                self._runtime.app_settings.get("download_options_json") or "{}"
            )
        except (TypeError, json.JSONDecodeError):
            stored = {}
        options = dict(stored) if isinstance(stored, dict) else {}
        options["processing_temp_dir"] = self._runtime.app_settings.get(
            "processing_temp_dir"
        )
        return DownloadOptions.from_mapping(options).to_dict()

    @Slot(str, str)
    def _collection_selection_requested(self, task_id: str, title: str) -> None:
        self._collection_model.setParentTaskId(task_id)
        self.collectionSelectionRequested.emit(task_id, title)

    @Slot(str, object)
    def _formats_ready(self, task_id: str, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        if not self._format_model.set_payload(task_id, payload):
            self._runtime.download_service.set_format_selector(task_id, "")
            self._set_message("没有可用的下载格式")
            return
        self.formatSelectionRequested.emit()

    @Slot(str, object)
    def _completed_media_changed(self, _task_id: str, _media: object) -> None:
        self._schedule_completed_media_refresh()

    @Slot(int, str, str)
    def _publish_status_changed(
        self,
        _task_id: int,
        _status: str,
        _result: str,
    ) -> None:
        self._publish_queue_model.refreshTask(_task_id)
        try:
            row = self._runtime.db.get_publish_task(int(_task_id or 0))
            media_id = int(row["media_id"] or 0) if row is not None else 0
        except Exception:
            media_id = 0
        if not self._completed_media_model.refreshDistributionStatus(media_id):
            self._schedule_completed_media_refresh()

    def _schedule_completed_media_refresh(self) -> None:
        self._completed_media_model.markDirty()
        if (
            self._completed_media_model.loaded
            and not self._completed_media_refresh_timer.isActive()
        ):
            self._completed_media_refresh_timer.start()

    def _refresh_completed_media_if_loaded(self) -> None:
        self._completed_media_refresh_timer.stop()
        self._completed_media_model.markDirty()
        if self._completed_media_model.loaded:
            self._completed_media_model.refresh()

    @Slot(str)
    def _collection_entries_changed(self, task_id: str) -> None:
        if (
            task_id == self._collection_model.parentTaskId
            and not self._collection_refresh_timer.isActive()
        ):
            self._collection_refresh_timer.start()

    def _refresh_collection_entries(self) -> None:
        self._collection_model.refresh(append_only=True)

    @Slot(str, str)
    def _conversion_failed(self, _task_id: str, error: str) -> None:
        self._set_message(str(error or "格式转换失败"))

    @Slot(str, str, str, bool, str)
    def _download_cookie_login_result(
        self,
        platform: str,
        account: str,
        action: str,
        ok: bool,
        result: str,
    ) -> None:
        if (platform, account, action) != ("browser", "download", "login"):
            return
        if not ok:
            self._set_message(str(result or "内置浏览器登录失败"))
            return
        try:
            self._runtime.app_settings.set_many({
                "download_cookie_source": COOKIE_SOURCE_EMBEDDED,
            })
            self._settings_model.refresh()
        except Exception as exc:
            self._set_message(f"登录成功，但无法保存 Cookie 设置：{exc}")
            return
        self._set_message(str(result or "登录成功，下载将使用内置 Cookie"))

    @Slot(str, str, str)
    def _database_recovery_notice(
        self,
        title: str,
        summary: str,
        detail: str,
    ) -> None:
        self.databaseRecoveryRequested.emit(title, summary, detail)

    @staticmethod
    def _cookie_expiry_text(value: object) -> str:
        try:
            expires = int(float(value or 0))
        except (TypeError, ValueError, OverflowError):
            return "未知"
        if expires <= 0:
            return "会话"
        try:
            timestamp = datetime.fromtimestamp(expires).strftime("%Y-%m-%d %H:%M")
        except (OverflowError, OSError, ValueError):
            return "未知"
        return f"已过期 · {timestamp}" if expires <= datetime.now().timestamp() else timestamp

    def _open_local_directory(self, path: Path, label: str) -> None:
        try:
            path.mkdir(parents=True, exist_ok=True)
            opened = QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.resolve())))
        except OSError as exc:
            self._set_message(f"无法打开{label}：{exc}")
            return
        if not opened:
            self._set_message(f"无法打开{label}")

    def _copy_text(self, value: str, success_message: str) -> None:
        text = str(value or "").strip()
        if not text:
            self._set_message("没有可复制的内容")
            return
        clipboard = QGuiApplication.clipboard()
        if clipboard is None:
            self._set_message("无法访问系统剪贴板")
            return
        clipboard.setText(text)
        self._set_message(success_message)

    @Slot(str)
    def copyText(self, value: str) -> None:
        self._copy_text(value, "已复制到剪贴板")

    def _refresh_after_database_reset(self, database: Any) -> None:
        draft_was_open = self._publish_draft.active
        self._completed_media_refresh_timer.stop()
        self._collection_workflow.reset_after_database_change()
        self._format_model.clear()
        self._task_model.refresh()
        self._collection_model.setDatabase(database)
        self._completed_media_model.setDatabase(database)
        self._completed_media_model.refresh()
        self._publish_queue_model.setDatabase(database)
        self._publish_queue_model.refresh()
        self._publish_draft.setDatabase(database)
        if draft_was_open:
            self.publishEditorCompleted.emit()
        self._refresh_summary()

    @Slot()
    def _refresh_summary(self) -> None:
        statistics = self._runtime.download_service.task_statistics(top_level_only=True)
        active = int(statistics.get("in_progress", 0) or 0)
        total = int(statistics.get("total", 0) or 0)
        completed = statistics.get("completed", 0)
        speed = format_speed(self._runtime.download_service.total_speed_bps())
        summary = f"任务 {total} · 进行中 {active} · 已完成 {completed}"
        if speed:
            summary += f" · {speed}"
        if summary != self._summary:
            self._summary = summary
            self.summaryChanged.emit()

    def _set_message(self, message: str) -> None:
        message = str(message or "")
        if message == self._message:
            return
        self._message = message
        self.messageChanged.emit()

    @Slot()
    def _shutdown_completed(self) -> None:
        self.shutdownChanged.emit()
        self.shutdownCompleted.emit()
