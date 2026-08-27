from __future__ import annotations

import os
from typing import Any

from PySide6.QtCore import QObject, QTimer, Slot
from PySide6.QtWidgets import QMessageBox

from app.core.application_updater import ApplicationUpdate
from app.core.version import APP_VERSION
from app.core.update_receipt import UpdateInstallReceipt, consume_update_install_receipt
from app.ui.application_update_dialog import ApplicationUpdateDialog
from app.ui.i18n import format_text as ui_format
from app.ui.i18n import runtime_text, text as ui_text
from app.ui.media_presentation import format_file_size


def application_update_receipt_presentation(
    receipt: UpdateInstallReceipt,
    current_version: str,
) -> tuple[bool, str, str]:
    """Build a user-facing, version-aware result for the one-shot receipt."""

    if receipt.succeeded and receipt.installed_version_matches(current_version):
        previous = ui_format(
            ' (previous version {version})',
            version=receipt.from_version,
        ) if receipt.from_version else ""
        details = ui_format(
            'The application was successfully updated to version {version}{previous}.',
            version=current_version,
            previous=previous,
        )
        if receipt.message:
            details += f"\n\n{receipt.message}"
        return True, ui_text('Update Installed'), details
    if receipt.succeeded:
        unknown = ui_text('Unknown')
        details = ui_format(
            'The update replacer reported success, but the running version does not match the target.\n\nTarget version: {target}\nCurrent version: {current}\n\nCheck for updates again. If this repeats, export a diagnostics package.',
            target=receipt.to_version or unknown,
            current=current_version or unknown,
        )
        return False, ui_text('Update Result Needs Confirmation'), details
    details = ui_format(
        'The previous update could not be installed. The application continues to use version {version}.',
        version=current_version or receipt.from_version or ui_text('Unknown'),
    )
    if receipt.message:
        details += ui_format(
            '\n\nFailure reason: {reason}',
            reason=receipt.message,
        )
    details += ui_text(
        '\n\nYou can check for and download the update again in Settings.',
    )
    return False, ui_text('Update Installation Failed'), details


