from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.atomic_json import write_json_atomic


class AtomicJsonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_write_replaces_json_and_removes_temporary_file(self) -> None:
        target = self.root / "state.json"

        write_json_atomic(target, {"version": 1, "name": "汇发"})

        self.assertEqual(
            json.loads(target.read_text(encoding="utf-8")),
            {"version": 1, "name": "汇发"},
        )
        self.assertFalse(target.with_name(target.name + ".tmp").exists())

    def test_replace_failure_preserves_previous_target_and_cleans_temporary(self) -> None:
        target = self.root / "state.json"
        target.write_text('{"version":1}\n', encoding="utf-8")

        with patch("app.core.atomic_json.os.replace", side_effect=OSError("busy")):
            with self.assertRaisesRegex(OSError, "busy"):
                write_json_atomic(target, {"version": 2})

        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"version": 1})
        self.assertFalse(target.with_name(target.name + ".tmp").exists())


if __name__ == "__main__":
    unittest.main()
