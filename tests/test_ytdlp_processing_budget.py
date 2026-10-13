from __future__ import annotations

import functools
import http.server
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication
from yt_dlp import YoutubeDL
from yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor

from app.core.download_service import DownloadService, DownloadTask, DownloadWorker, _WorkerCapacityBudget
from app.core.external_ytdlp import _ExternalProcessingGate
from app.storage.database import Database


ROOT = Path(__file__).resolve().parents[1]
FFMPEG = ROOT / "tools/ffmpeg/x64/ffmpeg.exe"
FFPROBE = ROOT / "tools/ffmpeg/x64/ffprobe.exe"
YTDLP = ROOT / "tools/yt-dlp/x64/yt-dlp.exe"


class WorkerCapacityBudgetTest(unittest.TestCase):
    def test_native_checkpoint_rejects_unrelated_files_and_changed_media_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            output.mkdir()
            database = Database(Path(temporary) / "tasks.db")
            worker = DownloadWorker("owned", "https://example.test/owned", str(output), database,
                                    native_task_title="Owned sample", native_source_key="generic:owned")
            origin = {"id": "owned", "title": "Owned sample", "ext": "mp4", "extractor_key": "Generic", "webpage_url": worker.url}
            try:
                with YoutubeDL({"outtmpl": str(output / "%(title)s [%(id)s].%(ext)s"), "quiet": True, "no_warnings": True}) as ydl:
                    source = Path(ydl.prepare_filename(origin))
                    source.write_bytes(b"owned media")
                    unrelated = output / "unrelated.mp4"
                    unrelated.write_bytes(b"unrelated media remains")
                    native = {"origin_info": origin, "info": {**origin, "filepath": str(source), "__finaldir": str(output)}, "processors": []}
                    self.assertEqual(worker._validate_native_processing_paths(native, ydl), [source])
                    mutations = [
                        {"filepath": str(unrelated)},
                        {"__files_to_move": {str(source): str(unrelated)}},
                        {"__files_to_move": {str(source): str(output / "another" / source.name)}},
                        {"__finaldir": str(output / "another")},
                    ]
                    for mutation in mutations:
                        with self.subTest(mutation=mutation):
                            altered = deepcopy(native)
                            altered["info"].update(mutation)
                            with self.assertRaises(ValueError):
                                worker._validate_native_processing_paths(altered, ydl)
                    changed_identity = deepcopy(native)
                    changed_identity["origin_info"]["id"] = "unrelated"
                    with self.assertRaisesRegex(ValueError, "来源与当前任务不匹配"):
                        worker._validate_native_processing_paths(changed_identity, ydl)
                    self.assertEqual(unrelated.read_bytes(), b"unrelated media remains")
                    self.assertEqual(source.read_bytes(), b"owned media")
            finally:
                database.close()

    def test_nested_postprocessors_keep_one_permit_until_the_outer_hook_finishes(self) -> None:
        budget = _WorkerCapacityBudget()
        first, second = object(), object()
        cancel, entered = threading.Event(), threading.Event()
        budget.acquire(first, cancel)
        budget.acquire(first, cancel)
        waiter = threading.Thread(target=lambda: (budget.acquire(second, cancel), entered.set()), daemon=True)
        waiter.start()
        budget.release(first)
        self.assertFalse(entered.wait(0.15))
        self.assertEqual(budget.active_count, 1)
        budget.release(first)
        self.assertTrue(entered.wait(2))
        budget.release(second)
        waiter.join(2)
        self.assertEqual(budget.active_count, 0)

    def test_canceling_a_waiter_never_releases_another_tasks_permit(self) -> None:
        budget = _WorkerCapacityBudget()
        first, second = object(), object()
        budget.acquire(first, threading.Event())
        cancel, stopped = threading.Event(), threading.Event()

        def wait() -> None:
            try:
                budget.acquire(second, cancel)
            except InterruptedError:
                stopped.set()

        waiter = threading.Thread(target=wait, daemon=True)
        waiter.start()
        cancel.set()
        self.assertTrue(stopped.wait(2))
        self.assertTrue(budget.holds(first))
        self.assertFalse(budget.holds(second))
        budget.release(first)
        waiter.join(2)
        self.assertEqual(budget.active_count, 0)

    def test_reset_task_cache_clears_every_processing_queue_and_runtime_id(self) -> None:
        app = QCoreApplication.instance() or QCoreApplication([])
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "tasks.db")
            service = DownloadService(database)
            service._processing_queue.append_unique("saved")
            service._processing_task_ids.add("saved")
            service._ytdlp_processing_task_ids.add("native")
            service.reset_task_cache()
            self.assertFalse(service._processing_queue)
            self.assertEqual(service._processing_task_ids, set())
            self.assertEqual(service._ytdlp_processing_task_ids, set())
            service.shutdown(0)
            database.close()
            app.processEvents()

    def test_missing_plugin_handshake_is_an_explicit_failure_and_temp_files_are_removed(self) -> None:
        gate = _ExternalProcessingGate(lambda _event: None)
        directory = Path(gate.directory.name)
        try:
            with self.assertRaisesRegex(RuntimeError, "未加载处理通道插件"):
                gate.verify()
        finally:
            gate.close()
        self.assertFalse(directory.exists())


