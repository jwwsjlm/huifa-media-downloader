from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import requests

from app.core.application_updater import (
    ApplicationUpdaterError,
    ApplicationUpdate,
    PortableExeApplicationUpdater,
    UpdateCheckError,
    UpdateDownloadCancelled,
    UpdateDownloadError,
    UpdateInstallError,
    VelopackUpdaterConfig,
    portable_single_exe_supported,
)


def valid_pe(marker: bytes = b"release") -> bytes:
    payload = bytearray(1024)
    payload[0:2] = b"MZ"
    payload[0x3C:0x40] = (0x80).to_bytes(4, "little")
    payload[0x80:0x84] = b"PE\x00\x00"
    payload[0x100:0x100 + len(marker)] = marker
    return bytes(payload)


def large_valid_pe(size: int = 2 * 1024 * 1024 + 257) -> bytes:
    payload = bytearray(size)
    payload[0:2] = b"MZ"
    payload[0x3C:0x40] = (0x80).to_bytes(4, "little")
    payload[0x80:0x84] = b"PE\x00\x00"
    for offset in range(0x100, size):
        payload[offset] = offset % 251
    return bytes(payload)


class FakeResponse:
    def __init__(
        self,
        *,
        json_value=None,
        body: bytes = b"",
        url: str = "",
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        error_after_chunks: int | None = None,
        close_error: Exception | None = None,
    ):
        self.json_value = json_value
        self.body = body
        self.url = url
        self.status_code = status_code
        self.headers = dict(headers or {})
        self.error_after_chunks = error_after_chunks
        self.close_error = close_error
        self.closed = False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self.json_value

    def iter_content(self, chunk_size: int):
        for index, offset in enumerate(range(0, len(self.body), max(1, chunk_size))):
            if self.error_after_chunks is not None and index >= self.error_after_chunks:
                raise requests.ConnectionError("simulated connection reset")
            yield self.body[offset:offset + chunk_size]
        if self.error_after_chunks is not None:
            chunk_count = (len(self.body) + max(1, chunk_size) - 1) // max(1, chunk_size)
            if chunk_count <= self.error_after_chunks:
                raise requests.ConnectionError("simulated connection reset")

    def close(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


class FakeSession:
    def __init__(
        self,
        api_response: FakeResponse,
        download_response: FakeResponse | list[FakeResponse] | None = None,
    ):
        self.api_response = api_response
        self.download_responses = (
            list(download_response)
            if isinstance(download_response, list)
            else [download_response] if download_response is not None else []
        )
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        if "/releases/download/" not in url:
            return self.api_response
        if not self.download_responses:
            raise AssertionError("unexpected extra download request")
        return self.download_responses.pop(0)


def release_payload(body: bytes, *, digest: str | None = None, tag: str = "v0.2.0") -> dict:
    digest = digest if digest is not None else "sha256:" + hashlib.sha256(body).hexdigest()
    return {
        "tag_name": tag,
        "draft": False,
        "prerelease": False,
        "body": "# 更新内容\n\n- 提升下载稳定性",
        "assets": [
            {
                "name": "HuifaVideoDownloader.exe",
                "state": "uploaded",
                "size": len(body),
                "digest": digest,
                "browser_download_url": (
                    "https://github.com/huifa/yt-release/releases/download/v0.2.0/"
                    "HuifaVideoDownloader.exe"
                ),
            }
        ],
    }


class PortableApplicationUpdaterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.executable = self.root / "HuifaVideoDownloader.exe"
        self.executable.write_bytes(valid_pe(b"current"))
        self.body = valid_pe(b"new-release")

    def create_updater(self, session: FakeSession) -> PortableExeApplicationUpdater:
        return PortableExeApplicationUpdater(
            VelopackUpdaterConfig(repository="huifa/yt-release"),
            self.root / "data" / "updates" / "application",
            executable_path=self.executable,
            current_version="0.1.0",
            session=session,
            process_id=4321,
        )

    def checked_update(self, updater: PortableExeApplicationUpdater) -> ApplicationUpdate:
        update = updater.check_for_updates()
        self.assertIsNotNone(update)
        return update

    def test_check_uses_exact_single_exe_asset_digest_and_release_notes(self) -> None:
        api = FakeResponse(json_value=release_payload(self.body))
        session = FakeSession(api)
        updater = self.create_updater(session)

        update = self.checked_update(updater)

        self.assertEqual(update.version, "0.2.0")
        self.assertEqual(update.file_name, "HuifaVideoDownloader.exe")
        self.assertEqual(update.sha256, hashlib.sha256(self.body).hexdigest())
        self.assertEqual(update.delivery_kind, "single-exe")
        self.assertTrue(update.is_portable)
        self.assertIn("提升下载稳定性", update.release_notes_markdown)
        self.assertEqual(
            session.calls[0][0],
            "https://api.github.com/repos/huifa/yt-release/releases/latest",
        )
        self.assertEqual(
            session.calls[0][1]["headers"]["X-GitHub-Api-Version"],
            "2026-03-10",
        )

    def test_prerelease_check_uses_release_list_api(self) -> None:
        release = release_payload(self.body)
        release["prerelease"] = True
        session = FakeSession(FakeResponse(json_value=[release]))
        updater = PortableExeApplicationUpdater(
            VelopackUpdaterConfig(repository="huifa/yt-release", prerelease=True),
            self.root / "data" / "updates" / "application",
            executable_path=self.executable,
            current_version="0.1.0",
            session=session,
            process_id=4321,
        )

        update = self.checked_update(updater)

        self.assertEqual(update.version, "0.2.0")
        self.assertEqual(
            session.calls[0][0],
            "https://api.github.com/repos/huifa/yt-release/releases?per_page=30",
        )

    def test_check_result_is_not_replaced_by_response_close_failure(self) -> None:
        api = FakeResponse(
            json_value=release_payload(self.body),
            close_error=OSError("simulated socket close failure"),
        )
        updater = self.create_updater(FakeSession(api))

        update = self.checked_update(updater)

        self.assertEqual(update.version, "0.2.0")
        self.assertTrue(api.closed)

    def test_check_rejects_release_without_github_sha256_digest(self) -> None:
        payload = release_payload(self.body, digest="")
        updater = self.create_updater(FakeSession(FakeResponse(json_value=payload)))

        with self.assertRaisesRegex(UpdateCheckError, "SHA-256"):
            updater.check_for_updates()

    def test_download_is_atomic_verified_and_restorable_after_restart(self) -> None:
        api = FakeResponse(json_value=release_payload(self.body))
        download = FakeResponse(
            body=self.body,
            url=(
                "https://release-assets.githubusercontent.com/github-production-release-asset/"
                "demo/HuifaVideoDownloader.exe"
            ),
        )
        session = FakeSession(api, download)
        updater = self.create_updater(session)
        update = self.checked_update(updater)
        progress: list[int] = []

        downloaded = updater.download_update(update, progress.append)

        self.assertTrue(downloaded.downloaded)
        self.assertEqual(progress[0], 0)
        self.assertEqual(progress[-1], 100)
        pending = updater._pending_path
        self.assertTrue(pending.is_file())
        manifest = json.loads(pending.read_text(encoding="utf-8"))
        self.assertEqual(manifest["sha256"], downloaded.sha256)
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["file_name"], "HuifaVideoDownloader-0.2.0.exe")
        self.assertNotIn("path", manifest)
        self.assertFalse(Path(manifest["file_name"]).is_absolute())
        self.assertFalse(any(updater.state_dir.glob("*.part")))

        restored_updater = self.create_updater(FakeSession(FakeResponse(json_value=[])))
        restored = restored_updater.pending_restart()
        self.assertIsNotNone(restored)
        self.assertTrue(restored.downloaded)
        self.assertEqual(restored.version, "0.2.0")
        self.assertEqual(restored.delivery_kind, "single-exe")

    def test_progress_callback_failure_cannot_undo_a_committed_download(self) -> None:
        api = FakeResponse(json_value=release_payload(self.body))
        download = FakeResponse(
            body=self.body,
            url="https://objects.githubusercontent.com/releases/HuifaVideoDownloader.exe",
        )
        updater = self.create_updater(FakeSession(api, download))

        downloaded = updater.download_update(
            self.checked_update(updater),
            lambda _value: (_ for _ in ()).throw(RuntimeError("stale UI receiver")),
        )

        self.assertTrue(downloaded.downloaded)
        self.assertTrue(updater._pending_path.is_file())

    def test_empty_response_is_rejected_without_leaving_a_fake_resume(self) -> None:
        api = FakeResponse(json_value=release_payload(self.body))
        download = FakeResponse(
            body=b"",
            url="https://objects.githubusercontent.com/releases/HuifaVideoDownloader.exe",
        )
        updater = self.create_updater(FakeSession(api, download))

        with self.assertRaisesRegex(UpdateDownloadError, "未返回可下载内容"):
            updater.download_update(self.checked_update(updater))

        self.assertEqual(list(updater.state_dir.glob("*.part")), [])
        self.assertEqual(list(updater.state_dir.glob("*.part.json")), [])

    def test_download_rejects_update_model_that_no_longer_matches_checked_asset(self) -> None:
        api = FakeResponse(json_value=release_payload(self.body))
        session = FakeSession(api)
        updater = self.create_updater(session)
        update = self.checked_update(updater)

        with self.assertRaisesRegex(ApplicationUpdaterError, "发布元数据不一致"):
            updater.download_update(replace(update, size_bytes=update.size_bytes + 1))

        self.assertEqual(
            [call for call in session.calls if "/releases/download/" in call[0]],
            [],
        )

    def test_completed_download_is_not_replaced_by_response_close_failure(self) -> None:
        api = FakeResponse(json_value=release_payload(self.body))
        download = FakeResponse(
            body=self.body,
            url=(
                "https://release-assets.githubusercontent.com/github-production-release-asset/"
                "demo/HuifaVideoDownloader.exe"
            ),
            close_error=OSError("simulated socket close failure"),
        )
        updater = self.create_updater(FakeSession(api, download))

        downloaded = updater.download_update(self.checked_update(updater))

        self.assertTrue(downloaded.downloaded)
        self.assertTrue(download.closed)

    def test_completed_part_survives_pending_manifest_failure_and_retries_locally(self) -> None:
        api = FakeResponse(json_value=release_payload(self.body))
        download = FakeResponse(
            body=self.body,
            url="https://objects.githubusercontent.com/releases/HuifaVideoDownloader.exe",
        )
        session = FakeSession(api, download)
        updater = self.create_updater(session)
        update = self.checked_update(updater)

        with patch.object(
            updater,
            "_write_pending_manifest",
            side_effect=OSError("state directory unavailable"),
        ):
            with self.assertRaisesRegex(UpdateDownloadError, "完整下载已保留"):
                updater.download_update(update)

        partial = next(updater.state_dir.glob("*.part"))
        self.assertEqual(partial.read_bytes(), self.body)
        self.assertTrue(partial.with_name(partial.name + ".json").is_file())
        self.assertFalse(any(updater.state_dir.glob("HuifaVideoDownloader-*.exe")))
        call_count = len(session.calls)

        downloaded = updater.download_update(update)

        self.assertTrue(downloaded.downloaded)
        self.assertEqual(len(session.calls), call_count)
        self.assertFalse(partial.exists())
        self.assertTrue(updater._pending_path.is_file())

    def test_cancel_during_complete_part_validation_does_not_publish_update(self) -> None:
        api = FakeResponse(json_value=release_payload(self.body))
        updater = self.create_updater(FakeSession(api))
        update = self.checked_update(updater)
        plan = updater._portable_download_plan(update)
        plan.temporary.write_bytes(self.body)
        plan.resume_manifest.write_text(
            json.dumps({
                "schema_version": 1,
                "url": str(plan.asset["url"]),
                "version": update.version,
                "size_bytes": plan.expected_size,
                "sha256": plan.expected_digest,
                "etag": "",
                "last_modified": "",
            }),
            encoding="utf-8",
        )
        checks = 0

        def cancel_during_validation() -> bool:
            nonlocal checks
            checks += 1
            return checks >= 3

        with self.assertRaises(UpdateDownloadCancelled):
            updater.download_update(
                update,
                cancel_callback=cancel_during_validation,
            )

        self.assertTrue(plan.temporary.is_file())
        self.assertFalse(plan.target.exists())
        self.assertFalse(updater._pending_path.exists())

    def test_fresh_transfer_does_not_hash_the_complete_package_twice(self) -> None:
        api = FakeResponse(json_value=release_payload(self.body))
        download = FakeResponse(
            body=self.body,
            url="https://objects.githubusercontent.com/releases/HuifaVideoDownloader.exe",
        )
        updater = self.create_updater(FakeSession(api, download))
        update = self.checked_update(updater)

        with patch.object(
            updater,
            "_validate_download",
            wraps=updater._validate_download,
        ) as validate:
            downloaded = updater.download_update(update)

        self.assertTrue(downloaded.downloaded)
        validate.assert_not_called()

    def test_tampered_download_is_removed_instead_of_restored(self) -> None:
        api = FakeResponse(json_value=release_payload(self.body))
        download = FakeResponse(
            body=self.body,
            url="https://objects.githubusercontent.com/releases/HuifaVideoDownloader.exe",
        )
        updater = self.create_updater(FakeSession(api, download))
        downloaded = updater.download_update(self.checked_update(updater))
        source = updater._downloaded_paths[downloaded.token]
        source.write_bytes(valid_pe(b"tampered"))

        restored_updater = self.create_updater(FakeSession(FakeResponse(json_value=[])))
        self.assertIsNone(restored_updater.pending_restart())
        self.assertFalse(source.exists())
        self.assertFalse(restored_updater._pending_path.exists())

    def test_pending_restart_discards_an_update_already_running(self) -> None:
        api = FakeResponse(json_value=release_payload(self.body))
        download = FakeResponse(
            body=self.body,
            url="https://objects.githubusercontent.com/releases/HuifaVideoDownloader.exe",
        )
        updater = self.create_updater(FakeSession(api, download))
        downloaded = updater.download_update(self.checked_update(updater))
        source = updater._downloaded_paths[downloaded.token]
        current = PortableExeApplicationUpdater(
            VelopackUpdaterConfig(repository="huifa/yt-release"),
            updater.state_dir,
            executable_path=self.executable,
            current_version="0.2.0",
            session=FakeSession(FakeResponse(json_value=[])),
            process_id=4321,
        )

        self.assertIsNone(current.pending_restart())
        self.assertFalse(source.exists())
        self.assertFalse(current._pending_path.exists())

    def test_pending_restart_rejects_relative_path_escape_without_touching_target(self) -> None:
        updater = self.create_updater(FakeSession(FakeResponse(json_value=[])))
        updater.state_dir.mkdir(parents=True, exist_ok=True)
        outside = updater.state_dir.parent / "outside.exe"
        outside.write_bytes(self.body)
        updater._pending_path.write_text(
            json.dumps({
                "schema_version": 1,
                "file_name": "../outside.exe",
                "current_version": "0.1.0",
                "version": "0.2.0",
                "size_bytes": len(self.body),
                "sha256": hashlib.sha256(self.body).hexdigest(),
            }),
            encoding="utf-8",
        )

        self.assertIsNone(updater.pending_restart())
        self.assertTrue(outside.is_file())
        self.assertFalse(updater._pending_path.exists())

    def test_incomplete_download_keeps_verified_resume_state(self) -> None:
        api = FakeResponse(json_value=release_payload(self.body))
        short = self.body[:-1]
        download = FakeResponse(
            body=short,
            url="https://objects.githubusercontent.com/releases/HuifaVideoDownloader.exe",
        )
        updater = self.create_updater(FakeSession(api, download))

        with self.assertRaisesRegex(UpdateDownloadError, "再次下载将从断点继续"):
            updater.download_update(self.checked_update(updater))
        partials = list(updater.state_dir.glob("*.part"))
        self.assertEqual(len(partials), 1)
        self.assertEqual(partials[0].read_bytes(), short)
        resume = partials[0].with_name(partials[0].name + ".json")
        self.assertTrue(resume.is_file())
        payload = json.loads(resume.read_text(encoding="utf-8"))
        self.assertEqual(payload["sha256"], hashlib.sha256(self.body).hexdigest())

    def test_network_interruption_resumes_with_range_and_if_range(self) -> None:
        body = large_valid_pe()
        api = FakeResponse(json_value=release_payload(body))
        asset_url = "https://objects.githubusercontent.com/releases/HuifaVideoDownloader.exe"
        first = FakeResponse(
            body=body,
            url=asset_url,
            headers={"ETag": '"asset-v1"', "Last-Modified": "Mon, 24 Aug 2026 08:00:00 GMT"},
            error_after_chunks=1,
        )
        offset = 1024 * 1024
        second = FakeResponse(
            body=body[offset:],
            url=asset_url,
            status_code=206,
            headers={
                "ETag": '"asset-v1"',
                "Content-Range": f"bytes {offset}-{len(body) - 1}/{len(body)}",
            },
        )
        session = FakeSession(api, [first, second])
        updater = self.create_updater(session)
        update = self.checked_update(updater)

        with self.assertRaisesRegex(UpdateDownloadError, "已保留"):
            updater.download_update(update)
        partial = next(updater.state_dir.glob("*.part"))
        self.assertEqual(partial.stat().st_size, offset)

        progress: list[int] = []
        downloaded = updater.download_update(update, progress.append)

        self.assertTrue(downloaded.downloaded)
        download_calls = [call for call in session.calls if "/releases/download/" in call[0]]
        self.assertEqual(download_calls[1][1]["headers"]["Range"], f"bytes={offset}-")
        self.assertEqual(download_calls[1][1]["headers"]["If-Range"], '"asset-v1"')
        self.assertEqual(download_calls[1][1]["headers"]["Accept-Encoding"], "identity")
        self.assertGreater(progress[1], 0)
        target = updater._downloaded_paths[downloaded.token]
        self.assertEqual(target.read_bytes(), body)
        self.assertFalse(any(updater.state_dir.glob("*.part")))
        self.assertFalse(any(updater.state_dir.glob("*.part.json")))

    def test_server_ignoring_range_restarts_without_appending_duplicate_bytes(self) -> None:
        body = large_valid_pe()
        api = FakeResponse(json_value=release_payload(body))
        asset_url = "https://release-assets.githubusercontent.com/releases/HuifaVideoDownloader.exe"
        first = FakeResponse(
            body=body,
            url=asset_url,
            headers={"ETag": '"asset-v1"'},
            error_after_chunks=1,
        )
        full_replacement = FakeResponse(
            body=body,
            url=asset_url,
            status_code=200,
            headers={"ETag": '"asset-v2"'},
        )
        session = FakeSession(api, [first, full_replacement])
        updater = self.create_updater(session)
        update = self.checked_update(updater)
        with self.assertRaises(UpdateDownloadError):
            updater.download_update(update)

        downloaded = updater.download_update(update)

        calls = [call for call in session.calls if "/releases/download/" in call[0]]
        self.assertIn("Range", calls[1][1]["headers"])
        self.assertEqual(calls[1][1]["headers"]["If-Range"], '"asset-v1"')
        self.assertEqual(updater._downloaded_paths[downloaded.token].read_bytes(), body)

    def test_malformed_partial_response_is_discarded_and_retried_once_from_zero(self) -> None:
        body = large_valid_pe()
        api = FakeResponse(json_value=release_payload(body))
        asset_url = "https://objects.githubusercontent.com/releases/HuifaVideoDownloader.exe"
        first = FakeResponse(
            body=body,
            url=asset_url,
            headers={"ETag": '"asset-v1"'},
            error_after_chunks=1,
        )
        malformed = FakeResponse(
            body=body[1024 * 1024:],
            url=asset_url,
            status_code=206,
            headers={
                "ETag": '"asset-v1"',
                "Content-Range": f"bytes 0-{len(body) - 1}/{len(body)}",
            },
        )
        fresh = FakeResponse(body=body, url=asset_url, headers={"ETag": '"asset-v1"'})
        session = FakeSession(api, [first, malformed, fresh])
        updater = self.create_updater(session)
        update = self.checked_update(updater)
        with self.assertRaises(UpdateDownloadError):
            updater.download_update(update)

        downloaded = updater.download_update(update)

        calls = [call for call in session.calls if "/releases/download/" in call[0]]
        self.assertIn("Range", calls[1][1]["headers"])
        self.assertNotIn("Range", calls[2][1]["headers"])
        self.assertTrue(malformed.closed)
        self.assertEqual(updater._downloaded_paths[downloaded.token].read_bytes(), body)

    def test_cancelled_download_keeps_partial_for_later_resume(self) -> None:
        body = large_valid_pe()
        api = FakeResponse(json_value=release_payload(body))
        response = FakeResponse(
            body=body,
            url="https://objects.githubusercontent.com/releases/HuifaVideoDownloader.exe",
            headers={"ETag": '"asset-v1"'},
        )
        updater = self.create_updater(FakeSession(api, response))
        update = self.checked_update(updater)
        checks = 0

        def cancel_after_first_chunk() -> bool:
            nonlocal checks
            checks += 1
            return checks >= 4

        with self.assertRaisesRegex(UpdateDownloadCancelled, "已保留进度"):
            updater.download_update(update, cancel_callback=cancel_after_first_chunk)

        partial = next(updater.state_dir.glob("*.part"))
        self.assertEqual(partial.stat().st_size, 1024 * 1024)
        self.assertTrue(partial.with_name(partial.name + ".json").is_file())
        self.assertTrue(response.closed)

    def test_mismatched_resume_manifest_is_removed_before_fresh_request(self) -> None:
        api = FakeResponse(json_value=release_payload(self.body))
        download = FakeResponse(
            body=self.body,
            url="https://objects.githubusercontent.com/releases/HuifaVideoDownloader.exe",
        )
        session = FakeSession(api, download)
        updater = self.create_updater(session)
        update = self.checked_update(updater)
        updater.state_dir.mkdir(parents=True, exist_ok=True)
        partial = updater.state_dir / "HuifaVideoDownloader-0.2.0.exe.part"
        partial.write_bytes(b"unrelated")
        partial.with_name(partial.name + ".json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "url": "https://github.com/attacker/release.exe",
                    "version": "0.2.0",
                    "size_bytes": len(self.body),
                    "sha256": hashlib.sha256(self.body).hexdigest(),
                }
            ),
            encoding="utf-8",
        )

        downloaded = updater.download_update(update)

        call = [item for item in session.calls if "/releases/download/" in item[0]][0]
        self.assertNotIn("Range", call[1]["headers"])
        self.assertEqual(updater._downloaded_paths[downloaded.token].read_bytes(), self.body)

    def test_install_stages_beside_exe_and_launches_exit_waiting_helper(self) -> None:
        api = FakeResponse(json_value=release_payload(self.body))
        download = FakeResponse(
            body=self.body,
            url="https://objects.githubusercontent.com/releases/HuifaVideoDownloader.exe",
        )
        updater = self.create_updater(FakeSession(api, download))
        downloaded = updater.download_update(self.checked_update(updater))

        with patch.object(updater, "_powershell_executable", return_value=r"C:\Windows\powershell.exe"), patch(
            "app.core.application_updater.subprocess.Popen"
        ) as popen:
            updater.schedule_install_on_exit(downloaded, confirmed=True, restart=True)

        staged = self.executable.with_name(self.executable.name + ".update")
        self.assertTrue(staged.is_file())
        command = popen.call_args.args[0]
        self.assertIn("-ParentPid", command)
        self.assertIn("4321", command)
        self.assertIn("-ReceiptPath", command)
        self.assertIn("-FromVersion", command)
        self.assertIn("-ToVersion", command)
        helper = Path(command[command.index("-File") + 1])
        helper_text = helper.read_text(encoding="utf-8-sig")
        self.assertIn("Wait-Process", helper_text)
        self.assertIn("[System.IO.File]::Replace", helper_text)
        self.assertIn("System.Security.Cryptography.SHA256", helper_text)
        self.assertIn("Write-InstallReceipt", helper_text)

    def test_install_preparation_failure_removes_staged_executable(self) -> None:
        api = FakeResponse(json_value=release_payload(self.body))
        download = FakeResponse(
            body=self.body,
            url="https://objects.githubusercontent.com/releases/HuifaVideoDownloader.exe",
        )
        updater = self.create_updater(FakeSession(api, download))
        downloaded = updater.download_update(self.checked_update(updater))

        with patch.object(
            updater,
            "_powershell_executable",
            side_effect=UpdateInstallError("PowerShell unavailable"),
        ):
            with self.assertRaisesRegex(UpdateInstallError, "无法准备"):
                updater.schedule_install_on_exit(
                    downloaded,
                    confirmed=True,
                    restart=True,
                )

        staged = self.executable.with_name(self.executable.name + ".update")
        self.assertFalse(staged.exists())
        self.assertEqual(list(updater.state_dir.glob("portable-update-*.ps1")), [])

    def test_runtime_probe_only_enables_frozen_windows_executable(self) -> None:
        with patch("app.core.application_updater.os.name", "nt"), patch.object(
            __import__("sys"), "frozen", True, create=True
        ):
            self.assertTrue(portable_single_exe_supported(self.executable))
            self.assertFalse(portable_single_exe_supported(self.root / "app.py"))

    @unittest.skipUnless(os.name == "nt", "portable replacement helper is Windows-only")
    def test_powershell_helper_replaces_verifies_and_cleans_up_without_restart(self) -> None:
        updater = self.create_updater(FakeSession(FakeResponse(json_value=[])))
        staged = self.root / "HuifaVideoDownloader.exe.update"
        downloaded = updater.state_dir / "HuifaVideoDownloader-0.2.0.exe"
        pending = updater._pending_path
        intent = updater._install_intent_path
        receipt = updater._install_receipt_path
        log_path = updater.state_dir / "portable-update.log"
        helper = updater.state_dir / "portable-update-test.ps1"
        updater.state_dir.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(self.body)
        downloaded.write_bytes(self.body)
        pending.write_text("{}", encoding="utf-8")
        intent.write_text("{}", encoding="utf-8")
        helper.write_text(updater._helper_script(), encoding="utf-8-sig")
        digest = hashlib.sha256(self.body).hexdigest()

        result = subprocess.run(
            [
                updater._powershell_executable(),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(helper),
                "-ParentPid",
                "999999",
                "-StagedPath",
                str(staged),
                "-TargetPath",
                str(self.executable),
                "-ExpectedSha256",
                digest,
                "-DownloadedPath",
                str(downloaded),
                "-PendingManifest",
                str(pending),
                "-IntentManifest",
                str(intent),
                "-ReceiptPath",
                str(receipt),
                "-FromVersion",
                "0.1.0",
                "-ToVersion",
                "0.2.0",
                "-LogPath",
                str(log_path),
                "-Restart",
                "0",
                "-RestartArgsBase64",
                "W10=",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        helper_log = log_path.read_text(encoding="utf-8-sig") if log_path.exists() else "<no log>"
        self.assertEqual(
            self.executable.read_bytes(),
            self.body,
            f"stdout={result.stdout!r} stderr={result.stderr!r} log={helper_log!r}",
        )
        self.assertFalse(staged.exists())
        self.assertFalse(downloaded.exists())
        self.assertFalse(pending.exists())
        self.assertFalse(intent.exists())
        receipt_payload = json.loads(receipt.read_text(encoding="utf-8-sig"))
        self.assertEqual(receipt_payload["status"], "succeeded")
        self.assertEqual(receipt_payload["from_version"], "0.1.0")
        self.assertEqual(receipt_payload["to_version"], "0.2.0")
        self.assertEqual(receipt_payload["current_version"], "0.2.0")
        self.assertIn("更新安装成功", helper_log)


if __name__ == "__main__":
    unittest.main()
