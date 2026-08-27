from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.core.language_packs import (
    discover_language_packs,
    load_language_pack,
    match_language_pack,
    normalize_locale,
)


class LanguagePackTests(unittest.TestCase):
    def _write_pack(self, root: Path, locale: str, translations: dict[str, str]) -> Path:
        path = root / f"{locale}.json"
        path.write_text(json.dumps({
            "schema_version": 1,
            "locale": locale,
            "name": locale,
            "native_name": locale,
            "authors": ["Test"],
            "translations": translations,
        }), encoding="utf-8")
        return path

    def test_loads_valid_pack_and_rejects_invalid_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = self._write_pack(root, "fa-IR", {"Settings": "تنظیمات"})
            pack = load_language_pack(valid)
            self.assertEqual(pack.locale, "fa-IR")
            self.assertEqual(pack.translations["Settings"], "تنظیمات")
            invalid = root / "invalid.json"
            invalid.write_text('{"schema_version": 2}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_language_pack(invalid)

    def test_auto_matches_exact_then_language_and_falls_back_to_chinese(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._write_pack(root, "zh-CN", {})
            self._write_pack(root, "en-US", {"Settings": "Settings"})
            packs = discover_language_packs((root,))
            self.assertEqual(match_language_pack("auto", ["en-GB"], packs).locale, "en-US")
            self.assertEqual(match_language_pack("auto", ["fa-IR"], packs).locale, "zh-CN")
            self.assertEqual(match_language_pack("fa-IR", [], packs).locale, "zh-CN")

    def test_later_directory_overrides_bundled_pack(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            self._write_pack(Path(first), "en-US", {"Settings": "Bundled"})
            self._write_pack(Path(second), "en_US", {"Settings": "User"})
            packs = discover_language_packs((Path(first), Path(second)))
            self.assertEqual(packs["en-us"].translations["Settings"], "User")
            self.assertEqual(normalize_locale("en_us"), "en-US")


if __name__ == "__main__":
    unittest.main()
