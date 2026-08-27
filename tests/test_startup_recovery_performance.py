from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.publish_service import PublishService
from app.storage.database import Database


class StartupRecoveryPerformanceTests(unittest.TestCase):
    def test_publish_recovery_is_one_set_based_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "app.db")
            with db._lock:
                db.conn.executemany(
                    """INSERT INTO publish_tasks(media_id, platform, status, idempotency_key)
                    VALUES(?, 'douyin', ?, ?)""",
                    [
                        (index + 1, "uploading" if index < 750 else "pending", f"key-{index}")
                        for index in range(1000)
                    ],
                )
                db.conn.commit()

            statements: list[str] = []
            db.conn.set_trace_callback(statements.append)
            service = PublishService(db)
            with patch.object(
                db,
                "list_publish_tasks",
                side_effect=AssertionError("recovery must not load the full publish queue"),
            ), patch.object(
                db,
                "update_publish_status",
                side_effect=AssertionError("recovery must not update tasks one by one"),
            ):
                recovered = service.recover_stale_tasks()
            db.conn.set_trace_callback(None)

            self.assertEqual(recovered, 750)
            updates = [sql for sql in statements if sql.lstrip().upper().startswith("UPDATE ")]
            commits = [sql for sql in statements if sql.strip().upper() == "COMMIT"]
            self.assertEqual(len(updates), 1)
            self.assertEqual(len(commits), 1)
            counts = dict(db.conn.execute("SELECT status, COUNT(*) FROM publish_tasks GROUP BY status"))
            self.assertEqual(counts, {"failed": 750, "pending": 250})
            db.close()


if __name__ == "__main__":
    unittest.main()
