from __future__ import annotations

import os
import json
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLineEdit, QTextEdit, QWidget

from app.ui.i18n import apply_runtime_translation, translate_standard_edit_menu


class ContextMenuTranslationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.previous_locale = self.app.property("huifa.ui_locale")
        self.previous_translations = self.app.property("huifa.ui_translations")
        self.app.setProperty("huifa.ui_locale", "zh-CN")
        pack = Path(__file__).resolve().parents[1] / "languages" / "zh-CN.json"
        translations = json.loads(pack.read_text(encoding="utf-8"))["translations"]
        self.app.setProperty("huifa.ui_translations", translations)

    def tearDown(self) -> None:
        self.app.setProperty("huifa.ui_locale", self.previous_locale)
        self.app.setProperty("huifa.ui_translations", self.previous_translations)

    def test_standard_line_edit_actions_are_chinese_and_keep_shortcuts(self) -> None:
        editor = QLineEdit("可编辑文本")
        editor.selectAll()
        menu = editor.createStandardContextMenu()
        translate_standard_edit_menu(menu)
        labels = {action.text().split("\t", 1)[0] for action in menu.actions() if not action.isSeparator()}
        self.assertTrue({"撤销", "重做", "剪切", "复制", "粘贴", "删除", "全选"}.issubset(labels))
        copy_action = next(action for action in menu.actions() if action.text().split("\t", 1)[0] == "复制")
        self.assertIn("Ctrl+C", copy_action.text())
        menu.deleteLater()

    def test_runtime_setup_installs_localized_menu_on_all_line_edits(self) -> None:
        root = QWidget()
        first = QLineEdit(root)
        second = QLineEdit(root)
        notes = QTextEdit(root)
        apply_runtime_translation(root)
        self.assertEqual(first.contextMenuPolicy(), Qt.CustomContextMenu)
        self.assertEqual(second.contextMenuPolicy(), Qt.CustomContextMenu)
        self.assertEqual(notes.contextMenuPolicy(), Qt.CustomContextMenu)
        self.assertTrue(first.property("huifa.localized_context_menu"))
        self.assertTrue(second.property("huifa.localized_context_menu"))
        self.assertTrue(notes.property("huifa.localized_context_menu"))

    def test_editors_created_after_runtime_setup_are_localized_on_show(self) -> None:
        root = QWidget()
        apply_runtime_translation(root)
        late_editor = QLineEdit()
        late_editor.show()
        self.app.processEvents()
        try:
            self.assertEqual(late_editor.contextMenuPolicy(), Qt.CustomContextMenu)
            self.assertTrue(late_editor.property("huifa.localized_context_menu"))
        finally:
            late_editor.close()


if __name__ == "__main__":
    unittest.main()
