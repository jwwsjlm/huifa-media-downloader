from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from PySide6.QtGui import QAction
from PySide6.QtWidgets import QApplication, QLabel, QMenu, QMessageBox, QWidget

from app.core.download_service import DownloadTask
from app.core.transcode_service import normalize_transcode_encoder
from app.ui.download_control_presentation import transcode_encoder_label
from app.ui.download_dialogs import DownloadLogDialog
from app.ui.i18n import format_text as ui_format
from app.ui.i18n import text as ui_text


def read_download_task_ids(database_path: str | Path) -> set[str] | None:
    """Read the durable task IDs, preserving read failure as an unknown state."""

    path = Path(database_path)
    if not path.exists():
        # The database may be between two atomic replacement steps. Absence
        # is therefore an unknown state, not proof that every task was deleted.
        return None
    connection = None
    try:
        connection = sqlite3.connect(
            f"file:{path.resolve().as_posix()}?mode=ro",
            uri=True,
            timeout=0.2,
        )
        rows = connection.execute("SELECT id FROM download_tasks").fetchall()
        return {str(row[0]) for row in rows}
    except (OSError, sqlite3.Error):
        return None
    finally:
        if connection is not None:
            connection.close()


@dataclass(frozen=True, slots=True)
class TaskMenuCapabilities:
    pause_mode: str = ""
    can_cancel: bool = False
    can_retry: bool = False
    can_custom_redownload: bool = False
    can_convert: bool = False


@dataclass(frozen=True, slots=True)
class TaskContextActions:
    copy_link: QAction
    copy_folder: QAction
    view_log: QAction
    open_task: QAction
    delete_task: QAction
    pause_or_resume: QAction | None = None
    cancel: QAction | None = None
    retry: QAction | None = None
    custom_redownload: QAction | None = None
    convert: QAction | None = None


def task_menu_capabilities(task: DownloadTask) -> TaskMenuCapabilities:
    """Return context-menu actions valid for the persisted task state."""

    is_collection = task.task_kind == "collection"
    pause_mode = ""
    if task.status in {"downloading", "queued"} and is_collection:
        pause_mode = "pause_collection"
    elif task.status == "downloading":
        pause_mode = "pause_download"
    elif task.status == "paused":
        pause_mode = "resume_collection" if is_collection else "resume_download"

    if is_collection:
        can_retry = task.status in {"failed", "partial_failed", "canceled", "paused"}
    else:
        can_retry = task.status in {"failed", "canceled", "completed", "paused", "deleted"}

    media_file_exists = bool(task.media_path) and Path(task.media_path).is_file()
    return TaskMenuCapabilities(
        pause_mode=pause_mode,
        can_cancel=task.status in {
            "queued",
            "downloading",
            "processing",
            "parsing_collection",
            "暂停中",
            "waiting_selection",
        },
        can_retry=can_retry,
        can_custom_redownload=(
            not is_collection
            and task.status in {"failed", "canceled", "completed", "paused", "deleted"}
        ),
        can_convert=(
            not is_collection
            and task.status == "completed"
            and media_file_exists
        ),
    )


