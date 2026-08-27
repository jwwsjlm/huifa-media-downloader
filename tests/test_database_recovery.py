from __future__ import annotations

import os
import sqlite3
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.storage.database import Database
from app.storage.database_recovery import (
    database_backup_due,
    database_backup_paths,
    database_integrity,
)
from app.storage.models import MediaItem


class DatabaseRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "app.db"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _open_with_media(self, title: str = "可恢复视频") -> None:
        database = Database(self.path)
        database.add_media(MediaItem(source_url=f"https://example.com/{title}", title=title))
        database.close()

    def test_clean_close_creates_a_valid_consistent_backup(self) -> None:
        self._open_with_media()

        newest = database_backup_paths(self.path)[0]
        healthy, detail = database_integrity(newest)
        self.assertTrue(healthy, detail)
        connection = sqlite3.connect(newest)
        try:
            titles = [row[0] for row in connection.execute("SELECT title FROM media_items ORDER BY id DESC")]
        finally:
            connection.close()
        self.assertEqual(titles, ["可恢复视频"])
        self.assertEqual(
            list((self.root / "backups").glob(".app.db.backup-*.tmp*")),
            [],
        )

    def test_future_dated_backup_is_due_after_system_clock_moves_backwards(self) -> None:
        self.path.write_bytes(b"database")
        newest = database_backup_paths(self.path)[0]
        newest.parent.mkdir(parents=True)
        newest.write_bytes(b"backup")
        os.utime(self.path, (90.0, 90.0))
        os.utime(newest, (200.0, 200.0))

        self.assertTrue(
            database_backup_due(
                self.path,
                interval_seconds=24 * 60 * 60,
                now=100.0,
            )
        )

    def test_corrupt_database_is_quarantined_and_restored_from_latest_backup(self) -> None:
        self._open_with_media()
        self.path.write_bytes(b"not a sqlite database")
        sidecars = {
            "app.db-wal": b"stale wal",
            "app.db-shm": b"stale shm",
            "app.db-journal": b"stale journal",
        }
        for name, payload in sidecars.items():
            (self.root / name).write_bytes(payload)

        database = Database(self.path)
        try:
            report = database.recovery_report
            self.assertEqual(report.status, "restored")
            self.assertTrue(report.requires_notice)
            self.assertEqual([item.title for item in database.list_media()], ["可恢复视频"])
            quarantine = Path(report.quarantine_dir)
            self.assertTrue((quarantine / "app.db").is_file())
            self.assertEqual((quarantine / "app.db").read_bytes(), b"not a sqlite database")
            for name, payload in sidecars.items():
                self.assertEqual((quarantine / name).read_bytes(), payload)
        finally:
            database.close()

    def test_corrupt_database_without_backup_starts_empty_and_preserves_original(self) -> None:
        self.path.write_bytes(b"broken")

        database = Database(self.path)
        try:
            report = database.recovery_report
            self.assertEqual(report.status, "reset")
            self.assertEqual(database.list_media(), [])
            self.assertEqual((Path(report.quarantine_dir) / "app.db").read_bytes(), b"broken")
        finally:
            database.close()

    def test_missing_database_is_an_intentional_reset_and_never_restores_stale_backup(self) -> None:
        self._open_with_media("旧任务")
        for suffix in ("", "-wal", "-shm", "-journal"):
            self.path.with_name(self.path.name + suffix).unlink(missing_ok=True)

        database = Database(self.path)
        try:
            self.assertEqual(database.recovery_report.status, "new")
            self.assertEqual(database.list_media(), [])
        finally:
            database.close()

    def test_outdated_or_partial_schema_is_rebuilt_to_current_definition(self) -> None:
        connection = sqlite3.connect(self.path)
        connection.execute(
            "CREATE TABLE download_tasks (id TEXT PRIMARY KEY, url TEXT, output_dir TEXT)"
        )
        connection.execute(
            "INSERT INTO download_tasks VALUES ('old', 'https://example.com', 'D:/old')"
        )
        connection.execute("PRAGMA user_version=1")
        connection.commit()
        connection.close()

        database = Database(self.path)
        try:
            self.assertEqual(database.recovery_report.status, "schema_reset")
            self.assertEqual(database.list_download_tasks(), [])
            columns = [
                row[1]
                for row in database.conn.execute('PRAGMA table_info("download_tasks")').fetchall()
            ]
            self.assertIn("created_at", columns)
            self.assertIn("total_bytes", columns)
        finally:
            database.close()

    def test_constructor_failure_closes_sqlite_connection(self) -> None:
        with patch.object(Database, "_initialize_schema", side_effect=RuntimeError("schema failed")):
            with self.assertRaisesRegex(RuntimeError, "schema failed"):
                Database(self.path)

        # Windows refuses this deletion if the failed constructor leaked its
        # SQLite handle.
        self.path.unlink()
        self.assertFalse(self.path.exists())

    def test_schema_initialization_failure_rolls_back_every_statement(self) -> None:
        statements = (
            "CREATE TABLE partial_schema(value TEXT)",
            "CREATE TABLE invalid_sql(",
        )
        with patch("app.storage.database._SCHEMA_STATEMENTS", statements):
            with self.assertRaises(sqlite3.DatabaseError):
                Database(self.path)

        connection = sqlite3.connect(self.path)
        try:
            partial = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='partial_schema'"
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNone(partial)

    def test_invalid_newest_backup_is_skipped_for_an_older_healthy_generation(self) -> None:
        self._open_with_media("第一代")
        database = Database(self.path)
        database.add_media(MediaItem(source_url="https://example.com/second", title="第二代"))
        database.close()
        newest, older, _oldest = database_backup_paths(self.path)
        self.assertTrue(older.is_file())
        newest.write_bytes(b"damaged backup")
        self.path.write_bytes(b"damaged active database")

        recovered = Database(self.path)
        try:
            self.assertEqual(recovered.recovery_report.status, "restored")
            self.assertEqual(Path(recovered.recovery_report.restored_from), older)
            self.assertEqual([item.title for item in recovered.list_media()], ["第一代"])
        finally:
            recovered.close()

    def test_read_only_check_uses_a_private_copy_when_wal_has_no_shm(self) -> None:
        seed = self.root / "seed.db"
        writer = sqlite3.connect(seed)
        try:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("CREATE TABLE sample(value TEXT)")
            writer.execute("INSERT INTO sample VALUES('committed in wal')")
            writer.commit()
            seed_wal = seed.with_name(seed.name + "-wal")
            self.assertTrue(seed_wal.is_file())
            shutil.copy2(seed, self.path)
            shutil.copy2(seed_wal, self.path.with_name(self.path.name + "-wal"))

            healthy, detail = database_integrity(self.path)
            self.assertTrue(healthy, detail)
            # Integrity checking must not create or alter sidecars beside the
            # user's database when SQLite needs writable SHM state.
            self.assertFalse(self.path.with_name(self.path.name + "-shm").exists())
        finally:
            writer.close()


if __name__ == "__main__":
    unittest.main()
