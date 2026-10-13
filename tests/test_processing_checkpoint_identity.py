from __future__ import annotations

import unittest
from pathlib import Path

from app.core.download_service import DownloadWorker
from app.core.transcode_service import PreparedTranscode
from app.storage.models import MediaItem
from tests.test_download_processing_checkpoint import ProcessingCheckpointTest


class ProcessingCheckpointIdentityTest(ProcessingCheckpointTest):
    def test_unrelated_prepared_entry_does_not_replace_same_folder_file(self):
        task = self._task()
        original = Path(task.options_json["_processing"]["entries"][0]["filepath"])
        unrelated = self.output / "unrelated.mp4"
        unrelated.write_bytes(b"unrelated media remains")
        workspace = Path(task.options_json["_processing"]["workspace"])
        temporary = workspace / ".unrelated.transcoding-1-2.part.mp4"
        temporary.write_bytes(b"prepared malicious result")
        task.options_json["_processing"]["prepared_entries"] = [DownloadWorker._prepared_processing_entry(
            MediaItem(source_url=task.url, title=task.title, video_path=str(unrelated)),
            PreparedTranscode(unrelated, unrelated, temporary, "libx264", False),
        )]
        worker = self._worker(task)
        self._run(worker)
        self.assertEqual(task.status, "processing_failed")
        self.assertEqual(unrelated.read_bytes(), b"unrelated media remains")
        self.assertEqual(original.read_bytes(), b"original media")
        self.assertTrue(temporary.is_file())
        self.assertEqual(self.db.list_media(), [])

    def test_legitimate_extension_change_is_accepted(self):
        task = self._task()
        checkpoint = task.options_json["_processing"]
        original = Path(checkpoint["entries"][0]["filepath"])
        source = original.with_suffix(".webm")
        original.replace(source)
        checkpoint["entries"][0]["filepath"] = str(source)
        target = source.with_suffix(".mp4")
        workspace = Path(checkpoint["workspace"])
        temporary = workspace / ".video.transcoding-1-2.part.mp4"
        temporary.write_bytes(b"converted media")
        checkpoint["prepared_entries"] = [DownloadWorker._prepared_processing_entry(
            MediaItem(source_url=task.url, title=task.title, video_path=str(target)),
            PreparedTranscode(source, target, temporary, "libx264", False),
        )]
        worker = self._worker(task)
        self._run(worker)
        self.assertEqual(task.status, "completed")
        self.assertEqual(target.read_bytes(), b"converted media")
        self.assertEqual(self.db.list_media()[0].video_path, str(target))

    def test_all_publications_are_checked_before_any_rename(self):
        task = self._task(prepared=True)
        checkpoint = task.options_json["_processing"]
        raw = checkpoint["prepared_entries"][0]["transcode"]
        source = Path(raw["source_path"])
        temporary = Path(raw["temporary_path"])
        backup = source.with_name(".video.mp4.pre-transcode-1-2.bak")
        source.replace(backup)
        temporary.replace(source)
        checkpoint["publications"] = [{**raw, "backup_path": str(backup)}, {"source_path": str(self.output / "unrelated.mp4")}]
        worker = self._worker(task)
        self._run(worker)
        self.assertEqual(task.status, "processing_failed")
        self.assertTrue(backup.is_file())
        self.assertEqual(source.read_bytes(), b"converted media")
        self.assertFalse(temporary.exists())
        self.assertEqual(self.db.list_media(), [])


if __name__ == "__main__":
    unittest.main()
