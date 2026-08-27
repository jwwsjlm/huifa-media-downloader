from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QCoreApplication, QTimer

from app.core.download_service import DownloadService
from app.storage.database import Database


def main() -> int:
    root = ROOT
    work = root / "data" / "temp" / "flow-verification"
    shutil.rmtree(work, ignore_errors=True)
    output = work / "downloads"
    output.mkdir(parents=True, exist_ok=True)
    db = Database(work / "app.db")
    app = QCoreApplication(sys.argv)
    service = DownloadService(db, max_concurrent=1)
    state: dict[str, object] = {"finished": False, "failed": None, "media": None}

    def on_finished(task_id: str, status: str, error: str) -> None:
        state["finished"] = True
        state["status"] = status
        state["error"] = error
        QTimer.singleShot(0, app.quit)

    def on_failed(error: str) -> None:
        state["failed"] = error

    def on_media(_task_id: str, media: object) -> None:
        state["media"] = media

    service.task_finished.connect(on_finished)
    service.failed.connect(on_failed)
    service.task_media_completed.connect(on_media)
    task_id = service.enqueue(
        "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        str(output),
        quality="best",
        playlist_mode="single",
    )
    QTimer.singleShot(120_000, app.quit)
    app.exec()
    task = service.tasks.get(task_id)
    files = [p for p in output.rglob("*") if p.is_file()]
    print({
        "task_id": task_id,
        "status": getattr(task, "status", None),
        "finished": state.get("finished"),
        "failed": state.get("failed"),
        "media": bool(state.get("media")),
        "files": [(str(p.relative_to(output)), p.stat().st_size) for p in files],
    })
    db.close()
    return 0 if task and task.status == "completed" and files else 1


if __name__ == "__main__":
    raise SystemExit(main())
