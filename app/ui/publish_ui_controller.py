from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QWidget

from app.storage.models import MediaItem
from app.ui.i18n import format_text as ui_format
from app.ui.i18n import runtime_text, text as ui_text
from app.ui.media_presentation import platform_label
from app.ui.navigation import navigation_icon
from app.ui.publish_editor import PublishPage


class PublishUiController(QObject):
    """Coordinate publishing navigation, live rows and editor ownership."""

    def __init__(self, window: Any) -> None:
        super().__init__(window)
        self.window = window
        self._editors: dict[int, QWidget] = {}

    def status_changed(self, task_id: int, status: str, result: str) -> None:
        selected_task_id = int(task_id or 0)
        try:
            row = self.window.db.get_publish_task(selected_task_id)
        except Exception as exc:
            self._mark_views_dirty()
            self.window.statusBar().showMessage(
                ui_format(
                    "Publishing status could not be refreshed: {error}",
                    error=runtime_text(exc),
                ),
                5000,
            )
            return

        if row is None:
            # A deleted row is authoritative. Do not let PublishQueuePage
            # query the database a second time using ``row=None`` semantics.
            self._mark_views_dirty()
            return

        try:
            self.window.publish_queue.refresh_task(selected_task_id, row)
        except Exception as exc:
            self._safe_mark_dirty(self.window.publish_queue)
            self._show_refresh_error(exc)

        try:
            self.window.completed.refresh_media_distribution(
                int(row["media_id"] or 0),
            )
        except Exception as exc:
            self._safe_mark_dirty(self.window.completed)
            self._show_refresh_error(exc)

        if status not in {"success", "failed"}:
            return
        try:
            platform_key = str(row["platform"] or "")
            platform_name = (
                platform_label(platform_key)
                if platform_key
                else ui_text("Publishing Platform")
            )
            self.window.desktop_notification_controller.publish_finished(
                selected_task_id,
                status,
                result,
                row,
                platform_name,
            )
        except Exception as exc:
            self._show_refresh_error(exc)

    def open_editor(
        self,
        media: MediaItem,
        preselected_platforms: tuple[str, ...] = (),
    ) -> QWidget:
        media_id = int(media.id or 0)
        existing = self._editors.get(media_id) if media_id > 0 else None
        if existing is not None:
            try:
                existing_index = self.window.tabs.indexOf(existing)
            except RuntimeError:
                existing_index = -1
            if existing_index >= 0:
                self.window.tabs.setCurrentWidget(existing)
                return existing
        if media_id > 0:
            self._editors.pop(media_id, None)

        page = PublishPage(
            self.window,
            media,
            tuple(preselected_platforms),
        )
        if media_id > 0:
            self._editors[media_id] = page
            page.destroyed.connect(
                lambda _object=None, selected_id=media_id, expected_id=id(page):
                self._editor_destroyed(selected_id, expected_id)
            )
        self.window.tabs.addTab(
            page,
            ui_text("Publish Editor"),
            navigation_icon("editor"),
        )
        self.window.tabs.setCurrentWidget(page)
        return page

    def focus_queue(self, media: MediaItem) -> None:
        try:
            self.window.publish_queue.focus_media(media)
        finally:
            self.window.tabs.setCurrentWidget(self.window.publish_queue)

    def complete_editor(self, editor: QWidget) -> None:
        self._mark_views_dirty()
        try:
            self.window.tabs.setCurrentWidget(self.window.publish_queue)
            index = self.window.tabs.indexOf(editor)
            if index >= 0:
                self.window.tabs.removeTab(index)
        except RuntimeError:
            pass
        for media_id, page in tuple(self._editors.items()):
            if page is editor:
                self._editors.pop(media_id, None)
        try:
            editor.deleteLater()
        except RuntimeError:
            pass

    def _editor_destroyed(self, media_id: int, expected_id: int) -> None:
        current = self._editors.get(media_id)
        if current is not None and id(current) == expected_id:
            self._editors.pop(media_id, None)

    def _mark_views_dirty(self) -> None:
        for view in (self.window.publish_queue, self.window.completed):
            self._safe_mark_dirty(view)

    @staticmethod
    def _safe_mark_dirty(view: Any) -> None:
        try:
            view.mark_dirty()
        except Exception:
            # Visible pages may immediately refresh when marked dirty. If the
            # database itself is the failing dependency, retaining ``dirty``
            # where possible avoids recursively surfacing the same exception.
            try:
                view.dirty = True
            except Exception:
                pass

    def _show_refresh_error(self, error: Exception) -> None:
        try:
            self.window.statusBar().showMessage(
                ui_format(
                    "Publishing status could not be refreshed: {error}",
                    error=runtime_text(error),
                ),
                5000,
            )
        except RuntimeError:
            pass


__all__ = ["PublishUiController"]