@unittest.skipUnless(FFMPEG.is_file() and FFPROBE.is_file(), "Real split AV checks need bundled FFmpeg and FFprobe")
class RealYtdlpProcessingBudgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QCoreApplication.instance() or QCoreApplication([])
        cls.samples = tempfile.TemporaryDirectory(prefix="huifa-split-av-sample-")
        cls.sample_root = Path(cls.samples.name)
        generated = subprocess.run([
            str(FFMPEG), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            "testsrc2=size=160x90:rate=12", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
            "-t", "2", "-threads", "1", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
            "-f", "dash", "-seg_duration", "1", str(cls.sample_root / "sample.mpd"),
        ], cwd=cls.sample_root, capture_output=True, text=True, timeout=30)
        if generated.returncode:
            raise RuntimeError(generated.stderr)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.samples.cleanup()

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="huifa-split-av-budget-")
        self.root = Path(self.directory.name)
        self.inputs, self.outputs = self.root / "inputs", self.root / "outputs"
        self.inputs.mkdir()
        self.outputs.mkdir()
        for name in ("first", "second", "third"):
            target = self.inputs / name
            shutil.copytree(self.sample_root, target)
            (target / "sample.mpd").rename(target / f"{name}.mpd")
            metadata = {"@context": "https://schema.org", "@type": "VideoObject", "name": name, "duration": "PT2S", "contentUrl": f"{name}.mpd"}
            (target / f"{name}.html").write_text(
                f"<html><title>{name}</title><script type='application/ld+json'>{json.dumps(metadata)}</script></html>",
                encoding="utf-8",
            )
        self.requests: list[tuple[float, str]] = []
        requests = self.requests

        class Handler(http.server.SimpleHTTPRequestHandler):
            def do_GET(self) -> None:
                requests.append((time.monotonic(), self.path))
                super().do_GET()

            def log_message(self, *_args: object) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(self.inputs)))
        for name in ("first", "second", "third"):
            metadata = {"@context": "https://schema.org", "@type": "VideoObject", "name": name, "duration": "PT2S", "contentUrl": f"http://127.0.0.1:{self.server.server_port}/{name}/{name}.mpd"}
            (self.inputs / name / f"{name}.html").write_text(
                f"<html><title>{name}</title><script type='application/ld+json'>{json.dumps(metadata)}</script></html>", encoding="utf-8",
            )
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.database = Database(self.root / "tasks.db")
        self.service: DownloadService | None = None
        self.trace = self.root / "processing.jsonl"
        self.trace_lock = threading.Lock()
        self.plugin_directories: list[Path] = []

    def tearDown(self) -> None:
        if self.service is not None:
            self.service.request_shutdown()
            self._wait(lambda: self.service.active_thread_count == 0, timeout=15)
        self.database.close()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(2)
        self.directory.cleanup()

    def _wait(self, condition, *, timeout: float = 45) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if condition():
                return True
            time.sleep(0.01)
        return False

    def _record(self, state: str, kind: str, detail: str = "") -> None:
        with self.trace_lock:
            with self.trace.open("a", encoding="utf-8") as output:
                output.write(json.dumps({"time": time.monotonic(), "state": state, "kind": kind, "detail": detail}) + "\n")

    def _tracing(self, stack: ExitStack, *, fail_native_once: bool = False) -> None:
        original_ffmpeg = FFmpegPostProcessor.real_run_ffmpeg
        original_saved = DownloadWorker._prepare_transcode_output
        failure_marker = self.root / "native-failed-once"

        def traced_ffmpeg(processor, *args, **kwargs):
            self._record("started", "native", repr(args)[:1024])
            try:
                time.sleep(1)
                if fail_native_once and not failure_marker.exists():
                    from yt_dlp.utils import PostProcessingError
                    failure_marker.touch()
                    raise PostProcessingError("fixture native merge failure")
                return original_ffmpeg(processor, *args, **kwargs)
            finally:
                self._record("finished", "native")

        def traced_saved(worker, *args, **kwargs):
            self._record("started", "saved", worker.task_id)
            try:
                time.sleep(0.1)
                return original_saved(worker, *args, **kwargs)
            finally:
                self._record("finished", "saved", worker.task_id)

        stack.enter_context(patch.object(FFmpegPostProcessor, "real_run_ffmpeg", traced_ffmpeg))
        stack.enter_context(patch.object(DownloadWorker, "_prepare_transcode_output", traced_saved))
        stack.enter_context(patch("app.core.download_service.detect_public_ip", return_value="local-fixture"))
        probe = self.root / "plugins" / "probe" / "yt_dlp_plugins" / "postprocessor" / "trace_launch.py"
        probe.parent.mkdir(parents=True)
        probe.write_text(
            "import json, time\nfrom yt_dlp.postprocessor.ffmpeg import FFmpegPostProcessor\n"
            + "from pathlib import Path\nfrom yt_dlp.utils import PostProcessingError\n"
            + "_trace_path = " + repr(str(self.trace)) + "\n"
            + "_failure_marker = Path(" + repr(str(failure_marker)) + ")\n"
            + "_fail_once = " + repr(fail_native_once) + "\n"
            + "def _record(state, detail=''):\n"
            + "    with open(_trace_path, 'a', encoding='utf-8') as output:\n"
            + "        output.write(json.dumps({'time': time.monotonic(), 'state': state, 'kind': 'native', 'detail': detail}) + '\\n')\n"
            + "_original = FFmpegPostProcessor.real_run_ffmpeg\n"
            + "def _traced(self, *args, **kwargs):\n"
            + "    _record('started', repr(args)[:1024])\n"
            + "    try:\n        time.sleep(1)\n"
            + "        if _fail_once and not _failure_marker.exists():\n"
            + "            _failure_marker.touch()\n            raise PostProcessingError('fixture native merge failure')\n"
            + "        return _original(self, *args, **kwargs)\n"
            + "    finally:\n        _record('finished')\n"
            + "FFmpegPostProcessor.real_run_ffmpeg = _traced\n", encoding="utf-8",
        )
        original_configure = _ExternalProcessingGate.configure
        directories = self.plugin_directories

        def configure(gate, command):
            directories.append(Path(gate.directory.name))
            original_configure(gate, command)
            command[-2:-2] = ["--plugin-dirs", str(self.root / "plugins")]

        stack.enter_context(patch.object(_ExternalProcessingGate, "configure", configure))

    def _start(self, mode: str, names: tuple[str, ...] = ("first", "second", "third")) -> list[DownloadTask]:
        self.service = DownloadService(self.database, max_concurrent=2, fragment_concurrent=1, ytdlp_core_mode=mode, ffprobe_path=str(FFPROBE))
        tasks = []
        for name in names:
            task = DownloadTask(
                name, f"http://127.0.0.1:{self.server.server_port}/{name}/{name}.html", str(self.outputs),
                ffmpeg_path=str(FFMPEG), format_selector="bestvideo+bestaudio/best", playlist_mode="single",
                transcode_codec="h264", transcode_encoder="libx264",
                options_json={"processing_temp_dir": str(self.root / "workspace")},
            )
            self.database.insert_download_task(task)
            self.service._register_task(task)
            self.service.queue.append_unique(task.id)
            tasks.append(task)
        return tasks

    def _finish_and_assert(self, tasks: list[DownloadTask]) -> list[dict]:
        self.assertTrue(self._wait(lambda: all(task.status in {"completed", "failed", "processing_failed", "canceled"} for task in tasks)))
        self.assertTrue(self._wait(lambda: self.service.active_thread_count == 0))
        for task in tasks:
            if task.status == "canceled":
                continue
            self.assertEqual(task.status, "completed", task.error)
            result = subprocess.run([str(FFPROBE), "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "json", task.media_path], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            streams = json.loads(result.stdout)["streams"]
            self.assertEqual({stream["codec_type"] for stream in streams}, {"video", "audio"})
            self.assertEqual(next(stream["codec_name"] for stream in streams if stream["codec_type"] == "video"), "h264")
        events = [json.loads(line) for line in self.trace.read_text(encoding="utf-8").splitlines()]
        active, maximum = 0, 0
        for event in sorted(events, key=lambda item: item["time"]):
            active += 1 if event["state"] == "started" else -1
            maximum = max(maximum, active)
            self.assertIn(active, (0, 1), events)
        self.assertEqual((active, maximum), (0, 1))
        self.assertEqual(self.service._processing_budget.active_count, 0)
        self.assertEqual(self.service._network_budget.active_count, 0)
        self.assertTrue(all(not directory.exists() for directory in self.plugin_directories))
        return events

    def _assert_real_split_concurrency(self, mode: str) -> None:
        with ExitStack() as stack:
            self._tracing(stack)
            tasks = self._start(mode)
            blocker = object()
            self.service._processing_budget.acquire(blocker, threading.Event())
            try:
                self.service._start_next()
                self.assertTrue(self._wait(lambda: sum(task.stage == "waiting_processing" for task in tasks) >= 2 or any(task.status in {"failed", "processing_failed"} for task in tasks)))
                self.assertGreaterEqual(sum(task.stage == "waiting_processing" for task in tasks), 2, {"tasks": [(task.status, task.stage, task.error, task.options_json.get("_storage_preview")) for task in tasks], "requests": self.requests[:30]})
                self.assertTrue(any(".m4s" in path for _, path in self.requests))
                self.assertFalse(self.trace.exists(), "The real FFmpeg launcher ran before receiving a processing permit")
                # The child is blocked inside its started hook, while the
                # downloaded split AV files remain intact on disk.
                time.sleep(0.15)
                self.assertFalse(self.trace.exists())
            finally:
                self.service._processing_budget.release(blocker)
            events = self._finish_and_assert(tasks)
            self.assertEqual(sum(event["kind"] == "native" and event["state"] == "started" for event in events), 3)
            self.assertEqual(sum(event["kind"] == "saved" and event["state"] == "started" for event in events), 3)
            intervals = []
            start = None
            for event in sorted(events, key=lambda item: item["time"]):
                if event["state"] == "started":
                    start = event["time"]
                elif start is not None:
                    intervals.append((start, event["time"]))
            self.assertTrue(any(
                "/third/" in path and ".m4s" in path and any(start <= moment <= finish for start, finish in intervals)
                for moment, path in self.requests
            ), "The third real download did not use the network slot released by processing")

    def test_builtin_merge_and_saved_transcode_share_one_permit_and_release_network_slots(self) -> None:
        self._assert_real_split_concurrency("builtin")

    @unittest.skipUnless(YTDLP.is_file(), "Real external gate check needs the shipped official yt-dlp executable")
    def test_official_external_merge_is_gated_before_launch_and_shares_the_saved_queue(self) -> None:
        self._assert_real_split_concurrency("external")

    def _assert_native_failure_restart_offline(self, mode: str, *, after_move: bool = False) -> None:
        with ExitStack() as stack:
            self._tracing(stack, fail_native_once=not after_move)
            task = self._start(mode, ("first",))[0]
            if after_move:
                original_update = self.database.update_download_task
                failed_ack = False

                def update(candidate):
                    nonlocal failed_ack
                    native = candidate.options_json.get("_processing", {}).get("native", {})
                    if not failed_ack and native and native.get("processors") == []:
                        failed_ack = True
                        raise RuntimeError("fixture native move checkpoint acknowledgement failure")
                    return original_update(candidate)

                stack.enter_context(patch.object(self.database, "update_download_task", update))
            self.service._start_next()
            self.assertTrue(self._wait(lambda: task.status in {"failed", "processing_failed", "completed"}))
            self.assertEqual(task.status, "processing_failed", task.error)
            self.assertTrue(self._wait(lambda: self.service.active_thread_count == 0))
            checkpoint = task.options_json.get("_processing")
            self.assertIsInstance(checkpoint, dict)
            self.assertEqual(checkpoint["kind"], "native")
            self.assertEqual(checkpoint["native"]["processors"][0]["key"], "MoveFilesAfterDownload" if after_move else "FFmpegMerger")
            if after_move:
                source = checkpoint["native"]["info"]["filepath"]
                target = checkpoint["native"]["info"]["__files_to_move"][source]
                self.assertFalse(Path(source).exists())
                self.assertTrue(Path(target).is_file() and Path(target).stat().st_size > 0)
            else:
                sources = [Path(value) for value in checkpoint["native"]["info"]["__files_to_merge"]]
                self.assertEqual(len(sources), 2)
                self.assertTrue(all(path.is_file() and path.stat().st_size > 0 for path in sources))
            self.assertEqual(self.service._processing_budget.active_count, 0)
            self.assertEqual(self.service._network_budget.active_count, 0)
            self.assertTrue(all(not directory.exists() for directory in self.plugin_directories))
            requests_before_retry = len(self.requests)
            self.server.shutdown()
            self.server.server_close()
            self.server_thread.join(2)
            self.service.shutdown(0)
            self.service = DownloadService(self.database, max_concurrent=2, ytdlp_core_mode=mode, ffprobe_path=str(FFPROBE))
            self.service.restore_tasks()
            restored = self.service.tasks[task.id]
            self.assertEqual(restored.status, "processing_failed")
            self.assertEqual(restored.options_json["_processing"]["native"], checkpoint["native"])
            stack.enter_context(patch.object(YoutubeDL, "extract_info", side_effect=AssertionError("Offline processing extracted a URL")))
            stack.enter_context(patch("app.core.download_service.run_external_ytdlp", side_effect=AssertionError("Offline processing launched a downloader")))
            self.service.retry(restored.id)
            events = self._finish_and_assert([restored])
            self.assertEqual(len(self.requests), requests_before_retry)
            self.assertEqual(sum(event["kind"] == "native" and event["state"] == "started" for event in events), 1 if after_move else 2)
            self.assertEqual(sum(event["kind"] == "saved" and event["state"] == "started" for event in events), 1)
            self.assertNotIn("_processing", restored.options_json)

    def test_builtin_native_merge_failure_survives_restart_and_retries_offline(self) -> None:
        self._assert_native_failure_restart_offline("builtin")

    @unittest.skipUnless(YTDLP.is_file(), "Real external failure replay check needs official yt-dlp")
    def test_external_native_merge_failure_survives_restart_and_retries_offline(self) -> None:
        self._assert_native_failure_restart_offline("external")

    def test_builtin_move_checkpoint_ack_failure_reconciles_the_published_file_offline(self) -> None:
        self._assert_native_failure_restart_offline("builtin", after_move=True)

    @unittest.skipUnless(YTDLP.is_file(), "Real external move replay check needs official yt-dlp")
    def test_external_move_checkpoint_ack_failure_reconciles_the_published_file_offline(self) -> None:
        self._assert_native_failure_restart_offline("external", after_move=True)

    @unittest.skipUnless(YTDLP.is_file(), "Real external cancellation check needs official yt-dlp")
    def test_canceling_external_pp_waiter_stops_the_child_and_releases_its_permits(self) -> None:
        with ExitStack() as stack:
            self._tracing(stack)
            tasks = self._start("external")
            blocker = object()
            self.service._processing_budget.acquire(blocker, threading.Event())
            try:
                self.service._start_next()
                self.assertTrue(self._wait(lambda: tasks[0].stage == "waiting_processing"))
                self.service.cancel(tasks[0].id)
                self.assertTrue(self._wait(lambda: tasks[0].id not in self.service.workers))
                self.assertEqual(tasks[0].status, "canceled")
                self.assertEqual(self.service._processing_budget.active_count, 1)
                self.assertFalse(self.trace.exists(), "Canceled child launched FFmpeg while waiting")
            finally:
                self.service._processing_budget.release(blocker)
            events = self._finish_and_assert(tasks)
            self.assertEqual(sum(event["kind"] == "native" and event["state"] == "started" for event in events), 2)
            self.assertFalse(any("first.f" in event["detail"] for event in events))


if __name__ == "__main__":
    unittest.main()