class ApplicationUpdateController(QObject):
    """Own application-update configuration, UI state and deferred actions."""

    AUTO_CHECK_DELAY_MS = 8000

    def __init__(self, window: Any) -> None:
        super().__init__(window)
        self.parent = window
        self.service = window.application_update_service
        self.app_settings = window.app_settings
        self.settings_page = window.settings
        self.update_dir = window.application_update_dir
        self.supported = bool(window.application_updates_supported)
        self.current_version = str(getattr(window, "application_version", APP_VERSION))
        self.shutdown_started = lambda: bool(
            getattr(getattr(window, "shutdown_controller", None), "started", False)
        )
        self.show_status = window.statusBar().showMessage
        self.close_application = window.close
        self._automatic = False
        self._restoring = False
        self._install_receipt: UpdateInstallReceipt | None = None
        self._auto_check_timer = QTimer(self)
        self._auto_check_timer.setSingleShot(True)
        self._auto_check_timer.setInterval(self.AUTO_CHECK_DELAY_MS)
        self._auto_check_timer.timeout.connect(self.auto_check)
        self._connect_signals()

    def _connect_signals(self) -> None:
        service = self.service
        service.update_available.connect(self.update_available)
        service.no_update.connect(self.no_update)
        service.pending_restart_available.connect(self.pending_restart_available)
        service.no_pending_restart.connect(self.no_pending_restart)
        service.failed.connect(self.failed)
        service.busy_changed.connect(self.busy_changed)

    def initialize_startup(self) -> None:
        configured = self.configure(silent=True) if self.supported else False
        QTimer.singleShot(0, self, self.show_install_result_once)
        if configured:
            # Restore a verified local package before the daily network check.
            QTimer.singleShot(0, self, self.restore_pending)

    def request_shutdown(self) -> None:
        self._auto_check_timer.stop()

    def configure(self, *, silent: bool = False) -> bool:
        if not self.supported:
            message = ui_text(
                'The app is running from source/development mode. Application updates are enabled only in official releases.',
            )
            self.settings_page.application_update_status.setText(message)
            if not silent:
                QMessageBox.information(
                    self.parent,
                    ui_text('Development Mode'),
                    message,
                )
            return False
        repository = self.app_settings.get("update_repo").strip()
        if not repository:
            if not silent:
                QMessageBox.information(
                    self.parent,
                    ui_text('Repository Not Configured'),
                    ui_text(
                        'Enter a GitHub repository such as owner/repository in Settings, then save the configuration.',
                    ),
                )
            return False
        try:
            self.service.configure(
                repository,
                prerelease=self.app_settings.get_bool(
                    "update_prerelease",
                    False,
                ),
                access_token=os.environ.get("GITHUB_TOKEN", "").strip() or None,
                channel=self.app_settings.get("update_channel") or None,
            )
        except Exception as exc:
            if not silent:
                QMessageBox.warning(
                    self.parent,
                    ui_text('Update Configuration Unavailable'),
                    runtime_text(exc),
                )
            self.settings_page.application_update_status.setText(
                ui_text('Update configuration unavailable: ') + runtime_text(exc)
            )
            return False
        return True

    def check(self) -> None:
        if self.shutdown_started() or not self.configure():
            return
        if self.service.busy:
            self.settings_page.application_update_status.setText(ui_text(
                'An application update operation is in progress. Please wait…',
            ))
            return
        pending = self.service.current_update
        if pending is not None and pending.downloaded:
            self.settings_page.application_update_status.setText(ui_format(
                'Version {version} has been downloaded and is waiting for restart installation',
                version=pending.version,
            ))
            self.show_dialog(pending)
            return
        self._automatic = False
        self.settings_page.application_update_status.setText(ui_text(
            'Checking GitHub Releases for application updates…',
        ))
        if not self.service.check():
            self.settings_page.application_update_status.setText(ui_text(
                'Unable to start the check. Try again later.',
            ))

    def show_install_result_once(self) -> None:
        if self.shutdown_started():
            return
        receipt = consume_update_install_receipt(self.update_dir)
        if receipt is None:
            return
        self._install_receipt = receipt
        succeeded, title, message = application_update_receipt_presentation(
            receipt,
            self.current_version,
        )
        if succeeded:
            self.settings_page.application_update_status.setText(ui_format(
                'Successfully updated to version {version}',
                version=self.current_version,
            ))
            self.show_status(ui_format(
                'Update installed successfully. Current version: {version}',
                version=self.current_version,
            ), 12_000)
            QMessageBox.information(self.parent, title, message)
        else:
            self.settings_page.application_update_status.setText(
                title + ui_text('; review the message and check again')
            )
            self.show_status(title, 12_000)
            QMessageBox.warning(self.parent, title, message)

    def restore_pending(self) -> None:
        if self.shutdown_started() or not self.supported:
            return
        if not self.configure(silent=True) or self.service.busy:
            return
        self._restoring = True
        self.settings_page.application_update_status.setText(ui_text(
            'Restoring a downloaded update that is waiting to be installed…',
        ))
        if not self.service.restore_pending_restart():
            self._restoring = False
            self.schedule_auto_check()

    def schedule_auto_check(self) -> None:
        if (
            self.shutdown_started()
            or self._auto_check_timer.isActive()
            or not self.supported
            or not self.app_settings.get_bool("auto_check_updates", True)
        ):
            return
        self._auto_check_timer.start()

    def auto_check(self) -> None:
        if self.shutdown_started():
            return
        if not self.app_settings.get_bool("auto_check_updates", True):
            return
        if not self.configure(silent=True):
            return
        pending = self.service.current_update
        if pending is not None and pending.downloaded:
            self.settings_page.application_update_status.setText(ui_format(
                'Version {version} has been downloaded and is waiting for restart installation',
                version=pending.version,
            ))
            return
        if not self.service.is_auto_check_due():
            return
        self._automatic = True
        if not self.service.check(automatic=True):
            # A start failure emits no-update/failure only in some adapters.
            # Never let a later manual result inherit the automatic flag.
            self._automatic = False

    def show_dialog(self, update: ApplicationUpdate) -> None:
        if self.shutdown_started():
            return
        ApplicationUpdateDialog(
            update,
            self.service,
            self.parent,
        ).exec()

    @Slot(object)
    def update_available(self, update: ApplicationUpdate) -> None:
        self.settings_page.application_update_status.setText(ui_format(
            'New version {version} found ({size})',
            version=update.version,
            size=format_file_size(update.size_bytes),
        ))
        self._automatic = False
        QTimer.singleShot(0, self, lambda current=update: self.show_dialog(current))

    @Slot(object)
    def pending_restart_available(self, update: ApplicationUpdate) -> None:
        self._restoring = False
        self._automatic = False
        self.settings_page.application_update_status.setText(ui_format(
            'Version {version} has been downloaded and is waiting for restart installation (click Check Application to view it again)',
            version=update.version,
        ))
        QTimer.singleShot(0, self, lambda current=update: self.show_dialog(current))

    @Slot()
    def no_pending_restart(self) -> None:
        self._restoring = False
        receipt = self._install_receipt
        version = self.current_version
        if receipt is not None and receipt.succeeded and receipt.installed_version_matches(version):
            self.settings_page.application_update_status.setText(ui_format(
                'Successfully updated to version {version}',
                version=version,
            ))
        elif receipt is None:
            self.settings_page.application_update_status.setText(ui_format(
                'Current version: {version}. No downloaded update is waiting to be installed.',
                version=version,
            ))
        self.schedule_auto_check()

    @Slot()
    def no_update(self) -> None:
        automatic = self._automatic
        self._automatic = False
        version = self.current_version
        self.settings_page.application_update_status.setText(ui_format(
            'The application is up to date ({version})',
            version=version,
        ))
        if not automatic:
            QMessageBox.information(
                self.parent,
                ui_text('Up to Date', context="application.update"),
                ui_format(
                    'Version {version} is the latest version in the configured GitHub update channel.',
                    version=version,
                ),
            )

    @Slot(str)
    def failed(self, error: str) -> None:
        if self._restoring:
            self._restoring = False
            self.settings_page.application_update_status.setText(
                ui_text('Unable to restore pending update: ') + runtime_text(error)
            )
            self.schedule_auto_check()
            return
        automatic = self._automatic
        self._automatic = False
        localized_error = runtime_text(error)
        self.settings_page.application_update_status.setText(localized_error)
        if not automatic:
            QMessageBox.warning(
                self.parent,
                ui_text('Application Update Check Failed'),
                localized_error,
            )

    @Slot(bool)
    def busy_changed(self, busy: bool) -> None:
        self.settings_page.application_update_button.setEnabled(
            self.supported and not busy
        )

    def install(self, update: ApplicationUpdate, *, confirmed: bool = False) -> None:
        self.service.schedule_install_and_restart(
            update,
            confirmed=confirmed,
        )
        self.show_status(
            ui_text('The update is ready. Exiting safely to install it…'),
            0,
        )
        QTimer.singleShot(0, self, self.close_application)


__all__ = [
    "ApplicationUpdateController",
    "application_update_receipt_presentation",
]
