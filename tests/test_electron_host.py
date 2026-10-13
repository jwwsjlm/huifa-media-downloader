from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QObject

from app.core.download_service import DownloadTask
from app.core.download_task_index import DownloadTaskIndex
from app.presentation.electron_host import ElectronHost, validate_request
from app.presentation.app_bridge import AppBridge
from app.presentation.task_list_model import TaskListModel
from tests.test_task_list_model import _Service


class ElectronProtocolTest(unittest.TestCase):
    def test_explicit_commands_and_argument_types(self):
        validate_request({"id": 1, "target": "app", "action": "startTask", "args": ["a"]})
        for target, action, args in [("app", "_task", ["a"]), ("app", "deleteTask", ["a", 1]), ("draft", "update", [{"__class__": "bad"}]), ("settings", "save", [[], {}, "key"]), ("app", "startTask", [True])]:
            with self.assertRaises(ValueError):
                validate_request({"id": 1, "target": target, "action": action, "args": args})

    def test_progress_delta_does_not_serialize_history(self):
        application = QCoreApplication.instance() or QCoreApplication([])
        service = _Service()
        service.tasks = {str(index): DownloadTask(id=str(index), url="https://example.test/a", output_dir="downloads", status="completed" if index else "downloading") for index in range(1000)}
        model = TaskListModel(service)
        host = ElectronHost.__new__(ElectronHost)
        QObject.__init__(host)
        host.runtime = SimpleNamespace(download_service=service)
        host.bridge = SimpleNamespace(taskModel=model)
        host._assets = {}
        host._dirty = set()
        host._recent_rows = []
        host._task_deltas = {"0"}
        events = []
        host.output = events.append
        host.flush()
        self.assertEqual(events[0]["event"], "tasksDelta")
        self.assertEqual(len(events[0]["data"]["rows"]), 1)
        self.assertLess(len(json.dumps(events[0])), 2500)
        self.assertEqual(model.rowCount(), 1000)

    def test_real_backend_restore_snapshot_and_graceful_exit_are_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = root / "data"
            data.mkdir()
            (data / "config.ini").write_text("[General]\nauto_check_updates=false\ndesktop_notifications=false\n", encoding="utf-8")
            (data / "languages").mkdir()
            (data / "languages" / "fr-FR.json").write_text(json.dumps({"schema_version": 1, "locale": "fr-FR", "name": "French", "native_name": "Français", "translations": {"Downloads": "Téléchargements ◷", "Settings": "Réglages"}}), encoding="utf-8")
            environment = {**os.environ, "HUIFA_APPLICATION_ROOT": str(root), "HUIFA_DATA_ROOT": str(data), "QT_QPA_PLATFORM": "offscreen", "PYTHONIOENCODING": "gbk"}
            process = subprocess.Popen([sys.executable, "-m", "app.electron_backend"], cwd=Path(__file__).resolve().parents[1], env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
            events = queue.Queue()
            def read():
                for line in process.stdout:
                    events.put(json.loads(line))
            threading.Thread(target=read, daemon=True).start()
            try:
                while True:
                    event = events.get(timeout=20)
                    self.assertNotEqual(event.get("type"), "fatal", event)
                    if event.get("event") == "ready":
                        break
                process.stdin.write(json.dumps({"id": 1, "target": "host", "action": "snapshot", "args": []}) + "\n")
                process.stdin.flush()
                while True:
                    event = events.get(timeout=10)
                    if event.get("id") == 1:
                        self.assertTrue(event["ok"], event)
                        self.assertEqual(event["value"]["settings"]["values"]["auto_check_updates"], "false")
                        self.assertEqual(event["value"]["tasks"]["rows"], [])
                        self.assertIn("fr-FR", [language["locale"] for language in event["value"]["settings"]["languages"]])
                        break
                process.stdin.write(json.dumps({"id": 2, "target": "settings", "action": "save", "args": [{"ui_language": "fr-FR"}, {}, ""]}) + "\n")
                process.stdin.flush()
                while True:
                    event = events.get(timeout=10)
                    if event.get("id") == 2:
                        self.assertTrue(event["ok"], event)
                        break
                process.stdin.write(json.dumps({"id": 3, "target": "host", "action": "snapshot", "args": []}) + "\n")
                process.stdin.flush()
                while True:
                    event = events.get(timeout=10)
                    if event.get("id") == 3:
                        self.assertEqual(event["value"]["settings"]["locale"], "fr-FR")
                        self.assertEqual(event["value"]["settings"]["translations"]["Settings"], "Réglages")
                        break
                process.stdin.write(json.dumps({"id": 4, "target": "app", "action": "requestShutdown", "args": []}) + "\n")
                process.stdin.flush()
                self.assertEqual(process.wait(timeout=20), 0, process.stderr.read())
                self.assertTrue((data / "app.db").is_file())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()

    def test_processing_error_text_has_no_url_credentials_or_tokens(self):
        application = QCoreApplication.instance() or QCoreApplication([])
        service = _Service()
        secret = "ACCEPTANCE_SECRET_51e6"
        message = "PROCESSING FAILED https://person:pass@example.test/video?token=" + secret
        task = DownloadTask(id="a", url="https://example.test/video", output_dir="downloads", status="processing_failed", stage_text=message, failed_step=message, error=message)
        service.tasks = {task.id: task}
        model = TaskListModel(service)
        host = ElectronHost.__new__(ElectronHost)
        QObject.__init__(host)
        host.bridge = SimpleNamespace(taskModel=model)
        host._assets = {}
        row = host.task_row(task)
        serialized = json.dumps(row)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("person:pass", serialized)
        self.assertIn("example.test/video", serialized)

    def test_saved_cover_intro_plan_is_serialized_without_current_settings(self):
        application = QCoreApplication.instance() or QCoreApplication([])
        service = _Service()
        task = DownloadTask(id="plan", url="https://example.test/video", output_dir="downloads", transcode_encoder="libsvtav1", options_json={"prepend_cover_enabled": True, "prepend_cover_frames": 9, "_processing_plan": {"cover_convert_jpeg": True, "cover_jpeg_quality": 82}})
        service.tasks = {task.id: task}
        host = ElectronHost.__new__(ElectronHost)
        QObject.__init__(host)
        host.bridge = SimpleNamespace(taskModel=TaskListModel(service))
        host._assets = {}
        self.assertEqual(host.task_row(task)["processingPlan"], {"encoder": "libsvtav1", "subtitles": "none", "jpeg": True, "jpegQuality": 82, "prependCover": True, "prependFrames": 9})

    def test_paused_tasks_do_not_report_work_in_progress(self):
        index = DownloadTaskIndex()
        for status in ("paused", "completed", "waiting_processing", "processing"):
            index.sync(status, parent_id="", status=status, speed_bps=0)
        statistics = index.statistics(top_level_only=True)
        self.assertEqual(statistics["in_progress"], 2)
        self.assertEqual(statistics["active"], 1)
        self.assertEqual(statistics["paused"], 1)

    def test_isolated_data_override_includes_downloads_and_update_receipts(self):
        from app.core.paths import downloads_dir
        from app.core.application_updater import _velopack_application_update_state_dir
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"HUIFA_DATA_ROOT": directory}):
            self.assertEqual(downloads_dir(), Path(directory).resolve() / "downloads")
            self.assertEqual(_velopack_application_update_state_dir(), Path(directory).resolve() / "updates" / "application")

    def test_bulk_pause_includes_running_and_waiting_processing(self):
        tasks = [DownloadTask(id=status, url="https://example.test/"+status, output_dir="downloads", status=status) for status in ("processing", "downloading", "paused", "completed")]
        tasks.append(DownloadTask(id="waiting", url="https://example.test/wait", output_dir="downloads", status="processing", stage="waiting_processing"))
        paused, messages = [], []
        bridge = SimpleNamespace(_runtime=SimpleNamespace(download_service=SimpleNamespace(tasks={task.id: task for task in tasks}, pause=paused.append)), _set_message=messages.append)
        AppBridge.pauseAllTasks(bridge)
        self.assertEqual(set(paused), {"processing", "downloading", "waiting"})
        self.assertIn("3", messages[0])


if __name__ == "__main__":
    unittest.main()
