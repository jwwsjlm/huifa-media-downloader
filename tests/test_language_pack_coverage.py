from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app.ui.download_control_presentation import transcode_encoder_label
from app.ui.media_presentation import error_category_text, platform_label
from app.ui.runtime import configure_font, ui_text
from scripts.sync_zh_language_pack import (
    collect_translation_keys,
    collect_translations,
    find_bare_chinese_ui_strings,
    find_bare_english_ui_strings,
    find_legacy_bilingual_calls,
    find_non_ui_translation_usage,
    find_unstable_translation_calls,
    validate_language_packs,
)


ROOT = Path(__file__).resolve().parents[1]
ZH_PACK = ROOT / "languages" / "zh-CN.json"
EN_PACK = ROOT / "languages" / "en-US.json"


class LanguagePackCoverageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.previous_locale = self.app.property("huifa.ui_locale")
        self.previous_translations = self.app.property("huifa.ui_translations")

    def tearDown(self) -> None:
        self.app.setProperty("huifa.ui_locale", self.previous_locale)
        self.app.setProperty("huifa.ui_translations", self.previous_translations)

    def test_chinese_pack_contains_every_source_language_key(self) -> None:
        payload = json.loads(ZH_PACK.read_text(encoding="utf-8"))
        self.assertEqual(payload["locale"], "zh-CN")
        self.assertEqual(payload["translations"], collect_translations())
        self.assertEqual(
            sorted(key for key in collect_translation_keys() if not payload["translations"].get(key)),
            [],
        )

    def test_english_pack_contains_every_source_language_key(self) -> None:
        payload = json.loads(EN_PACK.read_text(encoding="utf-8"))
        self.assertEqual(payload["locale"], "en-US")
        expected = {
            key: key.split("::", 1)[-1]
            for key in collect_translation_keys()
        }
        self.assertEqual(
            {key: payload["translations"].get(key) for key in expected},
            expected,
        )
        self.assertEqual(validate_language_packs(), [])

    def test_visible_ui_calls_do_not_bypass_language_pack(self) -> None:
        self.assertEqual(find_bare_chinese_ui_strings(), [])
        self.assertEqual(find_bare_english_ui_strings(), [])
        self.assertEqual(find_legacy_bilingual_calls(), [])
        self.assertEqual(find_unstable_translation_calls(), [])
        self.assertEqual(find_non_ui_translation_usage(), [])

    def test_chinese_runtime_prefers_pack_over_source_fallback(self) -> None:
        self.app.setProperty("huifa.ui_locale", "zh-CN")
        self.app.setProperty("huifa.ui_translations", {
            "Settings": "来自语言包的设置",
            "settings.title::Settings": "来自上下文语言包的设置",
        })
        self.assertEqual(ui_text("Settings"), "来自语言包的设置")
        self.assertEqual(
            ui_text("Settings", context="settings.title"),
            "来自上下文语言包的设置",
        )

    def test_fixed_platform_mapping_uses_the_pack_but_service_category_does_not(self) -> None:
        self.app.setProperty("huifa.ui_locale", "zh-CN")
        self.app.setProperty("huifa.ui_translations", {
            "Douyin": "来自语言包的平台名",
            "Login / Anti-bot": "来自语言包的错误分类",
        })
        self.assertEqual(platform_label("douyin"), "来自语言包的平台名")
        self.assertEqual(error_category_text("风控/登录"), "风控/登录")

    def test_native_encoder_names_bypass_language_pack(self) -> None:
        self.app.setProperty("huifa.ui_locale", "zh-CN")
        self.app.setProperty("huifa.ui_translations", {
            "NVIDIA NVENC H.264 (GPU)": "不应替换编码器名称",
        })
        self.assertEqual(
            transcode_encoder_label("h264_nvenc"),
            "NVIDIA NVENC H.264 (GPU)",
        )
        self.assertNotIn("NVIDIA NVENC H.264 (GPU)", collect_translation_keys())

    def test_forced_chinese_locale_loads_the_complete_pack(self) -> None:
        with patch.dict(os.environ, {"HUIFA_UI_LOCALE": "zh-CN"}):
            configure_font(self.app, "auto")
        translations = self.app.property("huifa.ui_translations")
        self.assertIsInstance(translations, dict)
        self.assertGreaterEqual(len(translations), 900)
        self.assertEqual(translations.get("Undo"), "撤销")


if __name__ == "__main__":
    unittest.main()
