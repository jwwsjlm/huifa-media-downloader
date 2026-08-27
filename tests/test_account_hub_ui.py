from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QPushButton, QTabWidget, QWidget

from app.ui.account_hub import AccountHubPage
from app.ui.main_window import MainWindow


class _Settings:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, _key: str) -> str:
        return self.values.get(_key, "")

    def set(self, key: str, value: str) -> None:
        self.values[key] = str(value)

    def set_many(self, values: dict[str, str]) -> dict[str, str]:
        normalized = {str(key): str(value) for key, value in values.items()}
        self.values.update(normalized)
        return normalized


class _PublishService(QObject):
    account_status = Signal(str, str, str, bool, str)

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[tuple, dict]] = []
        self.accept_actions = True

    def account_state(self, _platform: str, _account: str):
        return None

    def run_account_action(self, *args, **kwargs) -> bool:
        self.calls.append((args, kwargs))
        return self.accept_actions


class _Window:
    def __init__(self) -> None:
        self.tabs = QTabWidget()
        self.settings = QWidget()
        self.completed = QWidget()
        self.completed.mark_dirty = lambda: None  # type: ignore[attr-defined]
        self.app_settings = _Settings()
        self.publish_service = _PublishService()

    def run_sau_account_action(
        self,
        platform: str,
        account: str,
        action: str,
        *,
        vault_profile_id: str = "",
    ) -> bool:
        return self.publish_service.run_account_action(
            platform,
            account,
            action,
            **({"vault_profile_id": vault_profile_id} if vault_profile_id else {}),
        )


class AccountHubUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_primary_navigation_actions_do_not_start_login_browser(self) -> None:
        window = _Window()
        hub = AccountHubPage(window)
        window.tabs.addTab(hub, "账号中心")
        window.tabs.addTab(window.completed, "完成列表")
        window.tabs.addTab(window.settings, "设置")
        buttons = {button.text(): button for button in hub.findChildren(QPushButton)}

        self.assertNotIn("管理发布账号", buttons)

        buttons["从完成列表创建发布任务"].click()
        self.app.processEvents()
        self.assertIs(window.tabs.currentWidget(), window.completed)

    def test_account_hub_has_no_creator_portal_browser_actions(self) -> None:
        window = _Window()
        hub = AccountHubPage(window)
        buttons = {button.text() for button in hub.findChildren(QPushButton)}
        self.assertNotIn("打开创作者中心", buttons)
        self.assertFalse(hasattr(hub, "_open_creator_url"))

    def test_account_save_failure_does_not_publish_partial_preferences(self) -> None:
        window = _Window()
        hub = AccountHubPage(window)
        next(iter(hub.publish_target_checks.values())).setChecked(True)
        with patch.object(
            window.app_settings,
            "set_many",
            side_effect=OSError("settings disk unavailable"),
        ), patch.object(window.completed, "mark_dirty") as mark_dirty, patch(
            "app.ui.account_hub.QMessageBox.warning",
        ) as warning, patch(
            "app.ui.account_hub.QMessageBox.information",
        ) as information:
            hub.save_accounts()

        self.assertEqual(window.app_settings.values, {})
        mark_dirty.assert_not_called()
        information.assert_not_called()
        warning.assert_called_once()
        self.assertIn("settings disk unavailable", warning.call_args.args[2])

    def test_non_bilibili_login_uses_the_shared_sau_chromium(self) -> None:
        window = _Window()
        hub = AccountHubPage(window)
        hub.account_action("douyin", "login")

        self.assertEqual(window.publish_service.calls[0][0], ("douyin", "default", "login"))
        self.assertEqual(window.publish_service.calls[0][1], {})
        self.assertIn("登录", hub.platform_account_summary.text())

    def test_bilibili_uses_the_same_sau_account_action(self) -> None:
        window = _Window()
        hub = AccountHubPage(window)
        hub.account_action("bilibili", "login")
        self.assertEqual(window.publish_service.calls[0][0], ("bilibili", "default", "login"))

    def test_rejected_duplicate_operation_restores_row_controls(self) -> None:
        window = _Window()
        window.publish_service.accept_actions = False
        hub = AccountHubPage(window)

        hub.account_action("youtube", "login")

        self.assertTrue(hub.platform_login_buttons["youtube"].isEnabled())
        self.assertTrue(hub.platform_check_buttons["youtube"].isEnabled())
        self.assertTrue(hub.platform_account_summary.text().strip())
        self.assertIn("YouTube", hub.platform_account_summary.text())

    def test_running_action_locks_account_name_and_result_restores_row(self) -> None:
        window = _Window()
        hub = AccountHubPage(window)

        hub.account_action("youtube", "login")

        self.assertFalse(hub.platform_account_fields["youtube"].isEnabled())
        self.assertFalse(hub.platform_login_buttons["youtube"].isEnabled())
        self.assertFalse(hub.platform_check_buttons["youtube"].isEnabled())

        hub.account_action_result(
            "youtube",
            "default",
            "login",
            True,
            "cookies saved",
        )

        self.assertTrue(hub.platform_account_fields["youtube"].isEnabled())
        self.assertTrue(hub.platform_login_buttons["youtube"].isEnabled())
        self.assertTrue(hub.platform_check_buttons["youtube"].isEnabled())
        self.assertNotIn("youtube", hub._pending_account_actions)

    def test_result_restores_controls_even_if_account_changed_programmatically(self) -> None:
        window = _Window()
        hub = AccountHubPage(window)
        idle_text = hub.platform_account_states["youtube"].text()
        hub.account_action("youtube", "check")
        hub.platform_account_fields["youtube"].setText("another")

        hub.account_action_result(
            "youtube",
            "default",
            "check",
            False,
            "expired",
        )

        self.assertTrue(hub.platform_account_fields["youtube"].isEnabled())
        self.assertTrue(hub.platform_login_buttons["youtube"].isEnabled())
        self.assertTrue(hub.platform_check_buttons["youtube"].isEnabled())
        self.assertEqual(hub.platform_account_states["youtube"].text(), idle_text)

    def test_stale_result_for_another_action_does_not_unlock_current_row(self) -> None:
        window = _Window()
        hub = AccountHubPage(window)
        hub.account_action("youtube", "login")
        busy_text = hub.platform_account_states["youtube"].text()

        hub.account_action_result(
            "youtube",
            "default",
            "check",
            True,
            "stale check result",
        )

        self.assertFalse(hub.platform_account_fields["youtube"].isEnabled())
        self.assertEqual(hub.platform_account_states["youtube"].text(), busy_text)
        self.assertEqual(hub._pending_account_actions["youtube"].action, "login")

        hub.account_action_result(
            "youtube",
            "default",
            "login",
            True,
            "cookies saved",
        )
        self.assertTrue(hub.platform_account_fields["youtube"].isEnabled())

    def test_start_exception_restores_controls_and_reports_real_error(self) -> None:
        window = _Window()
        hub = AccountHubPage(window)

        with patch.object(
            window.publish_service,
            "run_account_action",
            side_effect=RuntimeError("account worker failed to start"),
        ):
            hub.account_action("youtube", "check")

        self.assertTrue(hub.platform_account_fields["youtube"].isEnabled())
        self.assertTrue(hub.platform_login_buttons["youtube"].isEnabled())
        self.assertTrue(hub.platform_check_buttons["youtube"].isEnabled())
        self.assertNotIn("youtube", hub._pending_account_actions)
        self.assertIn("account worker failed to start", hub.platform_account_summary.text())

    def test_main_window_dispatches_the_vendored_core_without_bootstrap_download(self) -> None:
        fake = SimpleNamespace(publish_service=_PublishService())
        started = MainWindow.run_sau_account_action(
            fake,
            "browser",
            "download",
            "login",
            vault_profile_id="download",
        )

        self.assertTrue(started)
        self.assertEqual(
            fake.publish_service.calls,
            [(('browser', 'download', 'login'), {'vault_profile_id': 'download'})],
        )


if __name__ == "__main__":
    unittest.main()
