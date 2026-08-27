from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from app.ui.main_window import MainWindow


def main() -> int:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    settings = window.app_settings
    old_dir = settings.get("download_dir")
    output = ROOT / "data" / "temp" / "gui-flow"
    shutil.rmtree(output, ignore_errors=True)
    output.mkdir(parents=True, exist_ok=True)
    settings.set("download_dir", str(output))
    settings.sync()
    window.dashboard.refresh_settings()
    window.dashboard.url.setText("https://www.youtube.com/watch?v=jNQXAC9IVRw")
    window.dashboard.start()

    def stop() -> None:
        app.quit()

    QTimer.singleShot(35_000, stop)
    app.exec()
    tasks = [task for task in window.download_service.tasks.values() if task.output_dir == str(output)]
    task = tasks[-1] if tasks else None
    print({
        "task_id": task.id if task else None,
        "status": task.status if task else None,
        "title": task.title if task else None,
        "error": task.error if task else None,
        "files": [str(p.relative_to(output)) for p in output.rglob("*") if p.is_file()],
    })
    # Restore the user's existing directory preference and remove this smoke
    # test task/files without touching unrelated tasks.
    settings.set("download_dir", old_dir)
    settings.sync()
    if task:
        window.download_service.delete_task(task.id, delete_files=True)
    window.close()
    app.processEvents()
    shutil.rmtree(output, ignore_errors=True)
    return 0 if task and task.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
