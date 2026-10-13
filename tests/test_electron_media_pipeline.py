from __future__ import annotations

import functools
from contextlib import closing
import http.server
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FFMPEG = ROOT / "tools/ffmpeg/x64/ffmpeg.exe"
FFPROBE = ROOT / "tools/ffmpeg/x64/ffprobe.exe"


@unittest.skipUnless(FFMPEG.is_file() and FFPROBE.is_file(), "Bundled FFmpeg/FFprobe are required for the real local-media integration check")
class ElectronMediaPipelineTest(unittest.TestCase):
    def test_local_http_batch_uses_real_downloader_processing_and_atomic_catalog(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs, outputs, data = root / "inputs", root / "outputs", root / "data"
            for directory in (inputs, outputs, data):
                directory.mkdir()
            source = inputs / "first.mp4"
            generated = subprocess.run([str(FFMPEG), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=24", "-t", "2", "-c:v", "mpeg4", "-q:v", "4", "-pix_fmt", "yuv420p", str(source)], capture_output=True, text=True, timeout=30)
            self.assertEqual(generated.returncode, 0, generated.stderr)
            for name in ("second.mp4", "third.mp4"):
                shutil.copyfile(source, inputs / name)
            (data / "config.ini").write_text("[General]\nauto_check_updates=false\ndesktop_notifications=false\n", encoding="utf-8")

            class Handler(http.server.SimpleHTTPRequestHandler):
                def log_message(self, *_):
                    pass

            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(inputs)))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            environment = {**os.environ, "HUIFA_APPLICATION_ROOT": str(ROOT), "HUIFA_DATA_ROOT": str(data), "QT_QPA_PLATFORM": "offscreen", "PYTHONIOENCODING": "utf-8"}
            process = subprocess.Popen([sys.executable, "-m", "app.electron_backend"], cwd=ROOT, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
            inbox = queue.Queue()
            def read():
                for line in process.stdout:
                    inbox.put(json.loads(line))
            threading.Thread(target=read, daemon=True).start()
            counter = 0
            states = {}
            def consume(event):
                if event.get("event") == "state":
                    for row in event["data"].get("tasks", {}).get("rows", []):
                        states[row["taskId"]] = row
                if event.get("event") == "tasksDelta":
                    for row in event["data"]["rows"]:
                        states[row["taskId"]] = row
                self.assertNotEqual(event.get("type"), "fatal", event)
            def call(target, action, *arguments):
                nonlocal counter
                counter += 1
                process.stdin.write(json.dumps({"id": counter, "target": target, "action": action, "args": list(arguments)}) + "\n")
                process.stdin.flush()
                while True:
                    event = inbox.get(timeout=30)
                    consume(event)
                    if event.get("id") == counter:
                        self.assertTrue(event["ok"], event)
                        return event.get("value")
            try:
                while True:
                    event = inbox.get(timeout=20)
                    consume(event)
                    if event.get("event") == "ready":
                        break
                values = {"download_dir": str(outputs), "processing_temp_dir": str(root / "workspace"), "download_performance_mode": "manual", "max_concurrent": "2", "fragment_concurrent": "1", "request_delay": "0", "transcode_encoder": "libx264", "download_cover_convert_jpeg": "true", "cover_jpeg_quality": "82", "ffmpeg_path": str(FFMPEG), "ffprobe_path": str(FFPROBE)}
                self.assertTrue(call("settings", "save", values, {"collection_mode": "single", "content_mode": "video"}, ""))
                links = "\n".join(f"http://127.0.0.1:{server.server_port}/{name}" for name in ("first.mp4", "second.mp4", "third.mp4"))
                self.assertTrue(call("app", "submitUrls", links))
                self.assertTrue(call("settings", "save", {"transcode_encoder": "original", "download_cover_convert_jpeg": "false", "cover_jpeg_quality": "96"}, {}, ""))
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    snapshot = call("host", "snapshot")
                    rows = snapshot["tasks"]["rows"]
                    if len(rows) == 3 and all(row["status"] == "completed" for row in rows):
                        break
                    self.assertFalse(any(row["status"] in {"failed", "processing_failed"} for row in rows), rows)
                    time.sleep(0.2)
                else:
                    self.fail(f"Local media processing did not complete: {states}")
                for row in rows:
                    self.assertEqual(row["processingPlan"], {"encoder": "libx264", "subtitles": "none", "jpeg": True, "jpegQuality": 82, "prependCover": False, "prependFrames": 3})
                    path = Path(row["mediaPath"])
                    self.assertTrue(path.is_file())
                    probe = subprocess.run([str(FFPROBE), "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1", str(path)], capture_output=True, text=True, timeout=10)
                    self.assertEqual(probe.stdout.strip(), "h264")
                with closing(sqlite3.connect(data / "app.db")) as database:
                    self.assertEqual(database.execute("SELECT COUNT(*) FROM media_items").fetchone()[0], 3)
                    self.assertEqual(database.execute("SELECT COUNT(*) FROM download_tasks WHERE status='completed'").fetchone()[0], 3)
                call("app", "requestShutdown")
                self.assertEqual(process.wait(timeout=20), 0, process.stderr.read())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
