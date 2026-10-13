from __future__ import annotations

import json
import base64
import math
import os
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, Property, QTimer, QUrl, Signal, Slot, Qt

from app.core.redaction import redact_secret_text, redact_url
from app.presentation.app_bridge import AppBridge

MAX_REQUEST_BYTES = 1024 * 1024


class ElectronNotifications(QObject):
    activateRequested = Signal()
    downloadRequested = Signal(str)
    publishRequested = Signal(int)
    notification = Signal(object)

    def __init__(self, runtime, _request_exit, parent=None):
        super().__init__(parent)
        self.runtime = runtime

    def setup(self):
        pass

    def syncVisibility(self):
        pass

    def requestShutdown(self):
        pass

    def downloadFinished(self, task_id, status, error):
        if status in {"completed", "failed", "processing_failed"} and self.runtime.app_settings.get_bool("desktop_notifications", True):
            task = self.runtime.download_service.tasks.get(task_id)
            self.notification.emit({"kind": "download", "id": task_id, "title": str(task.title if task else "下载任务"), "body": "下载及处理已完成" if status == "completed" else redact_secret_text(error or "任务失败")})

    def publishFinished(self, task_id, status, result):
        if status in {"success", "failed"} and self.runtime.app_settings.get_bool("desktop_notifications", True):
            self.notification.emit({"kind": "publish", "id": task_id, "title": "发布任务", "body": "发布成功" if status == "success" else redact_secret_text(result or "发布失败")})


class ElectronBridge(AppBridge):
    native = Signal(object)

    def __init__(self, runtime, parent=None):
        super().__init__(runtime, parent, desktop_notifications_factory=ElectronNotifications)

    def _copy_text(self, value, success_message):
        if not str(value or "").strip():
            self._set_message("没有可复制的内容")
            return
        self.native.emit({"action": "copy", "text": str(value)})
        self._set_message(success_message)

    def _open_local_directory(self, path, label):
        try:
            path = Path(path).resolve()
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._set_message(f"无法打开{label}：{exc}")
            return
        self.native.emit({"action": "openDirectory", "path": str(path)})

    def openTaskLocation(self, task_id):
        task = self._task(task_id)
        if task is None:
            return
        path = Path(task.media_path) if task.media_path else Path(task.output_dir)
        if path.is_file():
            self.native.emit({"action": "reveal", "path": str(path.resolve())})
        else:
            self._open_local_directory(path, "任务目录")

    def openMediaLocation(self, media_id):
        media = self._runtime.db.get_media(int(media_id))
        if media and Path(media.video_path).is_file():
            self.native.emit({"action": "reveal", "path": str(Path(media.video_path).resolve())})
        else:
            self._set_message("没有可打开的视频文件")


def json_value(value):
    if isinstance(value, QUrl):
        return value.toString()
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def model_rows(model):
    roles = {role: bytes(name).decode("utf-8") for role, name in model.roleNames().items()}
    return [{name: json_value(model.data(model.index(row, 0), role)) for role, name in roles.items()} for row in range(model.rowCount())]


class ElectronRoutes(QObject):
    changed = Signal()

    def __init__(self, runtime, parent=None):
        super().__init__(parent)
        self.runtime = runtime
        runtime.update_service.route_probe_finished.connect(lambda *_: self.changed.emit())
        runtime.update_service.route_probe_failed.connect(lambda *_: self.changed.emit())

    @Property("QVariantList", notify=changed)
    def rows(self):
        from app.core.github_mirrors import github_download_routes
        service = self.runtime.update_service
        return [{"id": route.id, "name": route.name, "url": route.base_url or "https://github.com/", "thirdParty": route.third_party, **service.route_probe_results.get(route.id, {})} for route in github_download_routes(self.runtime.app_settings.get("github_mirror_urls"))]

    @Property(bool, notify=changed)
    def busy(self):
        return self.runtime.update_service.runtime_active("route_probe")

    @Property(str, notify=changed)
    def selected(self):
        return self.runtime.app_settings.get("github_download_route")

    @Slot(result=bool)
    def probe(self):
        result = self.runtime.update_service.probe_download_routes()
        self.changed.emit()
        return bool(result)


