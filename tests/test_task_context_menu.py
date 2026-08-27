from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from app.ui.task_context_menu import read_download_task_ids


class TaskContextMenuTests(unittest.TestCase):
    def test_reads_durable_task_ids_from_a_fresh_connection(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "app.db"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("CREATE TABLE download_tasks(id TEXT PRIMARY KEY)")
                connection.executemany(
                    "INSERT INTO download_tasks(id) VALUES(?)",
                    (("task-a",), ("task-b",)),
                )
                connection.commit()

            self.assertEqual(
                read_download_task_ids(database),
                {"task-a", "task-b"},
            )

    def test_missing_or_unreadable_database_is_not_treated_as_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "missing.db"
            malformed = Path(temp_dir) / "malformed.db"
            malformed.write_text("not a sqlite database", encoding="utf-8")

            self.assertIsNone(read_download_task_ids(missing))
            self.assertIsNone(read_download_task_ids(malformed))


if __name__ == "__main__":
    unittest.main()
