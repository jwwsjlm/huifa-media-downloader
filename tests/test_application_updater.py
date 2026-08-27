from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from app.core.application_updater import (
    AutoUpdateCheckThrottle,
    UpdateConfirmationRequired,
    UpdateDownloadCancelled,
    UpdateDownloadError,
    UpdateInstallError,
    VelopackApplicationUpdater,
    VelopackUpdaterConfig,
    normalize_github_repository,
    run_velopack_startup,
    velopack_persistent_data_dir,
)
from app.core.version import APP_VERSION
from app.core.update_receipt import (
    consume_update_install_receipt,
    write_update_install_intent,
)


class FakeAsset:
    PackageId = "Huifa.VideoDownloader"
    Version = "0.2.0"
    Type = "Full"
    FileName = "Huifa.VideoDownloader-0.2.0-full.nupkg"
    SHA1 = ""
    SHA256 = "A" * 64
    Size = 42 * 1024 * 1024
    NotesMarkdown = "# 0.2.0\n\n- Faster startup"
    NotesHtml = "<h1>0.2.0</h1>"


class FakeInfo:
    TargetFullRelease = FakeAsset()
    DeltasToTarget = []
    IsDowngrade = False
    BaseRelease = None


class FakeManager:
    def __init__(self, source, options):
        self.source = source
        self.options = options
        self.available = FakeInfo()
        self.pending = None
        self.download_error = None
        self.applied = []
        self.scheduled = []

    def get_current_version(self):
        return "0.1.0"

    def get_app_id(self):
        return "Huifa.VideoDownloader"

    def get_is_portable(self):
        return True

    def get_update_pending_restart(self):
        return self.pending

    def check_for_updates(self):
        return self.available

    def download_updates(self, update, progress_callback=None):
        if self.download_error:
            raise self.download_error
        if progress_callback:
            progress_callback(25)
            progress_callback(80)

    def apply_updates_and_restart(self, update):
        self.applied.append((update, None))

    def apply_updates_and_restart_with_args(self, update, args):
        self.applied.append((update, args))

    def wait_exit_then_apply_updates(self, update, silent=False, restart=True, restart_args=None):
        self.scheduled.append((update, silent, restart, restart_args))


class FakeVelopackModule:
    def __init__(self):
        self.source_args = None
        self.option_args = None
        self.manager = None
        self.auto_apply = None
        self.startup_ran = False
        self.restart_callback = None

    def GithubSource(self, repo_url, token=None, prerelease=False):
        self.source_args = (repo_url, token, prerelease)
        return self.source_args

    def UpdateOptions(self, allow_downgrade, maximum_deltas, channel=None):
        self.option_args = (allow_downgrade, maximum_deltas, channel)
        return self.option_args

    def UpdateManager(self, source, options):
        self.manager = FakeManager(source, options)
        return self.manager

    def App(self):
        module = self

        class FakeApp:
            def set_auto_apply_on_startup(self, enabled):
                module.auto_apply = enabled
                return self

            def on_restarted(self, callback):
                module.restart_callback = callback
                return self

            def run(self):
                module.startup_ran = True

        return FakeApp()


