from __future__ import annotations

import traceback
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QMessageBox, QPushButton, QWidget

from app.core.download_links import extract_download_links
from app.core.download_options import DownloadOptions
from app.core.download_submission import (
    DOWNLOAD_SUBMIT_DEBOUNCE_SECONDS,
    DownloadSubmissionDebouncer,
    DownloadSubmissionSettingsError,
    build_download_request_context,
    service_task_arguments,
    submission_playlist_mode,
)
from app.ui.i18n import format_text as ui_format
from app.ui.i18n import runtime_text, text as ui_text


MAX_BATCH_DOWNLOAD_LINKS = 100


class DownloadSubmissionWorkflow:
    """Own URL intake, duplicate suppression and task publication feedback."""

    def __init__(
        self,
        *,
        parent: QWidget,
        window: Any,
        url_input: QLineEdit,
        add_button: QPushButton,
        paste_button: QPushButton,
        status_label: QLabel,
        options_provider: Callable[[], Mapping[str, object]],
        acknowledge_submission: Callable[[], None],
        start_collection: Callable[[str, dict[str, object]], bool],
    ) -> None:
        self.parent = parent
        self.window = window
        self.url_input = url_input
        self.add_button = add_button
        self.paste_button = paste_button
        self.status_label = status_label
        self.options_provider = options_provider
        self.acknowledge_submission = acknowledge_submission
        self.start_collection = start_collection
        self.debouncer = DownloadSubmissionDebouncer()

    def release_guard(self) -> None:
        for button in (self.add_button, self.paste_button):
            try:
                button.setEnabled(True)
            except RuntimeError:
                return

    def is_debounced(self, links: list[str]) -> bool:
        if self.debouncer.rejects(links):
            self.status_label.setText(ui_text(
                'This URL batch was just submitted; the duplicate click was ignored.',
            ))
            return True
        self.add_button.setEnabled(False)
        self.paste_button.setEnabled(False)
        QTimer.singleShot(
            int(DOWNLOAD_SUBMIT_DEBOUNCE_SECONDS * 1000),
            self.release_guard,
        )
        return False

    def submit_input(self) -> None:
        raw_text = self.url_input.text().strip()
        if not raw_text:
            if self.debouncer.suppresses_empty_followup():
                return
            QMessageBox.warning(
                self.parent,
                ui_text('Notice'),
                ui_text('Enter a video or playlist URL.'),
            )
            return
        links = extract_download_links(raw_text)
        if not links:
            QMessageBox.warning(
                self.parent,
                ui_text('Invalid URL Format'),
                ui_text(
                    'No complete video or playlist URL was found, for example: https://example.com/video',
                ),
            )
            return
        if not self._validate_batch_size(links, clipboard=False):
            return
        if len(links) == 1:
            self.url_input.setText(links[0])
        self.enqueue_links(links)

    def paste_and_submit(self) -> None:
        try:
            clipboard_text = QApplication.clipboard().text()
        except Exception as exc:
            QMessageBox.warning(
                self.parent,
                ui_text('Cannot Read Clipboard'),
                ui_format(
                    'Failed to read the system clipboard:\n{error}',
                    error=runtime_text(exc),
                ),
            )
            return
        links = extract_download_links(clipboard_text)
        if not links:
            QMessageBox.information(
                self.parent,
                ui_text('No URLs in Clipboard'),
                ui_text(
                    'Copy one or more video or playlist URLs, then click Paste & Download.',
                ),
            )
            return
        if not self._validate_batch_size(links, clipboard=True):
            return
        if len(links) == 1:
            self.url_input.setText(links[0])
        self.enqueue_links(links)

    def _validate_batch_size(self, links: list[str], *, clipboard: bool) -> bool:
        if len(links) <= MAX_BATCH_DOWNLOAD_LINKS:
            return True
        title = (
            ui_text('Too Many Clipboard URLs')
            if clipboard
            else ui_text('Too Many URLs')
        )
        if clipboard:
            message = ui_format(
                'A maximum of {maximum} URLs can be added at once; {count} were found in the clipboard. Copy them in batches.',
                maximum=MAX_BATCH_DOWNLOAD_LINKS,
                count=len(links),
            )
        else:
            message = ui_format(
                'A maximum of {maximum} URLs can be added at once; {count} were found. Add them in batches.',
                maximum=MAX_BATCH_DOWNLOAD_LINKS,
                count=len(links),
            )
        QMessageBox.warning(self.parent, title, message)
        return False

    def request_context(self) -> dict[str, object] | None:
        try:
            return build_download_request_context(
                self.window.app_settings,
                options_json=self.options_provider(),
            )
        except DownloadSubmissionSettingsError as exc:
            if exc.code == 'missing_download_dir':
                QMessageBox.warning(
                    self.parent,
                    ui_text('Folder Unavailable'),
                    ui_text('Configure the download folder on the Settings page first.'),
                )
                return None
            QMessageBox.warning(
                self.parent,
                ui_text('Invalid Proxy Address'),
                ui_text(
                    'Enter a complete proxy address, for example http://127.0.0.1:7890',
                ),
            )
            return None

    def enqueue_links(self, links: list[str]) -> tuple[int, int]:
        context = self.request_context()
        if context is None:
            return 0, 0
        if self.is_debounced(links):
            return 0, len(links)
        self.acknowledge_submission()
        service = self.window.download_service
        advanced = DownloadOptions.from_mapping(context.get('options_json'))
        playlist_mode = submission_playlist_mode(
            context,
            collection_mode=advanced.collection_mode,
        )
        created = 0
        skipped = 0
        failed_url = ''
        failed_error = ''
        failure_logged = False
        for url in links:
            duplicate_id = service.find_active_duplicate(
                url,
                str(context['output_dir']),
                str(context['quality']),
                playlist_mode,
                transcode_codec=str(context.get('transcode_codec', 'original')),
                transcode_device=str(context.get('transcode_device', 'auto')),
                transcode_encoder=str(context.get('transcode_encoder', 'original')),
                subtitle_language=str(context.get('subtitle_language', 'none')),
            )
            if duplicate_id:
                skipped += 1
                continue
            try:
                if playlist_mode != 'single':
                    if not self.start_collection(url, context):
                        failed_url = url
                        failed_error = ui_text('The collection parser is shutting down.')
                        break
                else:
                    service.enqueue(
                        url,
                        str(context['output_dir']),
                        **service_task_arguments(
                            context,
                            playlist_mode=playlist_mode,
                        ),
                    )
            except Exception as exc:
                failed_url = url
                failed_error = str(exc)
                failure_logged = self._write_failure_log(traceback.format_exc())
                break
            created += 1

        if created or skipped:
            self.url_input.clear()
        self._show_result(
            created=created,
            skipped=skipped,
            failed_url=failed_url,
            failed_error=failed_error,
            failure_logged=failure_logged,
        )
        return created, skipped

    def _write_failure_log(self, details: str) -> bool:
        try:
            log_path = Path(self.window.db.path).parent / 'logs' / 'app-crash.log'
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open('a', encoding='utf-8') as stream:
                stream.write(details + '\n')
            return True
        except (OSError, UnicodeError):
            return False

    def _show_result(
        self,
        *,
        created: int,
        skipped: int,
        failed_url: str,
        failed_error: str,
        failure_logged: bool,
    ) -> None:
        if failed_error:
            self.status_label.setText(ui_format(
                'Added {count} tasks; remaining tasks stopped because of an error',
                count=created,
            ))
            details_note = (
                ui_text('Details were written to data/logs/app-crash.log')
                if failure_logged
                else ui_text('The diagnostic log could not be written.')
            )
            QMessageBox.critical(
                self.parent,
                ui_text('Failed to Add Download Tasks'),
                ui_format(
                    'Successfully queued {count} tasks.\n\nFailed URL:\n{url}\n\nError: {error}\n\n{details}',
                    count=created,
                    url=failed_url,
                    error=failed_error,
                    details=details_note,
                ),
            )
        elif created:
            self.status_label.setText(
                ui_format(
                    'Queued {created} download tasks and skipped {skipped} duplicate URLs that are active or paused',
                    created=created,
                    skipped=skipped,
                )
                if skipped
                else ui_format('Queued {count} download tasks', count=created)
            )
        elif skipped:
            self.status_label.setText(ui_format(
                'Not added again: {count} URLs already have unfinished tasks',
                count=skipped,
            ))
            QMessageBox.information(
                self.parent,
                ui_text('Tasks Already Exist'),
                ui_format(
                    'All {count} recognized URLs already have queued, downloading, or paused tasks. Continue those tasks instead.',
                    count=skipped,
                ),
            )
