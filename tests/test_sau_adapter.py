from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.adapters.sau_adapter import (
    SAU_PLATFORM_CAPABILITIES,
    SAU_PLATFORM_DISPLAY_NAMES,
    SAU_SUPPORTED_PLATFORMS,
    SauAdapter,
    SauCoreCompatibility,
    get_sau_platform_capability,
    probe_sau_compatibility,
)
from app.integrations.social_auto_upload import runtime


class SauAdapterCapabilityTests(unittest.TestCase):
    @staticmethod
    def payload(platform: str, settings: dict | None = None) -> tuple[SauAdapter, dict]:
        adapter = SauAdapter(platform)
        platform_settings = {"account": "work", **(settings or {})}
        if platform == "bilibili":
            platform_settings.setdefault("tid", "17")
        return adapter, adapter.build_payload(
            {"video_path": "C:/media/video.mp4", "thumbnail_path": "C:/media/default.jpg"},
            {"title": "演示标题", "description": "演示简介", "tags": ["新闻", "测试"]},
            platform_settings,
        )

    def test_official_platform_list_and_display_names_are_centralized(self) -> None:
        expected = (
            "douyin", "kuaishou", "xiaohongshu", "bilibili", "tencent",
            "baijiahao", "alipay", "weibo", "hupu", "youtube",
        )
        self.assertEqual(SAU_SUPPORTED_PLATFORMS, expected)
        self.assertEqual(set(SAU_PLATFORM_DISPLAY_NAMES), set(expected))
        self.assertNotIn("tiktok", SAU_PLATFORM_CAPABILITIES)
        self.assertIs(get_sau_platform_capability(" YouTube "), SAU_PLATFORM_CAPABILITIES["youtube"])
        self.assertIsNone(get_sau_platform_capability("tiktok"))

    def test_capability_sets_match_the_vendored_parser(self) -> None:
        schedules = {name for name, item in SAU_PLATFORM_CAPABILITIES.items() if item.supports_schedule}
        collections = {name for name, item in SAU_PLATFORM_CAPABILITIES.items() if item.supports_collection}
        dual_covers = {name for name, item in SAU_PLATFORM_CAPABILITIES.items() if item.supports_dual_thumbnail}
        visibility = {name for name, item in SAU_PLATFORM_CAPABILITIES.items() if item.supports_visibility}
        playlists = {name for name, item in SAU_PLATFORM_CAPABILITIES.items() if item.supports_playlist}
        required_tid = {name for name, item in SAU_PLATFORM_CAPABILITIES.items() if item.requires_tid}
        self.assertEqual(schedules, {"douyin", "kuaishou", "xiaohongshu", "bilibili", "tencent"})
        self.assertEqual(collections, {"douyin", "kuaishou", "tencent", "baijiahao", "alipay", "weibo"})
        self.assertEqual(dual_covers, {"douyin", "tencent"})
        self.assertEqual(visibility, {"youtube"})
        self.assertEqual(playlists, {"youtube"})
        self.assertEqual(required_tid, {"bilibili"})
        self.assertTrue(all(item.actions == ("login", "check", "upload-video") for item in SAU_PLATFORM_CAPABILITIES.values()))

    def test_all_platforms_build_structured_upload_requests(self) -> None:
        for platform in SAU_SUPPORTED_PLATFORMS:
            with self.subTest(platform=platform):
                adapter, payload = self.payload(platform)
                request = adapter.build_upload_request(payload)
                self.assertEqual(request["account_name"], "work")
                self.assertEqual(request["video_file"], "C:/media/video.mp4")
                self.assertEqual(request["title"], "演示标题")
                self.assertEqual(request["tags"], ["新闻", "测试"])

    def test_all_structured_requests_are_accepted_by_the_real_vendored_dispatcher(self) -> None:
        module = runtime._load_upstream()
        upload_functions = {
            "douyin": "upload_video",
            "kuaishou": "upload_kuaishou_video",
            "xiaohongshu": "upload_xiaohongshu_video",
            "bilibili": "upload_bilibili_video",
            "tencent": "upload_tencent_video",
            "baijiahao": "upload_baijiahao_video",
            "alipay": "upload_alipay_video",
            "weibo": "upload_weibo_video",
            "hupu": "upload_hupu_video",
            "youtube": "upload_youtube_video",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "video.mp4"
            cover = root / "cover.jpg"
            video.write_bytes(b"media")
            cover.write_bytes(b"image")
            for platform in SAU_SUPPORTED_PLATFORMS:
                settings = {"account": "work"}
                if platform == "bilibili":
                    settings["tid"] = "17"
                adapter = SauAdapter(platform)
                payload = adapter.build_payload(
                    {"video_path": str(video), "thumbnail_path": str(cover)},
                    {"title": "title", "description": "description", "tags": ["tag"]},
                    settings,
                )
                request = adapter.build_upload_request(payload)
                upload = AsyncMock(return_value=root / "account.json")
                with self.subTest(platform=platform), patch.object(
                    module, upload_functions[platform], upload
                ):
                    result = asyncio.run(module.publish_video_payload(platform, request))
                    self.assertEqual(result, f"{platform} 发布流程已完成")
                    uploaded_request = upload.await_args.args[0]
                    self.assertEqual(uploaded_request.account_name, "work")
                    self.assertEqual(uploaded_request.video_file, video)

    def test_account_actions_call_runtime_directly_for_every_platform(self) -> None:
        compatibility = SauCoreCompatibility(True, "embedded", "", "")
        for platform in SAU_SUPPORTED_PLATFORMS:
            adapter = SauAdapter(platform)
            with self.subTest(platform=platform, action="login"), patch.object(
                adapter, "core_compatibility", return_value=compatibility
            ), patch(
                "app.adapters.sau_adapter.account_login",
                return_value={"success": True, "message": "logged in"},
            ) as login, patch("app.adapters.sau_adapter.account_check") as check:
                ok, message = adapter.account_action("login", "work")
                self.assertTrue(ok)
                self.assertEqual(message, "logged in")
                login.assert_called_once_with(platform, "work", headed=True, cancel_event=None)
                check.assert_not_called()

            with self.subTest(platform=platform, action="check"), patch.object(
                adapter, "core_compatibility", return_value=compatibility
            ), patch("app.adapters.sau_adapter.account_login") as login, patch(
                "app.adapters.sau_adapter.account_check", return_value=True
            ) as check:
                ok, message = adapter.account_action("check", "work")
                self.assertTrue(ok)
                self.assertIn("Cookie", message)
                check.assert_called_once_with(platform, "work", cancel_event=None)
                login.assert_not_called()

    def test_action_probe_uses_core_status(self) -> None:
        with patch("app.adapters.sau_adapter.core_status", return_value=(True, "ready")):
            action = probe_sau_compatibility("douyin", "upload-video")
        self.assertTrue(action.compatible)

        with patch("app.adapters.sau_adapter.core_status", return_value=(False, "missing playwright")):
            report = probe_sau_compatibility("douyin", "login")
        self.assertFalse(report.compatible)
        self.assertIn("missing playwright", report.user_message())

    def test_unsupported_platform_and_action_are_blocked_before_runtime(self) -> None:
        with patch("app.adapters.sau_adapter.core_status", return_value=(True, "ready")):
            unsupported_platform = probe_sau_compatibility("tiktok", "login")
            unsupported_action = probe_sau_compatibility("douyin", "delete")
        self.assertFalse(unsupported_platform.compatible)
        self.assertFalse(unsupported_action.compatible)

    def test_cancel_event_and_sensitive_output_are_propagated_safely(self) -> None:
        adapter = SauAdapter("douyin")
        compatibility = SauCoreCompatibility(True, "embedded", "douyin", "login")
        cancel = threading.Event()
        with patch.object(adapter, "core_compatibility", return_value=compatibility), patch(
            "app.adapters.sau_adapter.account_login", side_effect=InterruptedError,
        ) as login:
            ok, message = adapter.account_action("login", "work", cancel_event=cancel)
        self.assertFalse(ok)
        self.assertIn("取消", message)
        login.assert_called_once_with("douyin", "work", headed=True, cancel_event=cancel)

        with patch.object(adapter, "core_compatibility", return_value=compatibility), patch(
            "app.adapters.sau_adapter.account_login",
            return_value={"success": False, "message": "Cookie: super-secret"},
        ):
            ok, message = adapter.account_action("login", "work")
        self.assertFalse(ok)
        self.assertNotIn("super-secret", message)
        self.assertIn("<redacted>", message)

    def test_publish_calls_runtime_without_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            video = Path(directory) / "video.mp4"
            video.write_bytes(b"media")
            adapter = SauAdapter("douyin")
            payload = adapter.build_payload(
                {"video_path": str(video)},
                {"title": "title", "description": "description", "tags": []},
                {"account": "work"},
            )
            compatibility = SauCoreCompatibility(True, "embedded", "douyin", "upload-video")
            with patch.object(adapter, "core_compatibility", return_value=compatibility), patch(
                "app.adapters.sau_adapter.publish_video", return_value="douyin published"
            ) as publish:
                ok, message = adapter.publish(payload)
        self.assertTrue(ok)
        self.assertEqual(message, "douyin published")
        publish.assert_called_once()
        self.assertEqual(publish.call_args.args[0], "douyin")
        self.assertEqual(publish.call_args.args[1]["account_name"], "work")
        self.assertEqual(publish.call_args.kwargs, {"cancel_event": None})

    def test_dual_cover_schedule_visibility_playlist_and_tid_arguments(self) -> None:
        adapter, payload = self.payload("douyin", {
            "thumbnail": "C:/cover/generic.jpg",
            "thumbnail_landscape": "C:/cover/landscape.jpg",
            "thumbnail_portrait": "C:/cover/portrait.jpg",
            "schedule": "2026-08-24 09:30",
            "collection": "series",
        })
        request = adapter.build_upload_request(payload)
        self.assertEqual(request["thumbnail_file"], "C:/cover/generic.jpg")
        self.assertEqual(request["thumbnail_landscape_file"], "C:/cover/landscape.jpg")
        self.assertEqual(request["thumbnail_portrait_file"], "C:/cover/portrait.jpg")
        self.assertEqual(request["schedule"], "2026-08-24 09:30")
        self.assertEqual(request["collection_name"], "series")

        adapter, payload = self.payload("youtube", {"collection": "Playlist A", "visibility": "unlisted"})
        request = adapter.build_upload_request(payload)
        self.assertEqual(request["playlist"], "Playlist A")
        self.assertEqual(request["visibility"], "unlisted")

        adapter, payload = self.payload("bilibili", {"tid": "171"})
        request = adapter.build_upload_request(payload)
        self.assertEqual(request["tid"], 171)

    def test_invalid_schedule_tid_and_visibility_return_clear_errors(self) -> None:
        for value in ("2026/08/24 09:30", "2026-02-30 09:30", "2026-08-24 25:00"):
            adapter, payload = self.payload("douyin", {"schedule": value})
            with self.subTest(schedule=value), self.assertRaisesRegex(ValueError, "定时发布时间"):
                adapter.build_upload_request(payload)

        _, missing_tid = self.payload("bilibili", {"tid": ""})
        with self.assertRaisesRegex(ValueError, "tid"):
            SauAdapter("bilibili").build_upload_request(missing_tid)

        adapter, payload = self.payload("youtube", {"visibility": "friends-only"})
        with self.assertRaisesRegex(ValueError, "public"):
            adapter.build_upload_request(payload)


class SocialAutoUploadRuntimeTests(unittest.TestCase):
    def test_runtime_routes_login_and_check_to_every_upstream_function(self) -> None:
        login_functions: dict[str, AsyncMock] = {}
        check_functions: dict[str, AsyncMock] = {}
        module = SimpleNamespace()
        for platform, function_name in runtime._LOGIN_FUNCTIONS.items():
            login = AsyncMock(return_value={"success": True, "message": platform})
            check = AsyncMock(return_value=True)
            setattr(module, function_name, login)
            setattr(module, runtime._CHECK_FUNCTIONS[platform], check)
            login_functions[platform] = login
            check_functions[platform] = check

        with patch.object(runtime, "_load_upstream", return_value=module):
            for platform in runtime._LOGIN_FUNCTIONS:
                with self.subTest(platform=platform, action="login"):
                    result = runtime.account_login(platform, "work", headed=True)
                    self.assertTrue(result["success"])
                with self.subTest(platform=platform, action="check"):
                    self.assertTrue(runtime.account_check(platform, "work"))

        for platform, login in login_functions.items():
            login.assert_awaited_once_with("work", headless=False)
            check_functions[platform].assert_awaited_once_with("work")

    def test_runtime_publish_dispatches_structured_payload_in_process(self) -> None:
        dispatch = AsyncMock(return_value="douyin 发布流程已完成")
        module = SimpleNamespace(publish_video_payload=dispatch)
        payload = {"video_file": "video.mp4", "account_name": "work"}
        with patch.object(runtime, "_load_upstream", return_value=module):
            result = runtime.publish_video("douyin", payload)

        self.assertEqual(result, "douyin 发布流程已完成")
        dispatch.assert_awaited_once_with("douyin", payload)

    def test_runtime_rejects_unsupported_platforms_and_failed_dispatch(self) -> None:
        with patch.object(runtime, "_load_upstream", return_value=SimpleNamespace()):
            with self.assertRaises(runtime.SocialAutoUploadError):
                runtime.account_login("unsupported", "work")

        module = SimpleNamespace(
            publish_video_payload=AsyncMock(side_effect=RuntimeError("upload failed")),
        )
        with patch.object(runtime, "_load_upstream", return_value=module):
            with self.assertRaisesRegex(runtime.SocialAutoUploadError, "upload failed"):
                runtime.publish_video("douyin", {})

    def test_real_vendored_core_imports_with_official_playwright(self) -> None:
        runtime._load_upstream.cache_clear()
        module = runtime._load_upstream()
        self.assertTrue(callable(module.publish_video_payload))
        self.assertTrue(callable(module.build_parser))
        self.assertTrue(callable(module.dispatch))
        self.assertTrue(all(hasattr(module, name) for name in runtime._LOGIN_FUNCTIONS.values()))
        self.assertTrue(all(hasattr(module, name) for name in runtime._CHECK_FUNCTIONS.values()))


if __name__ == "__main__":
    unittest.main()
