from __future__ import annotations

import unittest

from app.core.download_links import extract_download_links, normalize_download_link
from app.core.download_service import DownloadService
from app.core.download_performance import (
    effective_download_performance,
    normalize_download_performance_mode,
    smart_download_performance,
)
from app.core.platforms import detect_platform


class _Settings:
    def __init__(self, **values: object) -> None:
        self.values = values

    def get(self, key: str) -> object:
        return self.values.get(key)


class DownloadLinkPolicyTests(unittest.TestCase):
    def test_normalizes_common_clipboard_wrappers_without_changing_query(self) -> None:
        self.assertEqual(
            normalize_download_link("[视频](https://example.com/watch?v=1&list=2)"),
            "https://example.com/watch?v=1&list=2",
        )
        self.assertEqual(
            normalize_download_link("<http://example.com/video>。"),
            "http://example.com/video",
        )

    def test_malformed_or_non_web_links_are_ignored(self) -> None:
        for value in ("", "not a link", "file:///D:/video.mp4", "http://[broken"):
            with self.subTest(value=value):
                self.assertEqual(normalize_download_link(value), "")

    def test_extracts_links_in_order_and_removes_duplicates(self) -> None:
        self.assertEqual(
            extract_download_links(
                "first https://example.com/a, duplicate https://example.com/a "
                "then http://example.com/b。"
            ),
            ["https://example.com/a", "http://example.com/b"],
        )


class DownloadPerformancePolicyTests(unittest.TestCase):
    def test_smart_profiles_remain_conservative(self) -> None:
        self.assertEqual(smart_download_performance(1), (1, 6, 0.5))
        self.assertEqual(smart_download_performance(4), (2, 8, 0.0))
        self.assertEqual(smart_download_performance(8), (3, 8, 0.0))
        self.assertEqual(smart_download_performance(64), (4, 8, 0.0))

    def test_manual_values_are_bounded_and_non_finite_delay_is_rejected(self) -> None:
        settings = _Settings(
            download_performance_mode="manual",
            max_concurrent="999",
            fragment_concurrent="0",
            request_delay="nan",
        )
        self.assertEqual(effective_download_performance(settings), (8, 1, 0.0))

    def test_unknown_mode_falls_back_to_smart(self) -> None:
        settings = _Settings(download_performance_mode="unexpected")
        self.assertEqual(normalize_download_performance_mode("unexpected"), "smart")
        self.assertEqual(
            effective_download_performance(settings, logical_processors=4),
            (2, 8, 0.0),
        )

    def test_runtime_performance_configuration_is_bounded_and_resumes_queue(self) -> None:
        starts: list[bool] = []
        service = type("RuntimeService", (), {})()
        service._start_next = lambda: starts.append(True)

        applied = DownloadService.configure_performance(
            service,
            max_concurrent="999",
            fragment_concurrent="0",
            request_delay="inf",
        )

        self.assertEqual(applied, (8, 1, 0.0))
        self.assertEqual(service.max_concurrent, 8)
        self.assertEqual(service.fragment_concurrent, 1)
        self.assertEqual(service.request_delay, 0.0)
        self.assertEqual(starts, [True])


class PlatformDetectionTests(unittest.TestCase):
    def test_detects_supported_media_hosts_and_subdomains(self) -> None:
        cases = {
            "https://music.youtube.com/watch?v=1": "youtube",
            "https://v.douyin.com/demo/": "douyin",
            "https://www.bilibili.com/video/BV1": "bilibili",
            "https://v.qq.com/x/cover/demo.html": "tencent",
            "https://baijiahao.baidu.com/s?id=1": "baijiahao",
            "https://www.tiktok.com/@demo/video/1": "tiktok",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(detect_platform(url), expected)

    def test_unrelated_parent_domains_are_not_mislabeled(self) -> None:
        self.assertEqual(detect_platform("https://mail.qq.com/"), "generic")
        self.assertEqual(detect_platform("https://www.baidu.com/s?wd=test"), "generic")

    def test_malformed_url_is_safe(self) -> None:
        self.assertEqual(detect_platform("http://[broken"), "generic")


if __name__ == "__main__":
    unittest.main()