class ApplicationUpdaterTests(unittest.TestCase):
    def create_updater(self, **kwargs):
        module = FakeVelopackModule()
        config = VelopackUpdaterConfig(repository="huifa/yt-release", **kwargs)
        return VelopackApplicationUpdater(config, module), module

    def test_repository_normalization_accepts_shorthand_and_https(self) -> None:
        expected = "https://github.com/huifa/yt-release"
        self.assertEqual(normalize_github_repository("huifa/yt-release"), expected)
        self.assertEqual(normalize_github_repository(expected + ".git/"), expected)
        for invalid in (
            "",
            "huifa",
            "http://github.com/huifa/yt-release",
            "https://example.com/huifa/yt-release",
            "https://token@github.com/huifa/yt-release",
            "https://github.com/huifa/yt-release/issues",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_github_repository(invalid)

    def test_check_maps_github_release_to_ui_safe_model(self) -> None:
        updater, module = self.create_updater(prerelease=True, channel="beta", maximum_deltas=4)
        update = updater.check_for_updates()
        self.assertIsNotNone(update)
        self.assertEqual(module.source_args, ("https://github.com/huifa/yt-release", None, True))
        self.assertEqual(module.option_args, (False, 4, "beta"))
        self.assertEqual(update.current_version, "0.1.0")
        self.assertEqual(update.version, "0.2.0")
        self.assertEqual(update.package_id, "Huifa.VideoDownloader")
        self.assertEqual(update.sha256, "a" * 64)
        self.assertIn("Faster startup", update.release_notes_markdown)
        self.assertTrue(update.is_portable)
        self.assertFalse(update.downloaded)

    def test_constructing_updater_does_not_require_managed_install(self) -> None:
        module = FakeVelopackModule()
        updater = VelopackApplicationUpdater(
            VelopackUpdaterConfig(repository="huifa/yt-release"),
            module,
        )
        self.assertIsNone(module.manager)
        self.assertEqual(updater.config.repository_url(), "https://github.com/huifa/yt-release")

    def test_download_reports_progress_and_explicit_confirmation_applies(self) -> None:
        updater, module = self.create_updater()
        update = updater.check_for_updates()
        progress = []
        downloaded = updater.download_update(update, progress.append)
        self.assertEqual(progress, [0, 25, 80, 100])
        self.assertTrue(downloaded.downloaded)
        with self.assertRaises(UpdateConfirmationRequired):
            updater.install_and_restart(downloaded, confirmed=False)
        updater.install_and_restart(downloaded, confirmed=True, restart_args=["--updated"])
        self.assertEqual(module.manager.applied[0][1], ["--updated"])

    def test_velopack_download_can_be_cancelled_between_progress_updates(self) -> None:
        updater, _module = self.create_updater()
        update = updater.check_for_updates()
        progress: list[int] = []
        cancellation_checks = iter((False, False, True))

        with self.assertRaises(UpdateDownloadCancelled):
            updater.download_update(
                update,
                progress.append,
                lambda: next(cancellation_checks),
            )

        self.assertEqual(progress, [0, 25])
        with self.assertRaises(UpdateInstallError):
            updater.install_and_restart(update, confirmed=True)

    def test_install_rejects_update_that_was_not_downloaded(self) -> None:
        updater, _module = self.create_updater()
        update = updater.check_for_updates()
        with self.assertRaises(UpdateInstallError):
            updater.install_and_restart(update, confirmed=True)

    def test_download_error_redacts_private_token(self) -> None:
        token = "github-secret-token"
        updater, module = self.create_updater(access_token=token)
        self.assertNotIn(token, repr(updater.config))
        update = updater.check_for_updates()
        module.manager.download_error = RuntimeError(f"request failed Authorization: Bearer {token}")
        with self.assertRaises(UpdateDownloadError) as context:
            updater.download_update(update)
        self.assertNotIn(token, str(context.exception))
        self.assertIn("***", str(context.exception))

    def test_pending_restart_can_be_scheduled_after_confirmation(self) -> None:
        updater, module = self.create_updater()
        updater.runtime()
        module.manager.pending = FakeAsset()
        update = updater.pending_restart()
        self.assertTrue(update.downloaded)
        updater.schedule_install_on_exit(
            update,
            confirmed=True,
            restart=True,
            restart_args=["--after-update"],
        )
        self.assertEqual(
            module.manager.scheduled[0][1:],
            (False, True, ["--after-update"]),
        )

    def test_startup_hook_never_auto_applies_without_ui_confirmation(self) -> None:
        module = FakeVelopackModule()
        run_velopack_startup(module)
        self.assertFalse(module.auto_apply)
        self.assertTrue(module.startup_ran)
        self.assertTrue(callable(module.restart_callback))

    def test_velopack_restart_callback_records_a_one_shot_success_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory) / "application"
            write_update_install_intent(
                state_dir,
                SimpleNamespace(
                    current_version="0.1.0",
                    version=APP_VERSION,
                    delivery_kind="velopack",
                ),
            )
            module = FakeVelopackModule()

            run_velopack_startup(module, state_dir)
            module.restart_callback(APP_VERSION)

            receipt = consume_update_install_receipt(state_dir)
            self.assertIsNotNone(receipt)
            self.assertTrue(receipt.succeeded)
            self.assertEqual(receipt.from_version, "0.1.0")
            self.assertEqual(receipt.to_version, APP_VERSION)
            self.assertEqual(receipt.delivery_kind, "velopack")
            self.assertIsNone(consume_update_install_receipt(state_dir))

    def test_auto_check_throttle_is_repository_scoped_and_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "update-check.json"
            throttle = AutoUpdateCheckThrottle(path, timedelta(hours=24))
            now = datetime(2026, 8, 23, 9, 0, tzinfo=timezone.utc)
            self.assertTrue(throttle.is_due("huifa/yt-release", now))
            throttle.mark_checked("huifa/yt-release", now)
            self.assertFalse(throttle.is_due("huifa/yt-release", now + timedelta(hours=23)))
            self.assertTrue(throttle.is_due("huifa/yt-release", now + timedelta(hours=24)))
            self.assertTrue(throttle.is_due("huifa/yt-release", now - timedelta(minutes=1)))
            self.assertTrue(throttle.is_due("other/project", now + timedelta(hours=1)))
            self.assertFalse(path.with_name("update-check.json.tmp").exists())
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["repository"], "https://github.com/huifa/yt-release")

    def test_managed_data_directory_lives_outside_replaceable_current(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / "current"
            current.mkdir()
            (root / "Update.exe").write_bytes(b"")
            (current / "sq.version").write_text("{}", encoding="utf-8")
            self.assertEqual(
                velopack_persistent_data_dir(current),
                (root / "data").resolve(),
            )
            self.assertIsNone(velopack_persistent_data_dir(root))

    def test_build_foundation_is_onedir_pinned_and_never_uploads(self) -> None:
        root = Path(__file__).resolve().parents[1]
        spec = (root / "build" / "HuifaVideoDownloader.velopack.spec").read_text(encoding="utf-8")
        script = (root / "scripts" / "build_velopack_release.ps1").read_text(encoding="utf-8")
        manifest = json.loads((root / ".config" / "dotnet-tools.json").read_text(encoding="utf-8"))
        requirements = (root / "requirements-velopack.txt").read_text(encoding="utf-8")
        runtime_hook = (root / "build" / "01_velopack_hook.py").read_text(encoding="utf-8")
        self.assertIn("exclude_binaries=True", spec)
        self.assertIn("COLLECT(", spec)
        self.assertIn("01_velopack_hook.py", spec)
        self.assertIn("run_velopack_startup()", runtime_hook)
        self.assertIn("'pack'", script)
        self.assertIn("Huifa.VideoDownloader*-Portable.zip", script)
        self.assertIn("Huifa.VideoDownloader*-Setup.exe", script)
        self.assertIn("Resolve-DotnetSdkExecutable", script)
        self.assertIn("$env:ProgramW6432", script)
        self.assertIn("& $DotnetExe tool restore", script)
        self.assertIn("& $DotnetExe @PackArgs", script)
        self.assertNotIn("& dotnet ", script)
        self.assertIn("ValidateEnvironmentOnly", script)
        self.assertIn("Release feed must contain exactly one full package", script)
        self.assertIn("Prerelease version", script)
        self.assertNotIn("'upload', 'github'", script)
        self.assertEqual(manifest["tools"]["vpk"]["version"], "1.2.0")
        self.assertIn("velopack==1.2.0", requirements)

    @unittest.skipUnless(os.name == "nt", "Velopack build script is Windows-only")
    def test_build_environment_probe_prefers_x64_sdk_over_x86_path_host(self) -> None:
        root = Path(__file__).resolve().parents[1]
        x64_dotnet = Path(os.environ.get("ProgramW6432", r"C:\Program Files")) / "dotnet" / "dotnet.exe"
        x86_dotnet = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "dotnet" / "dotnet.exe"
        if not x64_dotnet.is_file() or not x86_dotnet.is_file():
            self.skipTest("both x64 and x86 dotnet hosts are required for this regression test")
        sdk_probe = subprocess.run(
            [str(x64_dotnet), "--list-sdks"],
            cwd=root,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
        if sdk_probe.returncode or not sdk_probe.stdout.strip():
            self.skipTest("the x64 dotnet host has no SDK")

        environment = os.environ.copy()
        environment["PATH"] = str(x86_dotnet.parent) + os.pathsep + environment.get("PATH", "")
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(root / "scripts" / "build_velopack_release.ps1"),
                "-Version",
                APP_VERSION,
                "-ValidateEnvironmentOnly",
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn(str(x64_dotnet), output)
        self.assertNotIn(f"Using .NET SDK host: {x86_dotnet}", output)


if __name__ == "__main__":
    unittest.main()
