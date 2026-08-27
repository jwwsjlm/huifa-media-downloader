from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.adapters.sau_adapter import (
    SAU_SUPPORTED_PLATFORMS,
    get_sau_platform_capability,
)
from app.ui.distribution_plan import (
    distribution_target_platforms,
    serialize_distribution_target_platforms,
)
from app.ui.i18n import format_text as ui_format
from app.ui.i18n import runtime_text, text as ui_text
from app.ui.media_presentation import platform_label


@dataclass(slots=True)
class AccountRowWidgets:
    account: QLineEdit
    state: QLabel
    login: QPushButton
    check: QPushButton
    actions: QWidget


@dataclass(frozen=True, slots=True)
class PendingAccountAction:
    account: str
    action: str


class AccountHubPage(QWidget):
    """Manage publishing accounts backed by the app-local Chromium core."""

    def __init__(self, window: Any):
        super().__init__()
        self.window = window
        self._pending_account_actions: dict[str, PendingAccountAction] = {}
        self.account_rows: dict[str, AccountRowWidgets] = {}
        get_setting = window.app_settings.get

        root = self._build_scroll_shell()
        self._build_header(root)
        root.addLayout(self._build_primary_actions())
        root.addWidget(self._build_target_group(get_setting))
        root.addWidget(self._build_accounts_group(get_setting))
        window.publish_service.account_status.connect(self.account_action_result)
        root.addWidget(self._build_browser_note())
        root.addStretch(1)

    def _build_scroll_shell(self) -> QVBoxLayout:
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        root = QVBoxLayout(content)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(14)
        scroll.setWidget(content)
        outer_layout.addWidget(scroll)
        return root

    def _build_header(self, root: QVBoxLayout) -> None:
        title = QLabel(ui_text("Accounts"))
        title.setObjectName("pageTitle")
        root.addWidget(title)

        intro = QLabel(
            ui_text(
                "Manage publishing accounts, Cookie status and target platforms "
                "here. The publish page reads the selected account after login "
                "or checking."
            )
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

    def _build_primary_actions(self) -> QHBoxLayout:
        actions = QHBoxLayout()
        save_accounts = QPushButton(ui_text("Save Account Settings"))
        save_accounts.setObjectName("primaryButton")
        save_accounts.clicked.connect(self.save_accounts)
        completed = QPushButton(ui_text("Create Publish Task from Completed"))
        completed.clicked.connect(
            lambda: self.window.tabs.setCurrentWidget(self.window.completed)
        )
        actions.addWidget(save_accounts)
        actions.addWidget(completed)
        actions.addStretch(1)
        return actions

    def _build_target_group(
        self,
        get_setting: Callable[[str], object],
    ) -> QGroupBox:
        targets_group = QGroupBox(ui_text("Default Publishing Targets"))
        targets_layout = QGridLayout(targets_group)
        targets_layout.setContentsMargins(14, 12, 14, 12)
        self.publish_target_checks: dict[str, QCheckBox] = {}
        configured_targets = set(
            distribution_target_platforms(
                get_setting("publish_target_platforms")
            )
        )
        for index, platform in enumerate(SAU_SUPPORTED_PLATFORMS):
            checkbox = QCheckBox(platform_label(platform))
            checkbox.setChecked(platform in configured_targets)
            checkbox.setToolTip(
                ui_text(
                    "Default targets used by completion coverage and new "
                    "publishing tasks"
                )
            )
            targets_layout.addWidget(checkbox, index // 3, index % 3)
            self.publish_target_checks[platform] = checkbox
        target_note = QLabel(
            ui_text(
                "Select at least one platform; the corresponding Cookie is "
                "checked again before publishing."
            )
        )
        target_note.setObjectName("mutedText")
        target_note.setWordWrap(True)
        targets_layout.addWidget(
            target_note,
            (len(SAU_SUPPORTED_PLATFORMS) + 2) // 3,
            0,
            1,
            3,
        )
        return targets_group

    def _build_accounts_group(
        self,
        get_setting: Callable[[str], object],
    ) -> QGroupBox:
        accounts_group = QGroupBox(ui_text("Platform Accounts and Cookies"))
        accounts_layout = QGridLayout(accounts_group)
        accounts_layout.setContentsMargins(14, 12, 14, 12)
        accounts_layout.setHorizontalSpacing(8)
        accounts_layout.setVerticalSpacing(8)
        for column, key in enumerate(("Platform", "Account", "Status", "Actions")):
            heading = QLabel(ui_text(key))
            heading.setStyleSheet("font-weight: 600; color: #546172;")
            accounts_layout.addWidget(heading, 0, column)

        for row_index, platform in enumerate(SAU_SUPPORTED_PLATFORMS, start=1):
            row = self._build_account_row(platform, get_setting)
            self.account_rows[platform] = row
            accounts_layout.addWidget(QLabel(platform_label(platform)), row_index, 0)
            accounts_layout.addWidget(row.account, row_index, 1)
            accounts_layout.addWidget(row.state, row_index, 2)
            accounts_layout.addWidget(row.actions, row_index, 3)
            self._refresh_account_state(platform)
        accounts_layout.setColumnStretch(1, 1)

        self.platform_account_summary = QLabel(
            ui_text(
                "Login opens the application-bundled Chromium. The same runtime "
                "is reused for Cookie checks and publishing; no external browser "
                "or Python installation is required."
            )
        )
        self.platform_account_summary.setObjectName("mutedText")
        self.platform_account_summary.setWordWrap(True)
        accounts_layout.addWidget(
            self.platform_account_summary,
            len(SAU_SUPPORTED_PLATFORMS) + 1,
            0,
            1,
            4,
        )
        accounts_group.setMinimumHeight(430)
        return accounts_group

    def _build_account_row(
        self,
        platform: str,
        get_setting: Callable[[str], object],
    ) -> AccountRowWidgets:
        account = QLineEdit(
            str(get_setting(f"publish_account/{platform}") or "").strip()
            or "default"
        )
        account.setClearButtonEnabled(True)
        account.setPlaceholderText("default")
        account.setToolTip(ui_text(
            "Account name used to distinguish SAU cookie files; it is "
            "not a password or cookie value"
        ))
        state = QLabel(ui_text("Not checked"))
        state.setObjectName("mutedText")
        state.setMinimumWidth(72)
        login = QPushButton(ui_text("Login"))
        check = QPushButton(ui_text("Check"))
        capability = get_sau_platform_capability(platform)
        if capability is not None and capability.interactive_login:
            login_tip = ui_text(
                "Open a separate login terminal; cookies are validated automatically afterward"
            )
        else:
            login_tip = ui_text(
                "Open a visible browser to sign in; cookies are validated automatically afterward"
            )
        login.setToolTip(login_tip)
        check.setToolTip(ui_text("Check whether this account's cookies are valid"))
        login.clicked.connect(
            lambda _checked=False, target=platform: self.account_action(target, "login")
        )
        check.clicked.connect(
            lambda _checked=False, target=platform: self.account_action(target, "check")
        )
        actions = QWidget()
        actions_layout = QHBoxLayout(actions)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(6)
        actions_layout.addWidget(login)
        actions_layout.addWidget(check)
        account.textChanged.connect(
            lambda _value, target=platform: self._refresh_account_state(target)
        )
        return AccountRowWidgets(account, state, login, check, actions)

    def _build_browser_note(self) -> QLabel:
        browser_note = QLabel(
            ui_text(
                "The application does not call Chrome or Edge installed by the "
                "user. Login and publishing share one app-local Playwright "
                "Chromium, so there is no second QtWebEngine browser."
            )
        )
        browser_note.setObjectName("mutedText")
        browser_note.setWordWrap(True)
        return browser_note

    @property
    def platform_account_fields(self) -> dict[str, QLineEdit]:
        return {platform: row.account for platform, row in self.account_rows.items()}

    @property
    def platform_account_states(self) -> dict[str, QLabel]:
        return {platform: row.state for platform, row in self.account_rows.items()}

    @property
    def platform_login_buttons(self) -> dict[str, QPushButton]:
        return {platform: row.login for platform, row in self.account_rows.items()}

    @property
    def platform_check_buttons(self) -> dict[str, QPushButton]:
        return {platform: row.check for platform, row in self.account_rows.items()}

    def _set_account_row_busy(
        self,
        platform: str,
        busy: bool,
        *,
        account: str = "",
        action: str = "",
    ) -> None:
        row = self.account_rows.get(platform)
        if row is None:
            return
        row.account.setEnabled(not busy)
        row.login.setEnabled(not busy)
        row.check.setEnabled(not busy)
        if busy:
            self._pending_account_actions[platform] = PendingAccountAction(
                account=account,
                action=action,
            )
        else:
            self._pending_account_actions.pop(platform, None)

    def _refresh_account_state(self, platform: str) -> None:
        row = self.account_rows.get(platform)
        if row is None or platform in self._pending_account_actions:
            return
        account = row.account.text().strip()
        state = (
            self.window.publish_service.account_state(platform, account)
            if account
            else None
        )
        if isinstance(state, dict) and bool(state.get("ok")):
            row.state.setText(ui_text("Valid"))
            row.state.setStyleSheet("color: #138a4b; font-weight: 600;")
        elif isinstance(state, dict):
            row.state.setText(ui_text("Invalid"))
            row.state.setStyleSheet("color: #d64444; font-weight: 600;")
        else:
            row.state.setText(ui_text("Not checked"))
            row.state.setStyleSheet("")

    def account_action(self, platform: str, action: str) -> None:
        row = self.account_rows[platform]
        account = row.account.text().strip()
        if not account:
            QMessageBox.warning(
                self,
                ui_text("Account Name Required"),
                ui_format(
                    "Enter the account name for {platform} first.",
                    platform=platform_label(platform),
                ),
            )
            return
        self._begin_account_action(platform, account, action, row)
        started = self._start_account_action(platform, account, action)
        if started is None:
            return
        if not started:
            self._reject_account_action(platform, account)

    def _begin_account_action(
        self,
        platform: str,
        account: str,
        action: str,
        row: AccountRowWidgets,
    ) -> None:
        self._set_account_row_busy(
            platform,
            True,
            account=account,
            action=action,
        )
        row.state.setText(
            ui_text("Signing in") if action == "login" else ui_text("Checking")
        )
        row.state.setStyleSheet(
            "color: #d48716; font-weight: 600;"
        )
        action_label = (
            ui_text("signing in") if action == "login" else ui_text("checking")
        )
        self.platform_account_summary.setText(
            ui_format(
                "Currently {action}: {platform} / {account}",
                action=action_label,
                platform=platform_label(platform),
                account=account,
            )
        )

    def _start_account_action(
        self,
        platform: str,
        account: str,
        action: str,
    ) -> bool | None:
        try:
            return bool(self.window.run_sau_account_action(
                platform,
                account,
                action,
            ))
        except Exception as exc:
            self.account_action_result(
                platform,
                account,
                action,
                False,
                str(exc),
            )
            return None

    def _reject_account_action(self, platform: str, account: str) -> None:
        self._set_account_row_busy(platform, False)
        self._refresh_account_state(platform)
        self.platform_account_summary.setText(
            ui_format(
                "An operation is already running for {platform} / {account}.",
                platform=platform_label(platform),
                account=account,
            )
        )

    def account_action_result(
        self,
        platform: str,
        account: str,
        action: str,
        ok: bool,
        result: str,
    ) -> None:
        row = self.account_rows.get(platform)
        if row is None:
            return
        pending = self._pending_account_actions.get(platform)
        if pending is not None:
            if pending.account != account or pending.action != action:
                return
            self._set_account_row_busy(platform, False)
        if row.account.text().strip() != account:
            self._refresh_account_state(platform)
            return
        message = result.strip().splitlines()[-1] if result.strip() else (
            ui_text("Success") if ok else ui_text("Failed")
        )
        operation = ui_text("Sign-in") if action == "login" else ui_text("Check")
        outcome = ui_text("succeeded") if ok else ui_text("failed")
        self.platform_account_summary.setText(
            ui_format(
                "{platform} / {account} {operation} {outcome}: {message}",
                platform=platform_label(platform),
                account=account,
                operation=operation,
                outcome=outcome,
                message=runtime_text(message)[-500:],
            )
        )
        self._refresh_account_state(platform)

    def save_accounts(self) -> None:
        try:
            selected = tuple(
                platform
                for platform in SAU_SUPPORTED_PLATFORMS
                if self.publish_target_checks[platform].isChecked()
            )
            target_setting = serialize_distribution_target_platforms(selected)
        except ValueError as exc:
            QMessageBox.warning(
                self,
                ui_text("Default Publishing Targets Required"),
                runtime_text(exc),
            )
            return
        settings = self.window.app_settings
        values = {
            f"publish_account/{platform}": row.account.text().strip() or "default"
            for platform, row in self.account_rows.items()
        }
        values["publish_target_platforms"] = target_setting
        try:
            settings.set_many(values)
        except Exception as exc:
            QMessageBox.warning(self, ui_text("Save Failed"), runtime_text(exc))
            return
        self.window.completed.mark_dirty()
        QMessageBox.information(
            self,
            ui_text("Account Settings Saved"),
            ui_text(
                "Publishing accounts, Cookie targets, and default publishing "
                "platforms were saved."
            ),
        )
