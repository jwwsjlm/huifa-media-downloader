from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import Any

from app.core.cookie_sources import (
    normalize_cookie_browser,
    normalize_cookie_source,
)


class TaskAuthActionController:
    """Start durable tasks with the authentication selected right now."""

    def __init__(
        self,
        service: Any,
        settings: Any,
        database: Callable[[], Any],
        resume_collection_probes: Callable[[], None],
    ) -> None:
        self._service = service
        self._settings = settings
        self._database = database
        self._resume_collection_probes = resume_collection_probes

    def _task_tree(self, task_id: str) -> list[Any]:
        service = self._service
        root = service.tasks.get(task_id)
        if root is None:
            return []
        result: list[Any] = []
        pending = deque([root])
        seen: set[str] = set()
        while pending:
            task = pending.popleft()
            current_id = str(task.id or "")
            if not current_id or current_id in seen:
                continue
            seen.add(current_id)
            result.append(task)
            if task.task_kind != "collection":
                continue
            children = service.collection_children(current_id)
            pending.extend(child for child in children if child is not None)
        return result

    def apply_current_auth(self, task_id: str) -> bool:
        tasks = self._task_tree(task_id)
        if not tasks:
            return False
        settings = self._settings
        source = normalize_cookie_source(settings.get("download_cookie_source"))
        cookie_file = str(
            settings.get_resolved_path("download_cookie_file") or ""
        )
        browser = normalize_cookie_browser(settings.get("download_cookie_browser"))
        profile = str(settings.get("download_cookie_profile") or "").strip()
        keyring = str(settings.get("download_cookie_keyring") or "").strip()
        container = str(settings.get("download_cookie_container") or "").strip()
        for task in tasks:
            task.cookie_source = source
            task.cookie_file = cookie_file
            task.cookie_browser = browser
            task.cookie_profile = profile
            task.cookie_keyring = keyring
            task.cookie_container = container
        return True

    def resume(self, task_id: str) -> None:
        if self.apply_current_auth(task_id):
            self._service.resume(task_id)

    def retry(self, task_id: str) -> None:
        if not self.apply_current_auth(task_id):
            return
        service = self._service
        task = service.tasks.get(task_id)
        if task is None:
            return
        if task.task_kind == "collection" and task.status == "failed":
            children = service.collection_children(task_id)
            if not children:
                database = self._database()
                try:
                    parsed_count = int(
                        database.collection_probe_entry_count(task_id)
                    )
                except (OSError, TypeError, ValueError):
                    parsed_count = 0
                task.error = ""
                service.update_collection_probe(
                    task_id,
                    title=task.title,
                    source_key=task.source_key,
                    parsed_count=max(0, parsed_count),
                    finished=False,
                )
                self._resume_collection_probes()
                return
        service.retry(task_id)

    def start(self, task_id: str) -> None:
        task = self._service.tasks.get(task_id)
        if task is None:
            return
        if task.status == "paused":
            self.resume(task_id)
        elif task.status in {"failed", "partial_failed", "canceled"}:
            self.retry(task_id)
        elif self.apply_current_auth(task_id):
            self._service.start_task(task_id)

    def redownload(
        self,
        task_id: str,
        quality_override: str | None = None,
    ) -> str | None:
        if not self.apply_current_auth(task_id):
            return None
        return self._service.redownload(
            task_id,
            quality_override=quality_override,
        )


__all__ = ["TaskAuthActionController"]