class TaskContextMenuController:
    """Build and execute task context menus outside the dashboard widget."""

    def __init__(
        self,
        *,
        parent: QWidget,
        window: Any,
        status_label: QLabel,
        cancel_task: Callable[[str], None],
        resume_task: Callable[[str], None],
        retry_task: Callable[[str], None],
        redownload_task: Callable[[str, str | None], str | None],
        open_collection: Callable[[str], None],
        open_folder: Callable[[str], None],
        delete_tasks: Callable[[list[str]], None],
    ) -> None:
        self.parent = parent
        self.window = window
        self.status_label = status_label
        self.cancel_task = cancel_task
        self.resume_task = resume_task
        self.retry_task = retry_task
        self.redownload_task = redownload_task
        self.open_collection = open_collection
        self.open_folder = open_folder
        self.delete_tasks = delete_tasks

    def show(self, task: DownloadTask, global_pos) -> None:
        menu, actions = self.build(task)
        chosen = menu.exec(global_pos)
        if chosen is not None:
            self.execute(task, actions, chosen)

    def build(self, task: DownloadTask) -> tuple[QMenu, TaskContextActions]:
        menu = QMenu(self.parent)
        is_collection = task.task_kind == "collection"
        capabilities = task_menu_capabilities(task)
        copy_link_action = menu.addAction(
            ui_text("Copy Collection URL")
            if is_collection
            else ui_text("Copy Video URL")
        )
        copy_folder_action = menu.addAction(
            ui_text("Copy Collection Folder Path")
            if is_collection
            else ui_text("Copy Video Folder Path")
        )
        log_action = menu.addAction(ui_text("View Download Log"))
        menu.addSeparator()

        pause_labels = {
            "pause_collection": ui_text("Pause Collection"),
            "pause_download": ui_text("Pause Download"),
            "resume_collection": ui_text("Resume Collection"),
            "resume_download": ui_text("Resume Download"),
        }
        pause_action = (
            menu.addAction(pause_labels[capabilities.pause_mode])
            if capabilities.pause_mode
            else None
        )
        cancel_action = (
            menu.addAction(ui_text("Cancel Task"))
            if capabilities.can_cancel
            else None
        )
        retry_action = (
            menu.addAction(
                ui_text("Retry Failed Items")
                if is_collection
                else ui_text("Download Again")
            )
            if capabilities.can_retry
            else None
        )
        custom_action = (
            menu.addAction(ui_text("Choose Resolution and Download Again"))
            if capabilities.can_custom_redownload
            else None
        )
        convert_action = (
            menu.addAction(ui_text("Convert Format"))
            if capabilities.can_convert
            else None
        )
        menu.addSeparator()
        open_action = menu.addAction(
            ui_text("View Collection Details")
            if is_collection
            else ui_text("Open Video Folder")
        )
        delete_action = menu.addAction(ui_text("Delete Task"))
        return menu, TaskContextActions(
            copy_link=copy_link_action,
            copy_folder=copy_folder_action,
            view_log=log_action,
            pause_or_resume=pause_action,
            cancel=cancel_action,
            retry=retry_action,
            custom_redownload=custom_action,
            convert=convert_action,
            open_task=open_action,
            delete_task=delete_action,
        )

    def execute(
        self,
        task: DownloadTask,
        actions: TaskContextActions,
        chosen: QAction,
    ) -> None:
        is_collection = task.task_kind == "collection"
        service = self.window.download_service
        if chosen is actions.copy_link:
            QApplication.clipboard().setText(task.url)
            self.status_label.setText(
                ui_text("Collection URL copied")
                if is_collection
                else ui_text("Video URL copied")
            )
        elif chosen is actions.copy_folder:
            folder = str(
                Path(task.media_path).parent
                if task.media_path
                else Path(task.output_dir)
            )
            QApplication.clipboard().setText(folder)
            self.status_label.setText(
                ui_text("Collection folder path copied")
                if is_collection
                else ui_text("Video folder path copied")
            )
        elif chosen is actions.view_log:
            DownloadLogDialog(task, service.logs, self.parent).exec()
        elif actions.pause_or_resume is not None and chosen is actions.pause_or_resume:
            if task.status in {"downloading", "queued"}:
                service.pause(task.id)
            else:
                self.resume_task(task.id)
        elif actions.cancel is not None and chosen is actions.cancel:
            self.cancel_task(task.id)
        elif actions.retry is not None and chosen is actions.retry:
            if is_collection:
                self.retry_task(task.id)
            else:
                self.confirm_redownload(task)
        elif actions.custom_redownload is not None and chosen is actions.custom_redownload:
            self.confirm_redownload(task, quality_override="custom")
        elif actions.convert is not None and chosen is actions.convert:
            encoder = normalize_transcode_encoder(
                self.window.app_settings.get("transcode_encoder")
            )
            started = service.convert_completed_task(
                task.id,
                encoder,
                ffmpeg_path=self.window.app_settings.get("ffmpeg_path"),
                ffprobe_path=self.window.app_settings.get("ffprobe_path"),
            )
            if started and encoder != "original":
                self.status_label.setText(
                    ui_format(
                        "Format conversion started with {encoder}.",
                        encoder=transcode_encoder_label(encoder),
                    )
                )
        elif chosen is actions.open_task:
            if is_collection:
                self.open_collection(task.id)
            else:
                self.open_folder(task.id)
        elif chosen is actions.delete_task:
            self.delete_tasks([task.id])

    def confirm_redownload(
        self,
        task: DownloadTask,
        quality_override: str | None = None,
    ) -> None:
        quality_text = (
            ui_text("Custom resolution")
            if quality_override == "custom"
            else ui_text("Current download settings")
        )
        box = QMessageBox(self.parent)
        box.setWindowTitle(ui_text("Confirm Download Again"))
        box.setText(
            ui_text(
                "Downloading again creates a new task record; the original task is not deleted."
            )
        )
        box.setInformativeText(
            ui_format(
                "Download settings: {quality}\nContinue?",
                quality=quality_text,
            )
        )
        yes = box.addButton(ui_text("Create Download Task"), QMessageBox.AcceptRole)
        box.addButton(ui_text("Cancel"), QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is yes:
            self.redownload_task(
                task.id,
                quality_override,
            )
