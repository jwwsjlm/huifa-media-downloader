from __future__ import annotations

import unittest
import tempfile
import importlib
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.core.browser_cookies import BrowserCookie
from app.integrations.social_auto_upload import runtime


class ChromiumBundleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.root = Path(__file__).resolve().parents[1]

    def test_release_builds_ship_only_the_complete_chromium_runtime(self) -> None:
        managed_spec = (
            self.root / "build" / "HuifaVideoDownloader.velopack.spec"
        ).read_text(encoding="utf-8")
        managed_build = (
            self.root / "scripts" / "build_velopack_release.ps1"
        ).read_text(encoding="utf-8")
        self.assertNotIn("tools/chromium/chrome-win64", managed_spec)
        self.assertIn("Stage-PortableRuntimeTools", managed_build)
        self.assertIn("tools\\chromium\\chrome-win64", managed_build)
        self.assertIn("chrome.exe", managed_build)

        for source in (managed_spec, managed_build):
            self.assertNotIn("chromium_headless_shell", source)
            self.assertNotIn("ffmpeg-1011", source)
            self.assertNotIn("winldd-1007", source)

    def test_browser_runtime_uses_official_playwright_only(self) -> None:
        runtime_source = Path(runtime.__file__).read_text(encoding="utf-8")
        self.assertIn("from playwright.async_api import async_playwright", runtime_source)
        self.assertNotIn("from patchright", runtime_source.casefold())

        vendor = self.root / "third_party" / "social_auto_upload"
        production_sources: list[Path] = []
        for path in vendor.rglob("*.py"):
            relative_parts = path.relative_to(vendor).parts
            if "examples" in relative_parts or "tests" in relative_parts:
                continue
            production_sources.append(path)
            source = path.read_text(encoding="utf-8").casefold()
            with self.subTest(source=str(path.relative_to(self.root))):
                self.assertNotIn("from patchright", source)
                self.assertNotIn("import patchright", source)
        self.assertTrue(production_sources)

        for spec_name in ("HuifaVideoDownloader.velopack.spec",):
            source = (self.root / "build" / spec_name).read_text(encoding="utf-8")
            with self.subTest(spec=spec_name):
                self.assertIn("collect_all('playwright')".replace("'", "\""), source.replace("'", "\""))
                self.assertNotIn("collect_all(\"patchright\")", source.replace("'", "\""))

    def test_production_uploaders_never_request_a_browser_channel(self) -> None:
        vendor = self.root / "third_party" / "social_auto_upload"
        browser_sources: list[Path] = []
        for path in vendor.rglob("*.py"):
            relative_parts = path.relative_to(vendor).parts
            if "examples" in relative_parts or "tests" in relative_parts:
                continue
            source = path.read_text(encoding="utf-8")
            if "chromium.launch" not in source:
                continue
            browser_sources.append(path)
            with self.subTest(source=str(path.relative_to(self.root))):
                self.assertNotIn('channel="chromium"', source)
                self.assertNotIn('channel="chrome"', source)
                self.assertIn("executable_path", source)
                self.assertIn("LOCAL_CHROME_PATH", source)

        self.assertTrue(browser_sources)

    def test_integration_rejects_a_missing_bundled_browser(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            runtime, "vendor_root", return_value=Path(directory) / "vendor"
        ), patch.object(
            runtime, "runtime_home", return_value=Path(directory) / "cookies"
        ), patch.object(
            runtime, "resolve_chromium_executable", return_value=None
        ):
            with self.assertRaisesRegex(runtime.SocialAutoUploadError, "缺少内置 Chromium"):
                runtime._prepare_environment()

    def test_resolver_accepts_the_flattened_packaged_browser_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "tools" / "chromium" / "chrome-win64" / "chrome.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"MZ")
            runtime.resolve_chromium_executable.cache_clear()
            try:
                with patch.object(runtime, "tool_runtime_roots", return_value=[root]), patch.dict(
                    runtime.os.environ,
                    {"HUIFA_CHROMIUM_PATH": ""},
                    clear=False,
                ):
                    self.assertEqual(runtime.resolve_chromium_executable(), executable.resolve())
            finally:
                runtime.resolve_chromium_executable.cache_clear()

    def test_resolver_prefers_the_newest_playwright_browser_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_executable = (
                root / "tools" / "chromium" / "chromium-1208" / "chrome-win64" / "chrome.exe"
            )
            new_executable = (
                root / "tools" / "chromium" / "chromium-1234" / "chrome-win64" / "chrome.exe"
            )
            old_executable.parent.mkdir(parents=True)
            new_executable.parent.mkdir(parents=True)
            old_executable.write_bytes(b"old")
            new_executable.write_bytes(b"new")
            runtime.resolve_chromium_executable.cache_clear()
            try:
                with patch.object(runtime, "tool_runtime_roots", return_value=[root]), patch.dict(
                    runtime.os.environ,
                    {"HUIFA_CHROMIUM_PATH": ""},
                    clear=False,
                ):
                    self.assertEqual(
                        runtime.resolve_chromium_executable(),
                        new_executable.resolve(),
                    )
            finally:
                runtime.resolve_chromium_executable.cache_clear()

    def test_youtube_login_opens_the_visible_browser_without_hidden_preflight(self) -> None:
        upstream = runtime._load_upstream()
        account_file = runtime.runtime_home() / "cookies" / "youtube_download.json"
        generated = {
            "success": True,
            "status": "logged_in",
            "message": "登录成功",
            "account_file": str(account_file),
        }
        direct_login = AsyncMock(return_value=generated)
        hidden_preflight = AsyncMock()
        with patch.object(
            upstream,
            "resolve_account_file",
            return_value=account_file,
        ), patch.object(
            upstream,
            "youtube_cookie_gen",
            direct_login,
        ), patch.object(
            upstream,
            "youtube_setup",
            hidden_preflight,
        ):
            result = runtime._run(upstream.login_youtube_account("download", headless=False))

        self.assertEqual(result, generated)
        direct_login.assert_awaited_once_with(str(account_file), headless=False)
        hidden_preflight.assert_not_awaited()

    def test_cookie_capture_browser_starts_on_about_blank(self) -> None:
        runtime._load_upstream()
        youtube_module = importlib.import_module("uploader.youtube_uploader.main")
        source = Path(youtube_module.__file__).read_text(encoding="utf-8")
        function_source = source.split("async def youtube_cookie_gen", 1)[1].split(
            "async def youtube_setup",
            1,
        )[0]

        self.assertIn('current_url = "about:blank"', function_source)
        self.assertIn("await context.new_page()", function_source)
        self.assertNotIn("page.goto(", function_source)
        self.assertIn("await context.storage_state", function_source)
        self.assertIn("context.cookies()", function_source)

    def test_download_cookie_browser_uses_an_app_local_persistent_profile(self) -> None:
        source = Path(runtime.__file__).read_text(encoding="utf-8")
        function_source = source.split("async def _download_cookie_browser_session", 1)[1].split(
            "def open_download_cookie_browser",
            1,
        )[0]

        self.assertIn("launch_persistent_context", function_source)
        self.assertIn("user_data_dir=str(profile_dir)", function_source)
        self.assertIn('args=["--hide-crash-restore-bubble"]', function_source)
        self.assertIn("_restore_download_browser_cookies", function_source)
        self.assertIn("_run_download_browser_loop", function_source)
        self.assertIn("_close_download_browser_context", function_source)
        self.assertIn("context.cookies()", source)
        self.assertIn("context.add_cookies", source)
        self.assertIn("vault.save(profile_id, cookies)", source)
        self.assertNotIn("snapshot_context", function_source)
        self.assertNotIn("youtube_cookie", function_source.casefold())

    def test_cookie_snapshot_signature_is_order_independent_and_tracks_security(self) -> None:
        first = BrowserCookie("sid", "value", ".example.com", secure=False)
        second = BrowserCookie("theme", "dark", ".example.com", path="/settings")

        self.assertEqual(
            runtime._browser_cookie_signature([first, second]),
            runtime._browser_cookie_signature([second, first]),
        )
        secured = BrowserCookie("sid", "value", ".example.com", secure=True)
        self.assertNotEqual(
            runtime._browser_cookie_signature([first, second]),
            runtime._browser_cookie_signature([secured, second]),
        )

    def test_cookie_snapshot_avoids_order_only_writes_but_saves_flag_changes(self) -> None:
        class FakeContext:
            def __init__(self) -> None:
                self.rows = [
                    {"name": "a", "value": "1", "domain": ".example.com", "path": "/"},
                    {"name": "b", "value": "2", "domain": ".example.com", "path": "/"},
                ]

            async def cookies(self):
                return list(self.rows)

        class FakeVault:
            def __init__(self) -> None:
                self.saved: list[list[BrowserCookie]] = []

            def save(self, _profile_id: str, cookies) -> None:
                self.saved.append(list(cookies))

        async def scenario() -> int:
            context = FakeContext()
            vault = FakeVault()
            state = runtime._DownloadCookieBrowserState(runtime.asyncio.Event())
            await runtime._save_download_browser_cookies(context, vault, "download", state)
            context.rows.reverse()
            await runtime._save_download_browser_cookies(context, vault, "download", state)
            context.rows[1] = dict(context.rows[1], secure=True)
            await runtime._save_download_browser_cookies(context, vault, "download", state)
            return len(vault.saved)

        self.assertEqual(runtime._run(scenario()), 2)

    def test_programmatic_browser_close_captures_cookies_before_shutdown(self) -> None:
        events: list[str] = []

        class FakeContext:
            async def cookies(self):
                events.append("cookies")
                return [{
                    "name": "sid",
                    "value": "fresh-login",
                    "domain": ".example.com",
                    "path": "/",
                    "secure": True,
                }]

            async def close(self) -> None:
                events.append("close")

        class FakeVault:
            def save(self, _profile_id: str, cookies) -> None:
                self.cookies = list(cookies)
                events.append("save")

        async def scenario() -> tuple[int, str]:
            state = runtime._DownloadCookieBrowserState(runtime.asyncio.Event())
            vault = FakeVault()
            await runtime._close_download_browser_context(
                FakeContext(),
                vault,
                "download",
                state,
            )
            return state.saved_count, vault.cookies[0].value

        self.assertEqual(runtime._run(scenario()), (1, "fresh-login"))
        self.assertEqual(events, ["cookies", "save", "close"])

    def test_pre_canceled_runtime_closes_unstarted_awaitable(self) -> None:
        class UnstartedAwaitable:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

            def __await__(self):
                if False:
                    yield None
                return None

        awaitable = UnstartedAwaitable()
        canceled = runtime.threading.Event()
        canceled.set()

        with self.assertRaisesRegex(InterruptedError, "任务已取消"):
            runtime._run(awaitable, canceled)

        self.assertTrue(awaitable.closed)

    def test_running_runtime_cancellation_waits_for_coroutine_cleanup(self) -> None:
        cleaned: list[bool] = []
        canceled = runtime.threading.Event()

        async def pending() -> None:
            try:
                await runtime.asyncio.Event().wait()
            finally:
                cleaned.append(True)

        timer = runtime.threading.Timer(0.02, canceled.set)
        timer.start()
        try:
            with self.assertRaisesRegex(InterruptedError, "任务已取消"):
                runtime._run(pending(), canceled)
        finally:
            timer.cancel()

        self.assertEqual(cleaned, [True])

    def test_download_browser_profile_is_inside_portable_data_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            runtime, "data_dir", return_value=Path(directory) / "data"
        ):
            profile = runtime.download_browser_profile_dir("download")

        self.assertEqual(profile.parent.parent, (Path(directory) / "data" / "browser").resolve())
        self.assertEqual(profile.parent.name, "profiles")

    def test_stealth_script_comes_from_vendor_resources_not_cookie_storage(self) -> None:
        runtime._load_upstream()
        conf = sys.modules["conf"]
        base_social_media = importlib.import_module("utils.base_social_media")
        captured: list[Path] = []

        class FakeContext:
            async def add_init_script(self, *, path) -> None:
                captured.append(Path(path).resolve())

        context = FakeContext()
        self.assertIs(runtime._run(base_social_media.set_init_script(context)), context)
        expected = runtime.vendor_root() / "utils" / "stealth.min.js"
        self.assertEqual(captured, [expected.resolve()])
        self.assertTrue(expected.is_file())
        self.assertEqual(Path(conf.SOURCE_DIR).resolve(), runtime.vendor_root())
        self.assertEqual(Path(conf.BASE_DIR).resolve(), runtime.runtime_home())
        self.assertNotEqual(Path(conf.SOURCE_DIR).resolve(), Path(conf.BASE_DIR).resolve())


if __name__ == "__main__":
    unittest.main()
