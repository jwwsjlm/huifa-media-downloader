from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QObject, QThread, Slot
from PySide6.QtWidgets import QLabel, QMessageBox, QStackedWidget, QWidget

from app.core.collection_service import CollectionProbeRequest, CollectionProbeWorker
from app.core.download_options import DownloadOptions
from app.core.download_service import same_storage_volume
from app.core.download_submission import service_task_arguments
from app.core.paths import resolve_portable_path
from app.ui.collection_probe_coordinator import CollectionProbeCoordinator
from app.ui.i18n import format_text as ui_format
from app.ui.i18n import runtime_text, text as ui_text
from app.ui.media_presentation import compact_path_display, format_file_size


class CollectionWorkflowController(QObject):
    """Coordinate collection parsing, durable selection and nested traversal."""

    def __init__(
        self,
        *,
        window: Any,
        selection_view: Any,
        page_stack: QStackedWidget,
        overview_page: QWidget,
        status_label: QLabel,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.window = window
        self.selection_view = selection_view
        self.page_stack = page_stack
        self.overview_page = overview_page
        self.status_label = status_label
        self.active_request_id = ""
        self.coordinator = CollectionProbeCoordinator(
            on_metadata=self._on_metadata,
            on_entries=self._on_entries,
            on_single=self._on_single,
            on_failed=self._on_failed,
            on_finished=self._on_finished,
            on_start_error=self._on_start_error,
            on_slot_released=lambda: self.coordinator.start_pending(),
            parent=self,
            max_concurrent=2,
        )

    @property
    def running(self) -> bool:
        return self.coordinator.running

    def request_shutdown(self) -> None:
        self.coordinator.request_shutdown()

    def request_id_for_parent(self, parent_id: str) -> str:
        return next(
            (
                request_id
                for request_id, state in self.coordinator.states.items()
                if (
                    str(state.get("parent_id") or "") == parent_id
                    and not state.get("confirmed")
                )
            ),
            "",
        )

    def cancel_parent(self, parent_id: str) -> bool:
        request_id = self.request_id_for_parent(parent_id)
        if not request_id:
            return False
        if self.active_request_id == request_id:
            self.active_request_id = ""
            self.page_stack.setCurrentWidget(self.overview_page)
        return self.coordinator.cancel(request_id)

    def resume(self) -> None:
        """Restore collection parsing and durable waiting-selection pages."""

        if self.coordinator.shutdown_requested:
            return
        active_parent_ids = self._active_probe_parent_ids()
        for task in list(self.window.download_service.tasks.values()):
            if not self._task_needs_probe_restore(task, active_parent_ids):
                continue
            self._restore_collection_task(task)
            active_parent_ids.add(str(task.id))

    def _active_probe_parent_ids(self) -> set[str]:
        return {
            str(state.get('parent_id') or '')
            for state in self.coordinator.states.values()
            if self._probe_state_blocks_restore(state)
        }

    def _probe_state_blocks_restore(self, state: Mapping[str, object]) -> bool:
        if not state.get('confirmed'):
            return True
        return self.coordinator.thread_is_running(state.get('thread'))

    @staticmethod
    def _task_needs_probe_restore(task: Any, active_parent_ids: set[str]) -> bool:
        return (
            task.task_kind == 'collection'
            and task.status in {'parsing_collection', 'waiting_selection'}
            and str(task.id) not in active_parent_ids
        )

    @staticmethod
    def _task_options_snapshot(task: Any) -> dict[str, object]:
        options = task.options_json
        return deepcopy(dict(options)) if isinstance(options, Mapping) else {}

    @staticmethod
    def _resume_context(
        task: Any,
        options: DownloadOptions,
        options_snapshot: dict[str, object],
    ) -> dict[str, object]:
        return {
            'output_dir': task.output_dir,
            'proxy': task.proxy,
            'cookie_file': task.cookie_file,
            'cookie_source': task.cookie_source,
            'cookie_browser': task.cookie_browser,
            'cookie_profile': task.cookie_profile,
            'cookie_keyring': task.cookie_keyring,
            'cookie_container': task.cookie_container,
            'quality': task.quality,
            'filename_template': task.filename_template,
            'organize_task_folder': options.organize_task_folder,
            'ffmpeg_path': task.ffmpeg_path,
            'download_album': True,
            'playlist_mode': 'playlist',
            'transcode_codec': task.transcode_codec,
            'transcode_device': task.transcode_device,
            'transcode_encoder': task.transcode_encoder,
            'subtitle_language': task.subtitle_language,
            'prepend_cover_enabled': options.prepend_cover_enabled,
            'prepend_cover_frames': options.prepend_cover_frames,
            'options_json': options_snapshot,
        }

    def _restore_collection_task(self, task: Any) -> None:
        options_snapshot = self._task_options_snapshot(task)
        options = DownloadOptions.from_mapping(options_snapshot)
        context = self._resume_context(task, options, options_snapshot)
        cached_count = max(
            0,
            int(self.window.db.collection_probe_entry_count(task.id) or 0),
        )
        if (
            task.status == 'waiting_selection'
            or bool(options.first_n and cached_count >= options.first_n)
        ):
            self._restore_finished_probe(task, context, options, cached_count)
            return
        self.start_probe(
            task.url,
            context,
            parent_collection_task_id=task.parent_task_id,
            collection_index=task.collection_index,
            existing_parent_id=task.id,
            resume_index=cached_count,
        )

    def _restore_finished_probe(
        self,
        task: Any,
        context: dict[str, object],
        options: DownloadOptions,
        cached_count: int,
    ) -> None:
        request_id = uuid4().hex[:12]
        request = self._build_probe_request(
            request_id,
            task.url,
            context,
            cached_count,
        )
        self.coordinator.states[request_id] = self._restored_probe_state(
            task,
            request,
            context,
            cached_count,
        )
        if task.status != 'waiting_selection':
            self.window.download_service.update_collection_probe(
                task.id,
                title=task.title,
                source_key=task.source_key,
                parsed_count=cached_count,
                finished=True,
            )
        if options.collection_mode == 'all':
            self._on_finished(request_id, True, cached_count)
        elif not self.active_request_id:
            self.show_selection(request_id)
            self.selection_view.set_finished()

    @staticmethod
    def _restored_probe_state(
        task: Any,
        request: CollectionProbeRequest,
        context: dict[str, object],
        cached_count: int,
    ) -> dict[str, object]:
        return {
            'request': request,
            'context': context,
            'url': task.url,
            'entry_count': cached_count,
            'metadata': {'title': task.title, 'source_key': task.source_key},
            'parent_id': task.id,
            'thread': None,
            'worker': None,
            'confirmed': False,
            'parent_collection_task_id': task.parent_task_id,
            'collection_index': task.collection_index,
            'visited_source_keys': {task.source_key} if task.source_key else set(),
            'source_key_registered': bool(task.source_key),
            'finished': True,
        }

    def start_probe(
        self,
        url: str,
        context: dict[str, object],
        *,
        parent_collection_task_id: str = '',
        collection_index: int = 0,
        visited_source_keys: set[str] | None = None,
        existing_parent_id: str = '',
        resume_index: int = 0,
        dedupe_request: CollectionProbeRequest | None = None,
    ) -> bool:
        if self.coordinator.shutdown_requested:
            return False
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
                int(self.window.db.collection_probe_entry_count(parent_id) or 0),
            )
            parent_task = self.window.download_service.tasks.get(parent_id)
            state = self._initial_probe_state(
                request=request,
                context=context,
                url=url,
                cached_count=cached_count,
                parent_id=parent_id,
                parent_task=parent_task,
                parent_collection_task_id=parent_collection_task_id,
                collection_index=collection_index,
                visited_source_keys=visited_source_keys,
            )
        except Exception:
            if created_parent:
                self.window.download_service.delete_task(parent_id, False)
            raise
        if not self.coordinator.enqueue(request_id, state):
            if created_parent:
                self.window.download_service.delete_task(parent_id, False)
            return False
        self.status_label.setText(ui_text('Parsing the submitted URL in the background…'))
        self.coordinator.start_pending()
        return True

    def _build_probe_request(
        self,
        request_id: str,
        url: str,
        context: Mapping[str, object],
        resume_index: int,
        dedupe_request: CollectionProbeRequest | None = None,
    ) -> CollectionProbeRequest:
        raw_options = context.get('options_json')
        options = dict(raw_options) if isinstance(raw_options, Mapping) else {}
        if dedupe_request is None:
            completed_source_keys, completed_urls, completed_titles = (
                self.window.db.completed_media_identities()
            )
        else:
            completed_source_keys = dedupe_request.completed_source_keys
            completed_urls = dedupe_request.completed_urls
            completed_titles = dedupe_request.completed_titles
        return CollectionProbeRequest(
            request_id=request_id,
            url=url,
            core_mode=self.window.download_service.ytdlp_core_mode,
            proxy=str(context.get('proxy') or ''),
            cookie_file=str(context.get('cookie_file') or ''),
            cookie_source=str(context.get('cookie_source') or 'none'),
            cookie_browser=str(context.get('cookie_browser') or 'chrome'),
            cookie_profile=str(context.get('cookie_profile') or ''),
            cookie_keyring=str(context.get('cookie_keyring') or ''),
            cookie_container=str(context.get('cookie_container') or ''),
            deno_path=self.window.download_service.deno_path,
            ytdlp_ejs_source=self.window.download_service.ytdlp_ejs_source,
            options=deepcopy(options),
            completed_source_keys=set(completed_source_keys),
            completed_urls=set(completed_urls),
            completed_titles=set(completed_titles),
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
        parent_collection = self.window.download_service.tasks.get(parent_collection_task_id)
        parent_id = str(self.window.download_service.create_collection(
            url,
            str(context['output_dir']),
            title=ui_text('Parsing Collection'),
            parent_task_id=parent_collection_task_id,
            root_task_id=(parent_collection.root_task_id if parent_collection else ''),
            collection_index=collection_index,
            **service_task_arguments(dict(context), playlist_mode='playlist'),
        ) or '')
        if not parent_id:
            raise RuntimeError(ui_text('The collection parent task could not be created.'))
        return parent_id, True

    @staticmethod
    def _initial_probe_state(
        *,
        request: CollectionProbeRequest,
        context: Mapping[str, object],
        url: str,
        cached_count: int,
        parent_id: str,
        parent_task: Any,
        parent_collection_task_id: str,
        collection_index: int,
        visited_source_keys: set[str] | None,
    ) -> dict[str, object]:
        return {
            'request': request,
            'context': deepcopy(dict(context)),
            'url': url,
            'entry_count': cached_count,
            'metadata': {
                'title': parent_task.title if parent_task is not None else '',
                'source_key': parent_task.source_key if parent_task is not None else '',
            },
            'parent_id': parent_id,
            'thread': None,
            'worker': None,
            'confirmed': False,
            'parent_collection_task_id': parent_collection_task_id,
            'collection_index': collection_index,
            'visited_source_keys': set(visited_source_keys or ()),
            'source_key_registered': False,
        }

    def _on_start_error(
        self,
        request_id: str,
        error: Exception,
    ) -> None:
        self._on_failed(
            request_id,
            ui_format(
                'Unable to start collection parsing: {error}',
                error=runtime_text(error),
            ),
        )

    def _ensure_parent(self, request_id: str) -> str:
        state = self.coordinator.states.get(request_id)
        if not state:
            return ''
        parent_id = str(state.get('parent_id') or '')
        context = state['context']
        metadata = state.get('metadata') or {}
        source_key = str(metadata.get('source_key') or '')
        visited = set(state.get('visited_source_keys') or ())
        if source_key and not state.get('source_key_registered'):
            if source_key in visited:
                self._on_failed(request_id, ui_text('A collection loop was detected and stopped.'))
                state['confirmed'] = True
                worker = state.get('worker')
                if isinstance(worker, CollectionProbeWorker):
                    worker.cancel()
                return ''
            visited.add(source_key)
            state['visited_source_keys'] = visited
            state['source_key_registered'] = True
        if parent_id:
            return parent_id
        parent_collection_id = str(state.get('parent_collection_task_id') or '')
        parent_collection = self.window.download_service.tasks.get(parent_collection_id)
        parent_id = self.window.download_service.create_collection(
            str(state['url']),
            str(context['output_dir']),
            title=str(metadata.get('title') or ui_text('Parsing Collection')),
            source_key=source_key,
            parent_task_id=parent_collection_id,
            root_task_id=(parent_collection.root_task_id if parent_collection else ''),
            collection_index=int(state.get('collection_index') or 0),
            **service_task_arguments(context, playlist_mode='playlist'),
        )
        state['parent_id'] = parent_id
        return parent_id

    @Slot(str, object)
    def _on_metadata(self, request_id: str, metadata: object) -> None:
        state = self.coordinator.result_state(request_id)
        if state is None or not isinstance(metadata, dict):
            return
        state['metadata'] = dict(metadata)
        parent_id = self._ensure_parent(request_id)
        if parent_id:
            self.window.download_service.update_collection_probe(
                parent_id,
                title=str(metadata.get('title') or ''),
                source_key=str(metadata.get('source_key') or ''),
                parsed_count=int(state.get('entry_count') or 0),
            )
        if self.active_request_id == request_id:
            self.selection_view.set_metadata(metadata)

    @Slot(str, object)
    def _on_entries(self, request_id: str, payload: object) -> None:
        state = self.coordinator.result_state(request_id)
        if state is None or not isinstance(payload, list):
            return
        entries = [dict(item) for item in payload if isinstance(item, dict)]
        parent_id = self._ensure_parent(request_id)
        if not parent_id:
            return
        old_count = int(state.get('entry_count') or 0)
        self.window.db.upsert_on_entries(parent_id, entries)
        current_count = self.window.db.collection_probe_entry_count(parent_id)
        state['entry_count'] = current_count
        self.window.download_service.update_collection_probe(
            parent_id,
            title=str((state.get('metadata') or {}).get('title') or ''),
            source_key=str((state.get('metadata') or {}).get('source_key') or ''),
            parsed_count=current_count,
        )
        options = DownloadOptions.from_mapping(state['context'].get('options_json'))
        if not self.active_request_id and options.collection_mode == 'select':
            self.show_selection(request_id)
        if self.active_request_id == request_id:
            if current_count > old_count:
                self.selection_view.append_entries(entries[-(current_count - old_count):])

    def show_selection(self, request_id: str) -> None:
        state = self.coordinator.states.get(request_id)
        if not state:
            return
        self.active_request_id = request_id
        metadata = state.get('metadata') or {}
        self.selection_view.reset(str(metadata.get('title') or ui_text('Parsing Collection')))
        self.selection_view.set_metadata(metadata)
        parent_id = str(state.get('parent_id') or '')
        count = self.window.db.collection_probe_entry_count(parent_id)
        self.selection_view.set_paged_entries(
            count,
            lambda offset, limit, parent_id=parent_id: self.window.db.list_on_entries(
                parent_id, offset=offset, limit=limit
            ),
            selection_updater=lambda index, selected, parent_id=parent_id: self.window.db.set_collection_probe_entry_selected(
                parent_id, index, selected
            ),
            selection_setter=lambda mode, parent_id=parent_id: self.window.db.set_collection_probe_selection(
                parent_id, mode
            ),
            selected_loader=lambda parent_id=parent_id: self.window.db.list_on_entries(
                parent_id, offset=0, limit=2**31 - 1, selected_only=True
            ),
            selected_counter=lambda parent_id=parent_id: self.window.db.count_selected_on_entries(
                parent_id
            ),
            view_loader=lambda offset, limit, view, parent_id=parent_id: self.window.db.list_on_entries(
                parent_id,
                offset=offset,
                limit=limit,
                query=str(view.get('query') or ''),
                state=str(view.get('state') or 'all'),
                date_after=str(view.get('date_after') or ''),
                date_before=str(view.get('date_before') or ''),
                duration_min=int(view.get('duration_min') or 0),
                duration_max=int(view.get('duration_max') or 0),
                sort_column=str(view.get('sort_column') or 'collection_index'),
                sort_descending=bool(view.get('sort_descending')),
            ),
            view_counter=lambda view, parent_id=parent_id: self.window.db.collection_probe_entry_count(
                parent_id,
                query=str(view.get('query') or ''),
                state=str(view.get('state') or 'all'),
                date_after=str(view.get('date_after') or ''),
                date_before=str(view.get('date_before') or ''),
                duration_min=int(view.get('duration_min') or 0),
                duration_max=int(view.get('duration_max') or 0),
            ),
        )
        self.selection_view.set_storage_preview_provider(
            lambda parent_id=parent_id, state=state: self._storage_preview_text(
                parent_id, state
            )
        )
        self.page_stack.setCurrentWidget(self.selection_view)

    def _storage_preview_text(self, parent_id: str, state: Mapping[str, object]) -> str:
        summary = self.window.db.collection_probe_storage_summary(parent_id)
        selected = int(summary.get('selected_count') or 0)
        known = int(summary.get('known_count') or 0)
        estimated = int(summary.get('estimated_bytes') or 0)
        context = state.get('context') if isinstance(state, Mapping) else {}
        if not isinstance(context, Mapping):
            context = {}
        options = DownloadOptions.from_mapping(context.get('options_json'))
        final_dir = resolve_portable_path(str(context.get('output_dir') or '.'))
        temporary_dir = (
            resolve_portable_path(options.processing_temp_dir)
            if options.processing_temp_dir else final_dir
        )
        route = (
            ui_text('Cross-disk transfer after processing')
            if not same_storage_volume(temporary_dir, final_dir)
            else ui_text('Same-disk processing')
        )
        path_text = ui_format(
            'Temporary path {temporary} · Final path {final}',
            temporary=compact_path_display(str(temporary_dir)),
            final=compact_path_display(str(final_dir)),
        )
        if not selected:
            return ui_format(
                'No items selected · {paths} · {route}',
                paths=path_text,
                route=route,
            )
        if estimated > 0:
            estimate_text = ui_format(
                'Known final size {size} ({known}/{selected} item(s) estimated)',
                size=format_file_size(estimated),
                known=known,
                selected=selected,
            )
            if known < selected:
                estimate_text += ui_text(' · Items with unknown size are not included; actual usage may be higher.')
        else:
            estimate_text = ui_text(
                'Size information is not available yet; required storage will be checked before each download.',
            )
        return ui_format(
            '{selected} item(s) selected · {estimate} · {paths} · {route}',
            selected=selected,
            estimate=estimate_text,
            paths=path_text,
            route=route,
        )

    @staticmethod
    def _ordered_entries(
        entries: list[dict[str, object]],
        options: DownloadOptions,
    ) -> list[dict[str, object]]:
        ordered = list(entries)
        if options.collection_order == 'reverse':
            ordered.reverse()
        elif options.collection_order == 'random':
            import random
            random.shuffle(ordered)
        return ordered

    @Slot(str, object)
    def _on_single(self, request_id: str, info: object) -> None:
        state = self.coordinator.result_state(request_id)
        if state is None:
            return
        parent_id = str(state.get('parent_id') or '')
        metadata = state.get('metadata') or {}
        single_info = info if isinstance(info, dict) else {}
        resolved = self.window.download_service.resolve_collection_as_video(
            parent_id,
            title=str(single_info.get('title') or metadata.get('title') or ''),
            source_key=str(metadata.get('source_key') or ''),
        )
        if not resolved:
            self._on_failed(
                request_id,
                ui_text('The parsed single-video task could not be started.'),
            )
            return
        state['confirmed'] = True
        if self.active_request_id == request_id:
            self.active_request_id = ''
            self.page_stack.setCurrentWidget(self.overview_page)

    @Slot(str, str)
    def _on_failed(self, request_id: str, error: str) -> None:
        state = self.coordinator.result_state(request_id)
        if state is None:
            return
        parent_id = str(state.get('parent_id') or '')
        if parent_id and parent_id in self.window.download_service.tasks:
            self.window.download_service.fail_collection_probe(parent_id, error)
        else:
            QMessageBox.warning(
                self.selection_view,
                ui_text('Unable to Parse URL'),
                runtime_text(error),
            )
        state['confirmed'] = True
        if self.active_request_id == request_id:
            self.active_request_id = ''
            self.page_stack.setCurrentWidget(self.overview_page)

    def _start_nested_probe(
        self,
        entry: Mapping[str, object],
        state: Mapping[str, object],
        parent_id: str,
    ) -> bool:
        url = str(entry.get('url') or '')
        context = state.get('context')
        request = state.get('request')
        if (
            not parent_id
            or not url
            or not isinstance(context, Mapping)
            or not isinstance(request, CollectionProbeRequest)
        ):
            return False
        return self.start_probe(
            url,
            dict(context),
            parent_collection_task_id=parent_id,
            collection_index=int(entry.get('index') or 0),
            visited_source_keys=set(state.get('visited_source_keys') or ()),
            dedupe_request=request,
        )

    @Slot(str, bool, int)
    def _on_finished(self, request_id: str, is_collection: bool, _count: int) -> None:
        state = self.coordinator.result_state(request_id)
        if state is None:
            return
        if not is_collection:
            return
        parent_id = self._ensure_parent(request_id)
        if not parent_id:
            return
        persisted_count = max(
            0,
            int(self.window.db.collection_probe_entry_count(parent_id) or 0),
        )
        state['entry_count'] = persisted_count
        self.window.download_service.update_collection_probe(
            parent_id,
            title=str((state.get('metadata') or {}).get('title') or ''),
            source_key=str((state.get('metadata') or {}).get('source_key') or ''),
            parsed_count=persisted_count,
            finished=True,
        )
        options = DownloadOptions.from_mapping(state['context'].get('options_json'))
        if options.collection_mode == 'all':
            nested = self.window.db.list_on_entries(
                parent_id,
                offset=0,
                limit=2**31 - 1,
                selected_only=True,
                entry_kind='collection',
            )
            state['confirmed'] = True
            self.window.download_service.start_collection_materialization(
                parent_id,
                options.collection_order,
            )
            for entry in nested:
                self._start_nested_probe(entry, state, parent_id)
            if self.active_request_id == request_id:
                self.active_request_id = ''
                self.page_stack.setCurrentWidget(self.overview_page)
            thread = state.get('thread')
            if not self.coordinator.thread_is_running(thread):
                self.coordinator.states.pop(request_id, None)
        elif self.active_request_id == request_id:
            self.selection_view.set_finished()
        elif not self.active_request_id:
            self.show_selection(request_id)
            self.selection_view.set_finished()

    @Slot(object)
    def confirm_selection(self, selected: object) -> None:
        request_id = self.active_request_id
        state = self.coordinator.states.get(request_id)
        if not state or (selected is not None and not isinstance(selected, list)):
            return
        worker = state.get('worker')
        if isinstance(worker, CollectionProbeWorker):
            worker.cancel()
        parent_id = self._ensure_parent(request_id)
        if not parent_id:
            return
        state['confirmed'] = True
        options = DownloadOptions.from_mapping(state['context'].get('options_json'))
        if selected is None:
            nested_entries = self.window.db.list_on_entries(
                parent_id,
                offset=0,
                limit=2**31 - 1,
                selected_only=True,
                entry_kind='collection',
            )
            self.window.download_service.start_collection_materialization(
                parent_id,
                options.collection_order,
            )
        else:
            selected_entries = [dict(item) for item in selected if isinstance(item, dict)]
            nested_entries = [entry for entry in selected_entries if entry.get('entry_kind') == 'collection']
            ordered = self._ordered_entries(
                [entry for entry in selected_entries if entry.get('entry_kind') == 'video'],
                options,
            )
            self.window.download_service.enqueue_collection_entries(parent_id, ordered)
        for entry in nested_entries:
            self._start_nested_probe(entry, state, parent_id)
        self.active_request_id = ''
        self.page_stack.setCurrentWidget(self.overview_page)
        thread = state.get('thread')
        if not self.coordinator.thread_is_running(thread):
            self.coordinator.states.pop(request_id, None)

    @Slot()
    def cancel_selection(self) -> None:
        request_id = self.active_request_id
        state = self.coordinator.states.get(request_id)
        if state:
            parent_id = str(state.get('parent_id') or '')
            self.coordinator.cancel(request_id)
            if parent_id:
                self.window.download_service.delete_task(parent_id, False)
        self.active_request_id = ''
        self.page_stack.setCurrentWidget(self.overview_page)

    @Slot(object)
    def parse_nested(self, entry: object) -> None:
        if not isinstance(entry, dict) or not entry.get('url'):
            return
        state = self.coordinator.states.get(self.active_request_id)
        if state:
            parent_id = self._ensure_parent(self.active_request_id)
            self._start_nested_probe(entry, state, parent_id)
