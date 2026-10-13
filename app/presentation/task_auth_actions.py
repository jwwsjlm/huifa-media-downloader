from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.core.cookie_sources import (
    CookieSource,
    normalize_cookie_browser,
    normalize_cookie_source,
)


class TaskAuthActionController:
    """Start durable tasks with the authentication selected right now."""

    def __init__(
        self,
        service: Any,
        settings: Any,
        resume_collection_probes: Callable[[], None],
    ) -> None:
        self._service = service
        self._settings = settings
        self._resume_collection_probes = resume_collection_probes

    def apply_current_auth(self, task_id: str) -> bool:
        settings = self._settings
        return self._service.set_task_authentication(
            task_id,
            CookieSource(
                source=normalize_cookie_source(settings.get("download_cookie_source")),
                file=str(settings.get_resolved_path("download_cookie_file") or ""),
                browser=normalize_cookie_browser(settings.get("download_cookie_browser")),
                profile=str(settings.get("download_cookie_profile") or "").strip(),
                keyring=str(settings.get("download_cookie_keyring") or "").strip(),
                container=str(settings.get("download_cookie_container") or "").strip(),
            ),
        )

    def resume(self, task_id: str) -> None:
        if self.apply_current_auth(task_id):
            self._service.resume(task_id)

    def retry(self, task_id: str) -> None:
        if not self.apply_current_auth(task_id):
            return
        service = self._service
        if service.restart_collection_probe(task_id):
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
