from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import QMessageBox

from app.core.cookie_sources import (
    COOKIE_SOURCE_BROWSER,
    COOKIE_SOURCE_EMBEDDED,
    COOKIE_SOURCE_FILE,
    COOKIE_SOURCE_NONE,
    EMBEDDED_DOWNLOAD_PROFILE,
    browser_cookie_count,
    normalize_cookie_source,
)
from app.core.paths import resolve_portable_path
from app.ui.i18n import format_text as ui_format
from app.ui.i18n import runtime_text, text as ui_text


class DownloadCookieController:
    """Coordinate download-cookie controls and embedded sign-in state."""

    def __init__(self, page: Any) -> None:
        self.page = page
        self._starting_login = False
        self._inline_login_result = False

    def selected_source(self) -> str:
        return normalize_cookie_source(self.page.download_cookie_source.currentData())

    def update_controls(self) -> None:
        page = self.page
        source = self.selected_source()
        browser_enabled = source == COOKIE_SOURCE_BROWSER
        file_enabled = source == COOKIE_SOURCE_FILE
        for widget in (
            page.download_cookie_browser,
            page.download_cookie_profile,
            page.download_cookie_keyring,
            page.download_cookie_container,
        ):
            widget.setEnabled(browser_enabled)
        page.download_cookie_file.setEnabled(file_enabled)

    def check_source(self) -> None:
        page = self.page
        source = self.selected_source()
        if source == COOKIE_SOURCE_NONE:
            QMessageBox.information(
                page,
                ui_text('Cookies disabled'),
                ui_text('Downloads will not send cookies.'),
            )
            return
        if source == COOKIE_SOURCE_FILE:
            path = resolve_portable_path(page.download_cookie_file.text().strip())
            if not path.is_file():
                QMessageBox.warning(
                    page,
                    ui_text('Cookie file unavailable'),
                    ui_text('Choose a valid Netscape cookie file.'),
                )
                return
            QMessageBox.information(
                page,
                ui_text('Cookie file ready'),
                ui_text('The cookie file is ready and values are not logged.'),
            )
            return
        if source == COOKIE_SOURCE_EMBEDDED:
            try:
                from app.core.browser_cookies import CookieVault

                count = CookieVault().count(EMBEDDED_DOWNLOAD_PROFILE)
            except Exception as exc:
                QMessageBox.warning(
                    page,
                    ui_text('Cannot read embedded cookies'),
                    runtime_text(exc),
                )
                return
            if count <= 0:
                QMessageBox.warning(
                    page,
                    ui_text('Not signed in'),
                    ui_text(
                        'The embedded browser has no saved cookies. Open the login page first.'
                    ),
                )
                return
            QMessageBox.information(
                page,
                ui_text('Cookie check complete'),
                ui_format(
                    'The embedded browser has {count} encrypted cookies.',
                    count=count,
                ),
            )
            return
        try:
            count = browser_cookie_count(
                page.download_cookie_browser.currentData(),
                page.download_cookie_profile.text().strip(),
                page.download_cookie_keyring.text().strip(),
                page.download_cookie_container.text().strip(),
            )
        except Exception as exc:
            QMessageBox.warning(
                page,
                ui_text('Cannot read browser cookies'),
                ui_format(
                    'Close the browser and retry, or check the profile.\n\n{error}',
                    error=runtime_text(exc),
                ),
            )
            return
        QMessageBox.information(
            page,
            ui_text('Cookie check complete'),
            ui_format(
                'Read {count} cookies. Values are not displayed or stored.',
                count=count,
            ),
        )

    def open_login(self) -> None:
        page = self.page
        window = page.window
        service = window.publish_service
        self._inline_login_result = False
        self._starting_login = True
        try:
            started = window.run_sau_account_action(
                "browser",
                "download",
                "login",
                vault_profile_id=EMBEDDED_DOWNLOAD_PROFILE,
            )
        except Exception as exc:
            QMessageBox.warning(
                page,
                ui_text('Cannot Open Embedded Sign-in'),
                runtime_text(exc),
            )
            return
        finally:
            self._starting_login = False
        # PublishService reports preparation/startup failures through the
        # account_status signal before returning False.  In that synchronous
        # path login_result() has already restored the controls and shown the
        # precise failure, so do not overwrite it with a second dialog.
        if self._inline_login_result:
            return
        if not started:
            if service.is_account_action_running("browser", "download"):
                QMessageBox.information(
                    page,
                    ui_text('Sign-in Already Running'),
                    ui_text('An operation for the same account is already running'),
                )
            else:
                QMessageBox.warning(
                    page,
                    ui_text('Cannot Open Embedded Sign-in'),
                    ui_text('The sign-in operation could not be started. Check the log and try again.'),
                )
            return
        page.open_cookie_login_button.setEnabled(False)
        page.open_cookie_login_button.setText(ui_text('Signing in'))
        window.settings_status(ui_text(
            'Opening the app-managed Chromium with a blank start page…',
        ))

    def open_viewer(self) -> None:
        from app.ui.embedded_browser import open_vault_cookie_viewer

        open_vault_cookie_viewer(EMBEDDED_DOWNLOAD_PROFILE, self.page)

    def login_result(
        self,
        platform: str,
        account: str,
        action: str,
        ok: bool,
        result: str,
    ) -> None:
        if (platform, account, action) != ("browser", "download", "login"):
            return
        if self._starting_login:
            self._inline_login_result = True
        page = self.page
        page.open_cookie_login_button.setEnabled(True)
        page.open_cookie_login_button.setText(ui_text('Open login page'))
        if not ok:
            QMessageBox.warning(
                page,
                ui_text('Sign-in Failed'),
                runtime_text(result),
            )
            page.window.settings_status(ui_text(
                'The browser session did not produce usable cookies'
            ))
            return
        embedded_index = page.download_cookie_source.findData(COOKIE_SOURCE_EMBEDDED)
        settings = page.window.app_settings
        try:
            settings.set_many({
                "download_cookie_source": COOKIE_SOURCE_EMBEDDED,
            })
        except Exception as exc:
            QMessageBox.warning(
                page,
                ui_text('Save Failed'),
                runtime_text(exc),
            )
            return
        if embedded_index >= 0:
            page.download_cookie_source.setCurrentIndex(embedded_index)
        page.window.dashboard.refresh_settings()
