from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.core.update_receipt import (
    INSTALL_INTENT_FILENAME,
    INSTALL_RECEIPT_FILENAME,
    consume_update_install_receipt,
    record_update_install_result,
    write_update_install_intent,
)


class UpdateInstallReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name) / "updates" / "application"
        self.update = SimpleNamespace(
            current_version="0.1.0",
            version="0.2.0",
        )

    def test_success_receipt_preserves_confirmed_versions_and_is_consumed_once(
        self,
    ) -> None:
        write_update_install_intent(self.state_dir, self.update)

        receipt = record_update_install_result(
            self.state_dir,
            status="succeeded",
            current_version="0.2.0",
            message="verified",
        )

        self.assertTrue(receipt.succeeded)
        self.assertTrue(receipt.installed_version_matches("v0.2.0"))
        self.assertEqual(receipt.from_version, "0.1.0")
        self.assertEqual(receipt.to_version, "0.2.0")
        self.assertFalse((self.state_dir / INSTALL_INTENT_FILENAME).exists())
        consumed = consume_update_install_receipt(self.state_dir)
        self.assertEqual(consumed, receipt)
        self.assertFalse((self.state_dir / INSTALL_RECEIPT_FILENAME).exists())
        self.assertIsNone(consume_update_install_receipt(self.state_dir))

    def test_failed_receipt_keeps_old_running_version_and_reason(self) -> None:
        write_update_install_intent(self.state_dir, self.update)

        receipt = record_update_install_result(
            self.state_dir,
            status="failed",
            current_version="0.1.0",
            message="target file was locked",
        )

        self.assertFalse(receipt.succeeded)
        self.assertFalse(receipt.installed_version_matches("0.1.0"))
        self.assertEqual(receipt.message, "target file was locked")

    def test_malformed_receipt_is_removed_instead_of_reappearing_forever(self) -> None:
        self.state_dir.mkdir(parents=True)
        path = self.state_dir / INSTALL_RECEIPT_FILENAME
        path.write_text(
            json.dumps({"schema_version": 1, "status": "unknown"}), encoding="utf-8"
        )

        self.assertIsNone(consume_update_install_receipt(self.state_dir))
        self.assertFalse(path.exists())

    def test_intent_rejects_an_empty_target_version(self) -> None:
        with self.assertRaisesRegex(ValueError, "目标版本"):
            write_update_install_intent(
                self.state_dir,
                SimpleNamespace(current_version="0.1.0", version=""),
            )


if __name__ == "__main__":
    unittest.main()
