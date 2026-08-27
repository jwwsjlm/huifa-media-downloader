from __future__ import annotations

import unittest
from unittest.mock import patch

from keyring.errors import PasswordDeleteError

from app.storage.secure_store import SecureStore


class _MemoryBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, str, str]] = []

    def set_password(self, service: str, key: str, value: str) -> None:
        self.calls.append(("set", service, key))
        self.values[(service, key)] = value

    def get_password(self, service: str, key: str) -> str | None:
        self.calls.append(("get", service, key))
        return self.values.get((service, key))

    def delete_password(self, service: str, key: str) -> None:
        self.calls.append(("delete", service, key))
        if self.values.pop((service, key), None) is None:
            raise PasswordDeleteError(service)


class SecureStoreTests(unittest.TestCase):
    def test_explicit_backend_round_trip(self) -> None:
        backend = _MemoryBackend()
        store = SecureStore(backend=backend)

        store.set("api-key", "new-secret")
        self.assertEqual(store.get("api-key"), "new-secret")
        store.delete("api-key")
        self.assertIsNone(store.get("api-key"))

    def test_missing_backend_is_nonfatal_for_reads_and_actionable_for_writes(self) -> None:
        with patch("app.storage.secure_store.WinVaultKeyring", None):
            store = SecureStore()
        self.assertEqual(store.backend_name, "")
        self.assertIsNone(store.get("missing"))
        with self.assertRaisesRegex(RuntimeError, "系统凭据库组件不可用"):
            store.set("key", "value")
        with self.assertRaisesRegex(RuntimeError, "系统凭据库组件不可用"):
            store.delete("key")

    def test_backend_errors_are_wrapped_without_changing_delete_missing_semantics(self) -> None:
        backend = _MemoryBackend()
        store = SecureStore(backend=backend)
        # Missing credentials are deliberately idempotent.
        store.delete("not-present")

        def fail(*_args):
            raise OSError("credential manager denied access")

        backend.set_password = fail
        with self.assertRaisesRegex(RuntimeError, "无法写入系统凭据库"):
            store.set("key", "value")


if __name__ == "__main__":
    unittest.main()
