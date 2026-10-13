from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QObject, Signal, Slot

from app.core.collection_service import CollectionProbeRequest, CollectionProbeWorker
from app.core.download_options import DownloadOptions
from app.core.download_submission import service_task_arguments
from app.presentation.collection_probe_coordinator import CollectionProbeCoordinator


class QmlCollectionWorkflow(QObject):
    """Run collection discovery without depending on QWidget presentation."""

    statusChanged = Signal(str)
    selectionRequested = Signal(str, str)
    entriesChanged = Signal(str)

    def __init__(self, runtime: Any, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self.coordinator = CollectionProbeCoordinator(
            on_metadata=self._on_metadata,
            on_entries=self._on_entries,
            on_single=self._on_single,
            on_failed=self._on_failed,
            on_finished=self._on_finished,
            on_start_error=self._on_start_error,
            on_slot_released=self._start_pending,
            parent=self,
            max_concurrent=2,
        )

    @property
    def running(self) -> bool:
        return self.coordinator.running

    def request_shutdown(self) -> None:
        self.coordinator.request_shutdown()

    def reset_after_database_change(self) -> None:
        self.coordinator.clear_inactive_states()

    def start(self, url: str, context: Mapping[str, object]) -> str:
        return self.start_probe(url, dict(context))

    def start_probe(
        self,
        url: str,
        context: dict[str, object],
        *,
        parent_collection_task_id: str = "",
        collection_index: int = 0,
        visited_source_keys: set[str] | None = None,
        existing_parent_id: str = "",
        resume_index: int = 0,
        dedupe_request: CollectionProbeRequest | None = None,
    ) -> str:
        if self.coordinator.shutdown_requested:
            return ""
        request_id = uuid4().hex[:12]
        request = self._build_probe_request(
            request_id,
            url,
            context,
            resume_index,
            dedupe_request,
        )
        parent_id, created_parent = self._resolve_probe_parent(
            url,
            context,
            parent_collection_task_id=parent_collection_task_id,
            collection_index=collection_index,
            existing_parent_id=existing_parent_id,
        )
        try:
            cached_count = max(
                0,
                int(self._runtime.db.collection_probe_entry_count(parent_id) or 0),
            )
            parent_task = self._runtime.download_service.tasks.get(parent_id)
            state = {
                "request": request,
                "context": deepcopy(context),
                "url": url,
                "entry_count": cached_count,
                "metadata": {
                    "title": parent_task.title if parent_task is not None else "",
                    "source_key": parent_task.source_key if parent_task is not None else "",
                },
                "parent_id": parent_id,
                "thread": None,
                "worker": None,
                "confirmed": False,
                "parent_collection_task_id": parent_collection_task_id,
                "collection_index": collection_index,
                "visited_source_keys": set(visited_source_keys or ()),
                "source_key_registered": False,
                "selection_shown": False,
            }
        except Exception:
            if created_parent:
                self._runtime.download_service.delete_task(parent_id, False)
            raise
        if not self.coordinator.enqueue(request_id, state):
            if created_parent:
                self._runtime.download_service.delete_task(parent_id, False)
            return ""
        self.statusChanged.emit("正在后台解析合集…")
        self._start_pending()
        return parent_id

    def resume(self) -> None:
        """Restore collection parsing or selection after task recovery."""

        if self.coordinator.shutdown_requested:
            return
        active_parent_ids = {
            str(state.get("parent_id") or "")
            for state in self.coordinator.states.values()
            if not state.get("confirmed")
        }
        for task in tuple(self._runtime.download_service.tasks.values()):
            if (
                task.task_kind != "collection"
                or task.status not in {"parsing_collection", "waiting_selection"}
                or task.id in active_parent_ids
            ):
                continue
            try:
                context = self._resume_context(task)
                count = max(
                    0,
                    int(self._runtime.db.collection_probe_entry_count(task.id) or 0),
                )
                options = self._options(context)
                if task.status == "waiting_selection" or (
                    options.first_n and count >= options.first_n
                ):
                    self._restore_finished_probe(task, context, count)
                else:
                    self.start_probe(
                        task.url,
                        context,
                        parent_collection_task_id=task.parent_task_id,
                        collection_index=task.collection_index,
                        existing_parent_id=task.id,
                        resume_index=count,
                    )
            except Exception as exc:
                # A malformed/stale collection row must not prevent other
                # collections from being restored on startup.
                message = f"恢复合集解析失败：{exc}"
                try:
                    self._runtime.download_service.fail_collection_probe(
                        task.id,
                        message,
                    )
                except Exception:
                    pass
                self.statusChanged.emit(message)

    def _resume_context(self, task: Any) -> dict[str, object]:
        options = (
            deepcopy(dict(task.options_json))
            if isinstance(task.options_json, Mapping)
            else {}
        )
        parsed = DownloadOptions.from_mapping(options)
        return {
            "output_dir": task.output_dir,
            "proxy": task.proxy,
            "cookie_file": task.cookie_file,
            "cookie_source": task.cookie_source,
            "cookie_browser": task.cookie_browser,
            "cookie_profile": task.cookie_profile,
            "cookie_keyring": task.cookie_keyring,
            "cookie_container": task.cookie_container,
            "quality": task.quality,
            "filename_template": task.filename_template,
            "organize_task_folder": parsed.organize_task_folder,
            "ffmpeg_path": task.ffmpeg_path,
            "download_album": True,
            "playlist_mode": "playlist",
            "transcode_codec": task.transcode_codec,
            "transcode_device": task.transcode_device,
            "transcode_encoder": task.transcode_encoder,
            "subtitle_language": task.subtitle_language,
            "prepend_cover_enabled": parsed.prepend_cover_enabled,
            "prepend_cover_frames": parsed.prepend_cover_frames,
            "options_json": options,
        }

    def _restore_finished_probe(
        self,
        task: Any,
        context: dict[str, object],
        cached_count: int,
    ) -> None:
        request_id = uuid4().hex[:12]
        request = self._build_probe_request(request_id, task.url, context, cached_count)
        self.coordinator.states[request_id] = {
            "request": request,
            "context": context,
            "url": task.url,
            "entry_count": cached_count,
            "metadata": {"title": task.title, "source_key": task.source_key},
            "parent_id": task.id,
            "thread": None,
            "worker": None,
            "confirmed": False,
            "parent_collection_task_id": task.parent_task_id,
            "collection_index": task.collection_index,
            "visited_source_keys": {task.source_key} if task.source_key else set(),
            "source_key_registered": bool(task.source_key),
            "selection_shown": False,
        }
        try:
            if task.status != "waiting_selection":
                self._runtime.download_service.update_collection_probe(
                    task.id,
                    title=task.title,
                    source_key=task.source_key,
                    parsed_count=cached_count,
                    finished=True,
                )
            options = self._options(context)
            if options.collection_mode == "all":
                self._materialize_collection(request_id)
                return
            self._request_selection(request_id)
        except Exception:
            self.coordinator.states.pop(request_id, None)
            raise

    def _build_probe_request(
        self,
        request_id: str,
        url: str,
        context: Mapping[str, object],
        resume_index: int,
        dedupe_request: CollectionProbeRequest | None = None,
    ) -> CollectionProbeRequest:
        raw_options = context.get("options_json")
        options = dict(raw_options) if isinstance(raw_options, Mapping) else {}
        if dedupe_request is None:
            source_keys, urls, titles = self._runtime.db.completed_media_identities()
        else:
            source_keys = dedupe_request.completed_source_keys
            urls = dedupe_request.completed_urls
            titles = dedupe_request.completed_titles
        return CollectionProbeRequest(
            request_id=request_id,
            url=url,
            core_mode=self._runtime.download_service.ytdlp_core_mode,
            proxy=str(context.get("proxy") or ""),
            cookie_file=str(context.get("cookie_file") or ""),
            cookie_source=str(context.get("cookie_source") or "none"),
            cookie_browser=str(context.get("cookie_browser") or "chrome"),
            cookie_profile=str(context.get("cookie_profile") or ""),
            cookie_keyring=str(context.get("cookie_keyring") or ""),
            cookie_container=str(context.get("cookie_container") or ""),
            deno_path=self._runtime.download_service.deno_path,
            ytdlp_ejs_source=self._runtime.download_service.ytdlp_ejs_source,
            options=deepcopy(options),
            completed_source_keys=set(source_keys),
            completed_urls=set(urls),
            completed_titles=set(titles),
            resume_index=max(0, int(resume_index or 0)),
        )

    def _resolve_probe_parent(
        self,
        url: str,
        context: Mapping[str, object],
        *,
        parent_collection_task_id: str,
        collection_index: int,
        existing_parent_id: str,
    ) -> tuple[str, bool]:
        if existing_parent_id:
            return str(existing_parent_id), False
        parent_collection = self._runtime.download_service.tasks.get(parent_collection_task_id)
        parent_id = self._runtime.download_service.create_collection(
            url,
            str(context["output_dir"]),
            title="正在解析合集",
            parent_task_id=parent_collection_task_id,
            root_task_id=(parent_collection.root_task_id if parent_collection else ""),
            collection_index=collection_index,
            **service_task_arguments(dict(context), playlist_mode="playlist"),
        )
        if not parent_id:
            raise RuntimeError("无法创建合集任务")
        return str(parent_id), True

    def _live_state(self, request_id: str) -> dict[str, Any] | None:
        state = self.coordinator.result_state(request_id)
        return state if isinstance(state, dict) else None

    def _register_source_key(self, request_id: str, state: dict[str, Any]) -> bool:
        metadata = state.get("metadata") or {}
        source_key = str(metadata.get("source_key") or "")
        if not source_key or state.get("source_key_registered"):
            return True
        visited = set(state.get("visited_source_keys") or ())
        if source_key in visited:
            self._on_failed(request_id, "检测到合集循环，已停止解析")
            return False
        visited.add(source_key)
        state["visited_source_keys"] = visited
        state["source_key_registered"] = True
        return True

    @Slot(str, object)
    def _on_metadata(self, request_id: str, metadata: object) -> None:
        state = self._live_state(request_id)
        if state is None or not isinstance(metadata, dict):
            return
        state["metadata"] = dict(metadata)
        if not self._register_source_key(request_id, state):
            return
        parent_id = str(state.get("parent_id") or "")
        try:
            updated = self._runtime.download_service.update_collection_probe(
                parent_id,
                title=str(metadata.get("title") or ""),
                source_key=str(metadata.get("source_key") or ""),
                parsed_count=int(state.get("entry_count") or 0),
            )
        except Exception as exc:
            self._on_failed(request_id, f"保存合集信息失败：{exc}")
            return
        if not updated:
            self._on_failed(request_id, "合集任务已不存在，已停止解析")

    @Slot(str, object)
    def _on_entries(self, request_id: str, payload: object) -> None:
        state = self._live_state(request_id)
        if state is None or not isinstance(payload, list):
            return
        entries = [dict(entry) for entry in payload if isinstance(entry, dict)]
        if not entries:
            return
        parent_id = str(state.get("parent_id") or "")
        try:
            count = self._runtime.download_service.store_collection_probe_entries(
                parent_id,
                entries,
            )
        except Exception as exc:
            self._on_failed(request_id, f"保存合集条目失败：{exc}")
            return
        if count is None:
            self._on_failed(request_id, "合集任务已不存在，已停止解析")
            return
        state["entry_count"] = count
        metadata = state.get("metadata") or {}
        try:
            updated = self._runtime.download_service.update_collection_probe(
                parent_id,
                title=str(metadata.get("title") or ""),
                source_key=str(metadata.get("source_key") or ""),
                parsed_count=count,
            )
        except Exception as exc:
            self._on_failed(request_id, f"更新合集进度失败：{exc}")
            return
        if not updated:
            self._on_failed(request_id, "合集任务已不存在，已停止解析")
            return
        self.entriesChanged.emit(parent_id)
        options = self._options(state.get("context"))
        if options.collection_mode == "select":
            self._request_selection(request_id)

    @Slot(str, object)
    def _on_single(self, request_id: str, info: object) -> None:
        state = self._live_state(request_id)
        if state is None:
            return
        metadata = state.get("metadata") or {}
        single = info if isinstance(info, dict) else {}
        parent_id = str(state.get("parent_id") or "")
        try:
            resolved = self._runtime.download_service.resolve_collection_as_video(
                parent_id,
                title=str(single.get("title") or metadata.get("title") or ""),
                source_key=str(metadata.get("source_key") or ""),
            )
        except Exception as exc:
            self._on_failed(request_id, f"创建单视频任务失败：{exc}")
            return
        if not resolved:
            self._on_failed(request_id, "无法启动解析出的单视频任务")
            return
        state["confirmed"] = True
        self.statusChanged.emit("链接已识别为单个视频，已加入下载队列")
        if not self.coordinator.thread_is_running(state.get("thread")):
            self.coordinator.states.pop(request_id, None)

    @Slot(str, str)
    def _on_failed(self, request_id: str, error: str) -> None:
        state = self._live_state(request_id)
        if state is None:
            return
        message = str(error or "合集解析失败")
        state["confirmed"] = True
        worker = state.get("worker")
        if isinstance(worker, CollectionProbeWorker):
            try:
                worker.cancel()
            except RuntimeError:
                pass
        parent_id = str(state.get("parent_id") or "")
        try:
            saved = (
                self._runtime.download_service.fail_collection_probe(parent_id, message)
                if parent_id else False
            )
        except Exception as exc:
            self.statusChanged.emit(f"{message}；无法保存失败状态：{exc}")
        else:
            self.statusChanged.emit(
                message if saved else f"{message}；合集任务已不存在"
            )
        if not self.coordinator.thread_is_running(state.get("thread")):
            self.coordinator.states.pop(request_id, None)

    @Slot(str, bool, int)
    def _on_finished(self, request_id: str, is_collection: bool, _count: int) -> None:
        state = self._live_state(request_id)
        if state is None or not is_collection:
            return
        parent_id = str(state.get("parent_id") or "")
        try:
            count = max(
                0,
                int(self._runtime.db.collection_probe_entry_count(parent_id) or 0),
            )
        except Exception as exc:
            self._on_failed(request_id, f"读取合集条目失败：{exc}")
            return
        state["entry_count"] = count
        metadata = state.get("metadata") or {}
        try:
            updated = self._runtime.download_service.update_collection_probe(
                parent_id,
                title=str(metadata.get("title") or ""),
                source_key=str(metadata.get("source_key") or ""),
                parsed_count=count,
                finished=True,
            )
        except Exception as exc:
            self._on_failed(request_id, f"保存合集完成状态失败：{exc}")
            return
        if not updated:
            self._on_failed(request_id, "合集任务已不存在，已停止解析")
            return
        self.entriesChanged.emit(parent_id)
        options = self._options(state.get("context"))
        if options.collection_mode == "all":
            self._materialize_collection(request_id)
        else:
            self._request_selection(request_id)

    def _request_selection(self, request_id: str) -> None:
        state = self.coordinator.states.get(request_id)
        if state is None or state.get("confirmed") or state.get("selection_shown"):
            return
        state["selection_shown"] = True
        parent_id = str(state.get("parent_id") or "")
        metadata = state.get("metadata") or {}
        self.selectionRequested.emit(parent_id, str(metadata.get("title") or "合集"))

    @Slot(str, result=bool)
    def confirm_selection(self, parent_id: str) -> bool:
        request_id = self._request_id_for_parent(parent_id)
        state = self.coordinator.states.get(request_id)
        if state is None:
            return False
        worker = state.get("worker")
        if isinstance(worker, CollectionProbeWorker):
            try:
                worker.cancel()
            except RuntimeError:
                pass
        return self._materialize_collection(request_id)

    @Slot(str, result=bool)
    def cancel_selection(self, parent_id: str) -> bool:
        request_id = self._request_id_for_parent(parent_id)
        if parent_id and self._runtime.download_service.tasks.get(parent_id) is not None:
            try:
                self._runtime.download_service.delete_task(parent_id, False)
            except Exception as exc:
                self.statusChanged.emit(f"取消合集失败：{exc}")
                return False
        if request_id:
            self.coordinator.cancel(request_id)
        return True

    def _materialize_collection(self, request_id: str) -> bool:
        state = self.coordinator.states.get(request_id)
        if state is None:
            return False
        parent_id = str(state.get("parent_id") or "")
        options = self._options(state.get("context"))
        try:
            nested = self._runtime.db.list_collection_probe_entries(
                parent_id,
                offset=0,
                limit=2**31 - 1,
                selected_only=True,
                entry_kind="collection",
            )
            started = self._runtime.download_service.start_collection_materialization(
                parent_id,
                options.collection_order,
            )
        except Exception as exc:
            message = f"无法加入合集任务：{exc}"
            if state.get("selection_shown"):
                self.statusChanged.emit(message)
            else:
                self._on_failed(request_id, message)
            return False
        state["confirmed"] = True
        for entry in nested:
            self._start_nested_probe(entry, state, parent_id)
        if not self.coordinator.thread_is_running(state.get("thread")):
            self.coordinator.states.pop(request_id, None)
        self.statusChanged.emit(
            "已将选中的合集项目加入下载队列" if started else "未选择可下载项目"
        )
        return True

    def _start_nested_probe(
        self,
        entry: Mapping[str, object],
        state: Mapping[str, object],
        parent_id: str,
    ) -> bool:
        url = str(entry.get("url") or "")
        context = state.get("context")
        request = state.get("request")
        if (
            not parent_id
            or not url
            or not isinstance(context, Mapping)
            or not isinstance(request, CollectionProbeRequest)
        ):
            return False
        return bool(self.start_probe(
            url,
            dict(context),
            parent_collection_task_id=parent_id,
            collection_index=int(entry.get("index") or 0),
            visited_source_keys=set(state.get("visited_source_keys") or ()),
            dedupe_request=request,
        ))

    def _request_id_for_parent(self, parent_id: str) -> str:
        return next(
            (
                request_id
                for request_id, state in self.coordinator.states.items()
                if str(state.get("parent_id") or "") == str(parent_id or "")
                and not state.get("confirmed")
            ),
            "",
        )

    @staticmethod
    def _options(context: object) -> DownloadOptions:
        if not isinstance(context, Mapping):
            return DownloadOptions()
        raw_options = context.get("options_json")
        return DownloadOptions.from_mapping(
            raw_options if isinstance(raw_options, Mapping) else {}
        )

    def _start_pending(self) -> None:
        self.coordinator.start_pending()

    @Slot(str, object)
    def _on_start_error(self, request_id: str, error: Exception) -> None:
        self._on_failed(request_id, f"无法启动合集解析：{error}")
