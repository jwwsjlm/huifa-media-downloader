from __future__ import annotations

from PySide6.QtCore import Qt, Slot
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.core.application_update_service import ApplicationUpdateService
from app.core.application_updater import ApplicationUpdate
from app.core.version import APP_VERSION
from app.ui.i18n import format_text as ui_format, runtime_text, text as ui_text
from app.ui.media_presentation import format_file_size


class ApplicationUpdateDialog(QDialog):
    def __init__(
        self,
        update: ApplicationUpdate,
        service: ApplicationUpdateService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.update = update
        self.service = service
        self._closed = False
        self._signals_connected = False
        self._resuming_download = False
        self._download_resumable = False
        self.setWindowTitle(
            ui_text('Update downloaded; restart to install')
            if update.downloaded
            else ui_text('Huifa Video Downloader update available')
        )
        self.resize(650, 500)
        layout = QVBoxLayout(self)
        heading = QLabel(
            ui_format('Version {version} downloaded', version=update.version)
            if update.downloaded
            else ui_format('Version {version} is available', version=update.version)
        )
        heading.setObjectName("pageTitle")
        layout.addWidget(heading)
        mode = (
            ui_text('Velopack self-updating portable')
            if update.is_portable
            else ui_text('Velopack installer')
        )
        summary = QLabel(ui_format(
            'Current: {current}   →   Target: {target}\nDelivery: {mode}   ·   Download size: {size}',
            current=update.current_version or APP_VERSION,
            target=update.version,
            mode=mode,
            size=format_file_size(update.size_bytes),
        ))
        summary.setWordWrap(True)
        layout.addWidget(summary)
        checksum = QLabel(ui_text('SHA-256: ') + f"{update.sha256 or ui_text('verified by the update engine')}")
        checksum.setObjectName("mutedText")
        checksum.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(checksum)
        layout.addWidget(QLabel(ui_text('Release Notes from GitHub Releases')))
        self.notes = QTextEdit()
        self.notes.setReadOnly(True)
        notes = update.release_notes_markdown.strip() or ui_text('No release notes were provided for this release.')
        try:
            self.notes.setMarkdown(notes)
        except AttributeError:
            self.notes.setPlainText(notes)
        layout.addWidget(self.notes, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(100 if update.downloaded else 0)
        self.progress.setVisible(update.downloaded)
        layout.addWidget(self.progress)
        self.status = QLabel(
            ui_format(
                'Version {version} is downloaded and verified; you can restart to install it.',
                version=update.version,
            )
            if update.downloaded
            else ui_text('Current tasks will remain active while the update downloads.')
        )
        self.status.setObjectName("mutedText")
        layout.addWidget(self.status)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.later_button = QPushButton(ui_text('Later'))
        self.later_button.clicked.connect(self.defer_or_pause)
        self.download_button = QPushButton(
            ui_text('Downloaded')
            if update.downloaded
            else ui_text('Download Update')
        )
        self.download_button.setObjectName("primaryButton")
        self.download_button.setEnabled(not update.downloaded)
        self.download_button.clicked.connect(self.start_download)
        self.install_button = QPushButton(ui_text('Restart to Install'))
        self.install_button.setObjectName("primaryButton")
        self.install_button.setEnabled(update.downloaded)
        self.install_button.clicked.connect(self.install)
        buttons.addWidget(self.later_button)
        buttons.addWidget(self.download_button)
        buttons.addWidget(self.install_button)
        layout.addLayout(buttons)
        service.progress.connect(self.on_progress)
        service.downloaded.connect(self.on_downloaded)
        service.download_cancelled.connect(self.on_download_cancelled)
        service.failed.connect(self.on_failed)
        self._signals_connected = True

    def start_download(self) -> None:
        if self._closed:
            return
        self._resuming_download = self._download_resumable
        self.download_button.setEnabled(False)
        try:
            started = self.service.download(self.update)
        except Exception as exc:
            self.download_button.setEnabled(True)
            self.status.setText(
                ui_text('Update failed: ') + runtime_text(exc)
            )
            return
        if not started:
            self.download_button.setEnabled(True)
            self.status.setText(ui_text('Another update operation is already running. Please wait.'))
            return
        self.later_button.setText(ui_text('Pause Download'))
        self.later_button.setEnabled(True)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.status.setText(
            ui_text('Resuming and verifying the saved download…')
            if self._resuming_download
            else ui_text('Downloading and verifying the update…')
        )

    def defer_or_pause(self) -> None:
        if self.service.cancel_download():
            self.later_button.setEnabled(False)
            self.status.setText(ui_text('Safely pausing and saving the download checkpoint…'))
            return
        self.reject()

    @Slot(int)
    def on_progress(self, progress: int) -> None:
        if self._closed:
            return
        normalized_progress = max(0, min(100, int(progress)))
        self.progress.setValue(normalized_progress)
        action = ui_text('Resuming and verifying') if self._resuming_download else ui_text('Downloading and verifying the update')
        self.status.setText(f"{action}: {normalized_progress}%")

    @Slot(object)
    def on_downloaded(self, update: ApplicationUpdate) -> None:
        if self._closed:
            return
        self.update = update
        self._download_resumable = False
        self._resuming_download = False
        self.progress.setValue(100)
        self.status.setText(ui_text('The update was downloaded and verified. Downloads and publishing tasks will stop safely during installation.'))
        self.later_button.setText(ui_text('Later'))
        self.later_button.setEnabled(True)
        self.download_button.setText(ui_text('Downloaded'))
        self.download_button.setEnabled(False)
        self.install_button.setEnabled(True)

    @Slot()
    def on_download_cancelled(self) -> None:
        if self._closed:
            return
        self._download_resumable = True
        self._resuming_download = False
        self.status.setText(ui_text('Download paused; the checkpoint was saved safely and can be resumed later.'))
        self.later_button.setText(ui_text('Later'))
        self.later_button.setEnabled(True)
        self.download_button.setText(ui_text('Resume Download'))
        self.download_button.setEnabled(True)

    @Slot(str)
    def on_failed(self, error: str) -> None:
        if self._closed:
            return
        resumable = "断点" in error or "已保留" in error
        self._download_resumable = resumable
        self._resuming_download = False
        self.status.setText(
            (ui_text('Download interrupted; progress saved: ') + runtime_text(error))
            if resumable else (ui_text('Update failed: ') + runtime_text(error))
        )
        self.later_button.setText(ui_text('Later'))
        self.later_button.setEnabled(True)
        self.download_button.setText(ui_text('Resume Download') if resumable else ui_text('Download Again'))
        self.download_button.setEnabled(True)

    def install(self) -> None:
        if self._closed:
            return
        answer = QMessageBox.question(
            self,
            ui_text('Confirm Update'),
            ui_text('The app will safely stop background tasks, exit, update, and restart automatically.\n\nUpdate now?'),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.parent().install_application_update(
                self.update,
                confirmed=True,
            )
        except Exception as exc:
            QMessageBox.warning(self, ui_text('Unable to install update'), runtime_text(exc))
            return
        self.accept()

    def _deactivate(self) -> None:
        self._closed = True
        if not self._signals_connected:
            return
        for signal, slot in (
            (self.service.progress, self.on_progress),
            (self.service.downloaded, self.on_downloaded),
            (self.service.download_cancelled, self.on_download_cancelled),
            (self.service.failed, self.on_failed),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._signals_connected = False

    def closeEvent(self, event) -> None:
        self._deactivate()
        super().closeEvent(event)

    def done(self, result: int) -> None:
        self._deactivate()
        super().done(result)
