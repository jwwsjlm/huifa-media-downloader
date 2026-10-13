from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.storage.database import Database


class DatabaseSchemaRecoveryTest(unittest.TestCase):
    def test_schema_mismatch_quarantines_original_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.db"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE legacy_tasks (id INTEGER PRIMARY KEY)")
                connection.execute("PRAGMA user_version=1")
                connection.commit()
            database = Database(path)
            try:
                self.assertEqual(database.recovery_report.status, "schema_reset")
                self.assertTrue(
                    (Path(database.recovery_report.quarantine_dir) / path.name).is_file()
                )
            finally:
                database.close()


if __name__ == "__main__":
    unittest.main()