# This table is the complete IPC surface, independent of QObject reflection.
COMMANDS = {
    "app": {
        "submitUrls": (str,), "startTask": (str,), "pauseTask": (str,), "cancelTask": (str,),
        "retryTask": (str,), "redownloadTask": (str,), "deleteTask": (str, bool),
        "copyTaskSource": (str,), "copyTaskFolder": (str,), "taskLogText": (str,),
        "clearTaskLog": (str,), "openTaskLogLocation": (str,), "openDownloadDirectory": (),
        "pauseAllTasks": (), "resumeAllTasks": (), "cleanupCompletedTasks": (),
        "openTaskLocation": (str,), "refreshCompletedMedia": (), "refreshPublishQueue": (),
        "showPublishQueueForMedia": (int,), "runPublishTask": (int,), "retryPublishTask": (int,),
        "openPublishEditor": (int,), "submitPublishDraft": (), "openMediaLocation": (int,),
        "openCoverStudio": (int,), "convertTask": (str, str), "showCollectionEntries": (str,),
        "openCollectionSelection": (str,), "openCollectionDetails": (str,), "openFormatSelection": (str,),
        "confirmFormatSelection": (int,), "cancelFormatSelection": (),
        "confirmCollectionSelection": (str,), "cancelCollectionSelection": (str,),
        "checkDownloadCookies": (), "openDownloadCookieLogin": (), "embeddedCookies": (str, bool),
        "openLogDirectory": (), "openLanguagePackDirectory": (), "diagnosticsDefaultPath": (),
        "exportDiagnostics": (str,), "openDiagnosticsLocation": (str,), "scanLocalHistory": (str,),
        "importScannedLocalHistory": (), "copyText": (str,), "requestShutdown": (),
    },
    "tasks": {"setFilterText": (str,), "setStatusFilter": (str,), "setSortMode": (str,)},
    "accounts": {"setAccount": (str, str), "setTarget": (str, bool), "save": (), "runAction": (str, str), "refresh": ()},
    "collection": {"setFilterText": (str,), "setStatusFilter": (str,), "setSortMode": (str, bool), "setSelected": (int, bool), "setSelectionMode": (str,)},
    "formats": {"setContentMode": (str,), "setAudioFormat": (str,)},
    "completed": {"setFilter": (str,), "setSearchText": (str,)},
    "publish": {"setMediaId": (int,), "setSearchText": (str,), "setStatusFilter": (str,)},
    "draft": {"close": (), "setPlatformSelected": (str, bool), "setPlatformAccount": (str, str)},
    "settings": {"save": (dict, dict, str), "clearOpenAiKey": ()},
    "cover": {"close": (), "setPreset": (str,), "setFitMode": (str,), "setFocusX": (float,), "setFocusY": (float,), "setQuality": (int,), "saveOptions": (), "restoreOriginal": (), "copyCover": (), "saveJpeg": (str,), "generate": (str,), "cancelGeneration": ()},
    "updates": {"checkApplication": (), "downloadApplication": (), "cancelApplicationDownload": (), "installApplication": (), "checkRuntimeComponents": (), "componentNeedsChecksumConfirmation": (str,), "installRuntimeComponent": (str, bool)},
    "sites": {"load": (), "setFilterText": (str,)},
    "routes": {"probe": ()},
}

PROPERTIES = {
    "app": ("appName", "appVersion", "message", "statusSummary", "shutdownStarted", "shutdownReady"),
    "tasks": ("count", "filterText", "statusFilter", "sortMode"),
    "accounts": ("count", "targetCount"),
    "collection": ("count", "parentTaskId", "filterText", "statusFilter", "sortMode", "sortDescending", "selectedCount"),
    "formats": ("count", "firstChoiceIndex", "pending", "taskId", "title", "thumbnailUrl", "contentMode", "audioFormat", "hasVideo", "hasAudio"),
    "completed": ("count", "filter", "searchText", "totalCount", "needsDistributionCount", "publishedCount", "queuedCount", "retryNeededCount", "completeCount"),
    "publish": ("count", "mediaId", "searchText", "statusFilter"),
    "draft": ("active", "mediaId", "mediaTitle", "sourceUrl", "sourceIp", "title", "description", "topics", "sourceType", "visibility", "collection", "playlist", "partition", "schedule", "thumbnail", "thumbnailLandscape", "thumbnailPortrait", "platforms", "lastMessage", "supportsVisibility", "supportsCollection", "supportsPlaylist", "requiresPartition", "supportsSchedule", "supportsDualThumbnail"),
    "settings": ("values", "downloadOptions", "hasOpenAiKey"),
    "cover": ("active", "busy", "dirty", "title", "previewUrl", "status", "preset", "fitMode", "focusX", "focusY", "quality", "defaultOutputPath", "presets"),
    "updates": ("applicationSupported", "applicationBusy", "applicationStatus", "applicationProgress", "applicationUpdate", "runtimeBusy", "runtimeStatus", "installingComponent", "runtimeResults"),
    "sites": ("count", "totalCount", "loaded", "error", "filterText"),
    "routes": ("rows", "busy", "selected"),
}
DRAFT_FIELDS = frozenset({"title", "description", "topics", "sourceType", "visibility", "collection", "playlist", "partition", "schedule", "thumbnail", "thumbnailLandscape", "thumbnailPortrait"})


