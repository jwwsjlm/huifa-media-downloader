from __future__ import annotations

import unittest

from app.ui.theme import (
    THEME_DARK,
    THEME_LIGHT,
    THEME_SYSTEM,
    build_application_stylesheet,
    normalize_theme,
    resolve_theme,
)


class ThemeTests(unittest.TestCase):
    def test_theme_values_are_normalized_and_unknown_values_follow_system(self) -> None:
        self.assertEqual(normalize_theme("跟随系统"), THEME_SYSTEM)
        self.assertEqual(normalize_theme("LIGHT"), THEME_LIGHT)
        self.assertEqual(normalize_theme("深色"), THEME_DARK)
        self.assertEqual(normalize_theme("unexpected"), THEME_SYSTEM)

    def test_system_theme_resolves_from_qt_scheme_hint(self) -> None:
        self.assertEqual(resolve_theme(THEME_SYSTEM, system_is_dark=False), THEME_LIGHT)
        self.assertEqual(resolve_theme(THEME_SYSTEM, system_is_dark=True), THEME_DARK)
        self.assertEqual(resolve_theme(THEME_LIGHT, system_is_dark=True), THEME_LIGHT)
        self.assertEqual(resolve_theme(THEME_DARK, system_is_dark=False), THEME_DARK)

    def test_dark_stylesheet_overrides_named_white_input_surfaces(self) -> None:
        light = build_application_stylesheet(THEME_LIGHT)
        dark = build_application_stylesheet(THEME_DARK)
        self.assertIn("background: white", light)
        self.assertIn("QLineEdit, QComboBox, QTextEdit", dark)
        self.assertIn("background: #20262f", dark)
        self.assertNotEqual(light, dark)

    def test_main_navigation_has_sidebar_styles_in_both_themes(self) -> None:
        light = build_application_stylesheet(THEME_LIGHT)
        dark = build_application_stylesheet(THEME_DARK)

        for stylesheet in (light, dark):
            self.assertIn("QFrame#mainSidebar", stylesheet)
            self.assertIn('QToolButton[navigationItem="true"]:checked', stylesheet)
            self.assertIn('QFrame#mainSidebar[collapsed="true"]', stylesheet)
            self.assertIn("background: #225ea8", stylesheet)

    def test_quick_download_controls_center_text_before_the_indicator(self) -> None:
        light = build_application_stylesheet(THEME_LIGHT)

        self.assertIn(
            "QPushButton#downloadOptionMenuButton { text-align: center; padding: 0 28px 0 16px; }",
            light,
        )

    def test_disabled_primary_buttons_do_not_look_actionable(self) -> None:
        light = build_application_stylesheet(THEME_LIGHT)
        dark = build_application_stylesheet(THEME_DARK)

        self.assertIn(
            "QPushButton#primaryButton:disabled { color: #f8fafc; background: #aeb8c4; border: none; }",
            light,
        )
        self.assertIn(
            "QPushButton#primaryButton:disabled { color: #aab6c6; background: #475465; border: none; }",
            dark,
        )

    def test_disabled_input_controls_have_distinct_surfaces(self) -> None:
        light = build_application_stylesheet(THEME_LIGHT)
        dark = build_application_stylesheet(THEME_DARK)
        selector = (
            "QLineEdit:disabled, QComboBox:disabled, QTextEdit:disabled, "
            "QPlainTextEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled"
        )

        self.assertIn(
            f"{selector} {{ color: #98a2b3; background: #f2f4f7; border-color: #e1e5eb; }}",
            light,
        )
        self.assertIn(
            f"{selector} {{ color: #667282; background: #252b34; border-color: #36404d; }}",
            dark,
        )
        self.assertIn(
            "QComboBox#quickDownloadCombo { padding: 0 28px 0 16px; }",
            light,
        )

    def test_shared_visual_system_has_clear_focus_press_and_surface_states(self) -> None:
        light = build_application_stylesheet(THEME_LIGHT)
        dark = build_application_stylesheet(THEME_DARK)

        self.assertIn('font-family: "Segoe UI Variable", "Microsoft YaHei UI", "Segoe UI"', light)
        self.assertIn("QMainWindow, QDialog { background: #f6f8fb", light)
        self.assertIn("QPushButton:pressed", light)
        self.assertIn('QToolButton[navigationItem="true"]:focus', light)
        self.assertIn("QFrame#taskMetricCard:focus", light)
        for stylesheet in (light, dark):
            self.assertIn("QStatusBar", stylesheet)
            self.assertIn("QLineEdit:focus, QComboBox:focus", stylesheet)

    def test_supported_sites_selection_keeps_readable_text_in_both_themes(self) -> None:
        light = build_application_stylesheet(THEME_LIGHT)
        dark = build_application_stylesheet(THEME_DARK)

        generic_selector = "QListWidget::item:selected"
        selector = "QListWidget#supportedSitesList::item:selected"
        table_selector = "QTableView::item:selected"
        self.assertIn(f"{generic_selector} {{ background: #e8f3ff; color: #172033; }}", light)
        self.assertIn(f"{generic_selector} {{ background: #263f5c; color: #f0f3f8; }}", dark)
        self.assertIn(f"{selector} {{ background: #e8f3ff; color: #172033; }}", light)
        self.assertIn(f"{selector} {{ background: #263f5c; color: #f0f3f8; }}", dark)
        self.assertIn(f"{table_selector} {{ background: #e8f3ff; color: #172033; }}", light)
        self.assertIn(f"{table_selector} {{ background: #263f5c; color: #f0f3f8; }}", dark)