def validate_request(request):
    if not isinstance(request, dict) or set(request) != {"id", "target", "action", "args"}:
        raise ValueError("Malformed request")
    if type(request["id"]) is not int or not 0 < request["id"] < 2**53:
        raise ValueError("Invalid request ID")
    target, action, args = request["target"], request["action"], request["args"]
    if type(target) is not str or type(action) is not str or type(args) is not list:
        raise ValueError("Invalid command")
    if target == "host" and action in {"snapshot", "capabilities", "health"} and not args:
        return ()
    if target == "draft" and action == "update" and len(args) == 1 and type(args[0]) is dict:
        if not set(args[0]).issubset(DRAFT_FIELDS) or any(type(value) is not str or len(value) > 100000 for value in args[0].values()):
            raise ValueError("Invalid draft fields")
        return (dict,)
    schema = COMMANDS.get(target, {}).get(action)
    if schema is None or len(args) != len(schema):
        raise ValueError("Unsupported command")
    for value, expected in zip(args, schema):
        valid = type(value) is expected or expected is float and type(value) in {int, float}
        if not valid or isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Invalid command arguments")
        if isinstance(value, str) and len(value) > 200000:
            raise ValueError("Command argument too long")
    return schema


class ElectronHost(QObject):
    incoming = Signal(object)
    disconnected = Signal()

    def __init__(self, runtime, output: Callable[[dict], None], parent=None):
        super().__init__(parent)
        self.runtime, self.output = runtime, output
        self.bridge = ElectronBridge(runtime, self)
        # Electron waits for the final persistence callback before closing.
        self.bridge._shutdown_controller.FORCE_EXIT_AFTER_SECONDS = float("inf")
        self.targets = {"app": self.bridge, "tasks": self.bridge.taskModel, "accounts": self.bridge.accountModel, "collection": self.bridge.collectionModel, "formats": self.bridge.formatModel, "completed": self.bridge.completedMediaModel, "publish": self.bridge.publishQueueModel, "draft": self.bridge.publishDraft, "settings": self.bridge.settingsModel, "cover": self.bridge.coverStudio, "updates": self.bridge.updateModel, "sites": self.bridge.supportedSiteModel, "routes": ElectronRoutes(runtime, self)}
        self._dirty = set(self.targets)
        self._task_deltas: set[str] = set()
        self._assets: dict[str, str] = {}
        self._recent_rows: list[dict] | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self.flush)
        self.incoming.connect(self.dispatch, Qt.QueuedConnection)
        self.disconnected.connect(self.bridge.requestShutdown, Qt.QueuedConnection)
        self.bridge.shutdownCompleted.connect(lambda: self.emit("shutdown", {}))
        self.bridge.native.connect(lambda payload: self.emit("native", payload))
        self.bridge._desktop_notifications.notification.connect(lambda payload: self.emit("notification", payload))
        events = {"collectionSelectionRequested": "collectionSelection", "collectionDetailsRequested": "collectionDetails", "formatSelectionRequested": "formatSelection", "navigationRequested": "navigation", "publishEditorRequested": "publishEditor", "publishEditorCompleted": "publishEditorCompleted", "coverStudioRequested": "coverStudio", "localHistoryImportRequested": "localHistoryImport", "databaseRecoveryRequested": "databaseRecovery", "windowActivationRequested": "activate", "taskFocusRequested": "taskFocus", "publishTaskFocusRequested": "publishTaskFocus"}
        for signal_name, event in events.items():
            getattr(self.bridge, signal_name).connect(lambda *args, name=event: self.emit(name, list(args)))
        for name, target in self.targets.items():
            meta = target.metaObject()
            connected = set()
            for index in range(meta.propertyCount()):
                prop = meta.property(index)
                if prop.name() in PROPERTIES[name] and prop.hasNotifySignal():
                    signal_name = bytes(prop.notifySignal().name()).decode()
                    if signal_name not in connected:
                        getattr(target, signal_name).connect(lambda *_, section=name: self.mark_dirty(section))
                        connected.add(signal_name)
            if hasattr(target, "roleNames"):
                if name == "tasks":
                    target.dataChanged.connect(self._tasks_changed)
                else:
                    target.dataChanged.connect(lambda *_, section=name: self.mark_dirty(section))
                for signal_name in ("modelReset", "rowsInserted", "rowsRemoved", "layoutChanged"):
                    getattr(target, signal_name).connect(lambda *_, section=name: self.mark_dirty(section))
        runtime.download_service.task_updated.connect(lambda task: self.mark_task(task.id))
        runtime.download_service.task_progress.connect(lambda task_id, *_: self.mark_task(task_id))

    def _tasks_changed(self, first, last, _roles=None):
        model = self.bridge.taskModel
        for row in range(first.row(), last.row() + 1):
            self.mark_task(model.taskIdAt(row))

    def mark_task(self, task_id):
        self._task_deltas.add(task_id)
        if not self._timer.isActive() and not self.bridge.shutdownStarted:
            self._timer.start()

    def task_row(self, task):
        model = self.bridge.taskModel
        row = {bytes(name).decode(): json_value(model._role_value(task, role)) for role, name in model.roleNames().items()}
        options = task.options_json or {}
        checkpoint = options.get("_processing") or {}
        plan = options.get("_processing_plan") or {}
        row["waitingReason"] = str(getattr(task, "waiting_reason", "") or ("等待处理资源" if task.stage == "waiting_processing" else ""))
        row["failedStep"] = str(getattr(task, "failed_step", "") or checkpoint.get("failed_step", ""))
        row["stageIndeterminate"] = bool(getattr(task, "stage_indeterminate", True))
        row["processingPaused"] = task.status == "paused" and bool(checkpoint)
        processing_encoder = checkpoint.get("encoder", task.transcode_encoder) if checkpoint.get("kind") == "manual" else task.transcode_encoder
        row["processingPlan"] = {"encoder": processing_encoder, "subtitles": task.subtitle_language, "jpeg": plan.get("cover_convert_jpeg", False), "jpegQuality": plan.get("cover_jpeg_quality", 90), "prependCover": bool(options.get("prepend_cover_enabled", False)), "prependFrames": options.get("prepend_cover_frames", 3)}
        row["stageProgress"] = getattr(task, "stage_progress", None)
        row["thumbnailUrl"] = self.asset_url(row.get("thumbnailUrl", ""))
        row["url"] = redact_url(row["url"]) if row.get("url") else ""
        row["error"] = redact_secret_text(row.get("error", ""))
        for field in ("stageText", "failedStep", "waitingReason", "reconnectMessage"):
            row[field] = redact_secret_text(row.get(field, ""))
        return row

    def asset_url(self, value):
        url = QUrl(str(value or ""))
        if not url.isLocalFile():
            return str(value or "")
        path = Path(url.toLocalFile()).resolve()
        if path.suffix.casefold() not in {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"} or not path.is_file():
            return ""
        key = str(path)
        token = self._assets.get(key)
        if token is None:
            token = uuid.uuid4().hex
            self._assets[key] = token
            self.emit("asset", {"token": token, "path": key})
        return "huifa-media://image/" + token

    def recent_completed(self, *, refresh=False):
        if self._recent_rows is not None and not refresh:
            return self._recent_rows
        tasks = (task for task in self.runtime.download_service.tasks.values() if not task.parent_task_id and task.status == "completed")
        self._recent_rows = [self.task_row(task) for task in sorted(tasks, key=lambda task: task.downloaded_at or task.created_at, reverse=True)[:6]]
        return self._recent_rows

    def emit(self, event, data):
        if event in {"databaseRecovery", "backendError", "notification"}:
            data = self.redact_display(data)
        self.output({"type": "event", "event": event, "data": json_value(data)})

    @staticmethod
    def redact_display(value):
        if isinstance(value, str):
            return redact_secret_text(value)
        if isinstance(value, list):
            return [ElectronHost.redact_display(item) for item in value]
        if isinstance(value, dict):
            return {key: ElectronHost.redact_display(item) for key, item in value.items()}
        return value

    def mark_dirty(self, section):
        self._dirty.add(section)
        if not self._timer.isActive() and not self.bridge.shutdownStarted:
            self._timer.start()

    def snapshot(self, sections=None):
        result = {}
        for name in sections if sections is not None else self.targets:
            target = self.targets[name]
            state = {prop: json_value(getattr(target, prop)) for prop in PROPERTIES[name]}
            if name == "settings":
                from PySide6.QtGui import QGuiApplication
                from app.core.language_packs import discover_language_packs
                from app.presentation.runtime import configure_font
                application = QGuiApplication.instance()
                if application is not None:
                    diagnostics = configure_font(application, state["values"].get("ui_language", "auto"))
                    state["locale"] = diagnostics.locale
                    state["translations"] = json_value(application.property("huifa.ui_translations") or {})
                state["languages"] = [{"locale": pack.locale, "name": pack.native_name} for pack in discover_language_packs().values()]
            if name == "settings" and state["values"].get("proxy"):
                state["values"]["proxy"] = redact_url(state["values"]["proxy"])
            if name == "app":
                state["message"] = redact_secret_text(state["message"])
            for prop in ("status", "lastMessage", "applicationStatus", "runtimeStatus", "error"):
                if prop in state:
                    state[prop] = self.redact_display(state[prop])
            if name == "updates":
                for row in state["runtimeResults"]:
                    for prop in ("error", "message", "status"):
                        if prop in row:
                            row[prop] = self.redact_display(row[prop])
            if name == "routes":
                for row in state["rows"]:
                    for prop in ("error", "metadata_error", "asset_error"):
                        if prop in row:
                            row[prop] = self.redact_display(row[prop])
            if name == "draft" and state.get("sourceUrl"):
                state["sourceUrl"] = redact_url(state["sourceUrl"])
            for prop in ("thumbnailUrl", "previewUrl"):
                if prop in state:
                    state[prop] = self.asset_url(state[prop])
            if hasattr(target, "roleNames") and name != "tasks":
                state["rows"] = model_rows(target)
                for row in state["rows"]:
                    if "thumbnailUrl" in row:
                        row["thumbnailUrl"] = self.asset_url(row["thumbnailUrl"])
                    if row.get("sourceUrl"):
                        row["sourceUrl"] = redact_url(row["sourceUrl"])
                    if "result" in row:
                        row["result"] = redact_secret_text(row["result"])
            if name == "tasks":
                ids = [target.taskIdAt(row) for row in range(target.rowCount())]
                state["rows"] = [self.task_row(self.runtime.download_service.tasks[task_id]) for task_id in ids if task_id in self.runtime.download_service.tasks]
                state["recentCompleted"] = self.recent_completed(refresh=True)
            result[name] = state
        return result

    @Slot()
    def flush(self):
        sections, self._dirty = self._dirty, set()
        task_ids, self._task_deltas = self._task_deltas, set()
        if sections:
            self.emit("state", self.snapshot(sections))
        if task_ids and "tasks" not in sections:
            rows = [self.task_row(self.runtime.download_service.tasks[task_id]) for task_id in task_ids if task_id in self.runtime.download_service.tasks]
            refresh_recent = any(row["status"] == "completed" for row in rows)
            self.emit("tasksDelta", {"rows": rows, "recentCompleted": self.recent_completed(refresh=refresh_recent)})

    @Slot(object)
    def dispatch(self, request):
        request_id = request.get("id") if isinstance(request, dict) else None
        try:
            validate_request(request)
            target, action, args = request["target"], request["action"], request["args"]
            if self.bridge.shutdownStarted and not (target == "host" or target == "app" and action == "requestShutdown"):
                raise ValueError("Backend is shutting down")
            if target == "host":
                if action == "snapshot":
                    value = self.snapshot()
                elif action == "health":
                    value = self.health()
                else:
                    value = {name: list(commands) for name, commands in COMMANDS.items()}
            elif target == "draft" and action == "update":
                for name, value in args[0].items():
                    setattr(self.bridge.publishDraft, name, value)
                value = True
            elif target == "cover" and action == "copyCover":
                studio = self.bridge.coverStudio
                if studio._current is None:
                    value = False
                else:
                    image = self.runtime.cover_service.prepare_clipboard(studio._current, studio._options())
                    self.bridge.native.emit({"action": "copyImage", "png": base64.b64encode(image.png_bytes).decode("ascii")})
                    studio._set_status(f"已复制 {image.width}×{image.height} 封面到剪贴板")
                    value = True
            else:
                if target == "settings" and action == "save":
                    current_proxy = self.runtime.app_settings.get("proxy")
                    if current_proxy and args[0].get("proxy") == redact_url(current_proxy):
                        args[0]["proxy"] = current_proxy
                value = getattr(self.targets[target], action)(*args)
            self.output({"type": "result", "id": request_id, "ok": True, "value": json_value(value)})
            self.mark_dirty(target if target in self.targets else "app")
        except Exception as exc:
            self.output({"type": "result", "id": request_id, "ok": False, "error": redact_secret_text(str(exc))[:2000]})

    def health(self):
        from PySide6 import __version__ as pyside_version
        from PySide6.QtGui import QGuiApplication
        from app.core.download_service import yt_dlp
        from app.core.paths import application_dir, data_dir
        from app.core.update_service import installed_component_details
        from app.core.version import APP_VERSION, APP_PUBLISHER
        application = QGuiApplication.instance()
        result = {"schema_version": 2, "frozen": bool(getattr(sys, "frozen", False)), "executable": str(Path(sys.executable).resolve()), "application_dir": str(application_dir()), "data_dir": str(data_dir()), "application_update_mode": self.runtime.application_update_mode, "application_version": application.applicationVersion() if application else "", "expected_application_version": APP_VERSION, "organization_name": application.organizationName() if application else "", "expected_organization_name": APP_PUBLISHER, "pyside6_version": pyside_version, "secure_store_backend": getattr(self.runtime.secure_store, "backend_name", ""), "protocol": 1}
        for name, label in (("yt_dlp", "yt-dlp"), ("ffmpeg", "FFmpeg"), ("ffprobe", "FFprobe")):
            configured_ffprobe = self.runtime.app_settings.get("ffprobe_path")
            version, source, path = installed_component_details(label, self.runtime.app_settings.get("ffmpeg_path"), configured_ffprobe) if label == "FFprobe" and configured_ffprobe.strip() else installed_component_details(label, self.runtime.app_settings.get("ffmpeg_path") if label != "yt-dlp" else "")
            result[name] = {"version": version, "source": source, "runtime_path": path}
        result["yt_dlp"]["core_ready"] = bool(yt_dlp is not None and callable(getattr(yt_dlp, "YoutubeDL", None)))
        return result

    def start(self):
        self.bridge.restoreStartupState()
        self.bridge.refreshCompletedMedia()
        self.bridge.refreshPublishQueue()
        self.flush()
        self.emit("ready", {"protocol": 1})

    def read_stdin(self, stream=None):
        # Raw I/O has no buffered-reader lock held during interpreter teardown.
        stream = stream or os.fdopen(os.dup(sys.stdin.fileno()), "rb", buffering=0)
        while True:
            raw = stream.readline(MAX_REQUEST_BYTES + 1)
            if not raw:
                self.disconnected.emit()
                return
            if len(raw) > MAX_REQUEST_BYTES or not raw.endswith(b"\n"):
                self.output({"type": "protocolError", "error": "Request exceeds limit"})
                self.disconnected.emit()
                return
            try:
                request = json.loads(raw)
            except (UnicodeError, ValueError):
                self.output({"type": "protocolError", "error": "Invalid JSON request"})
                continue
            self.incoming.emit(request)

    def start_reader(self):
        threading.Thread(target=self.read_stdin, name="electron-ipc-reader", daemon=True).start()
