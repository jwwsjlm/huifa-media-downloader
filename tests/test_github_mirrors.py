from __future__ import annotations

import hashlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.core.github_mirrors import (
    GithubDownloadRoute,
    github_download_routes,
    normalize_mirror_base_url,
    route_download_url,
    route_metadata_probe_url,
)
from app.core.update_service import (
    AssetDownloadWorker,
    GithubRouteProbeWorker,
    UpdateService,
    UpdateWorker,
    release_assets_from_html,
)


class GithubMirrorTests(unittest.TestCase):
    def test_builtin_route_catalog_contains_requested_nodes_and_jsdelivr_origins(self) -> None:
        routes = github_download_routes()
        bases = {route.base_url for route in routes}
        for expected in (
            "https://gh.idayer.com/",
            "https://gh.monlor.com/",
            "https://ghm.078465.xyz/",
            "https://github.tbap.top/",
            "https://down.mxw.xx.kg/",
            "https://ghproxy.monkeyray.net/",
            "https://gh.jasonzeng.dev/",
            "https://cdn.akaere.online/",
            "https://git.yylx.win/",
            "https://ghfast.top/",
            "https://cdn.jsdelivr.net/",
            "https://fastly.jsdelivr.net/",
            "https://testingcf.jsdelivr.net/",
        ):
            self.assertIn(expected, bases)

    def test_mirror_url_validation_rejects_unsafe_targets(self) -> None:
        self.assertEqual(
            normalize_mirror_base_url("https://proxy.example/path"),
            "https://proxy.example/path/",
        )
        self.assertEqual(
            normalize_mirror_base_url("http://proxy.example/path"),
            "http://proxy.example/path/",
        )
        for value in (
            "ftp://proxy.example/",
            "https://user:pass@proxy.example/",
            "https://localhost/",
            "https://127.0.0.1/",
            "https://192.168.1.2/",
            "https://proxy.example/?token=secret",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_mirror_base_url(value)

    def test_proxy_prefixes_official_release_url(self) -> None:
        route = GithubDownloadRoute("mirror:test", "Test", "https://proxy.example/", True)
        official = "https://github.com/owner/repo/releases/download/v1/tool.exe"
        self.assertEqual(route_download_url(route, official), "https://proxy.example/" + official)

    def test_jsdelivr_is_a_metadata_only_standard_route(self) -> None:
        route = next(route for route in github_download_routes() if route.id == "mirror:jsdelivr")
        self.assertTrue(route.metadata_supported)
        self.assertFalse(route.release_page_supported)
        self.assertFalse(route.asset_supported)
        self.assertEqual(
            route_metadata_probe_url(route),
            "https://data.jsdelivr.com/v1/package/gh/yt-dlp/yt-dlp",
        )
        with self.assertRaisesRegex(ValueError, "不支持通用 GitHub"):
            route_download_url(route, "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest")

    def test_jsdelivr_reads_public_repository_versions_without_updates_json(self) -> None:
        route = next(route for route in github_download_routes() if route.id == "mirror:jsdelivr")

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "tags": {},
                    "versions": ["2026.08.19", "2026.07.04"],
                }

        worker = UpdateWorker(
            {"yt-dlp": "yt-dlp/yt-dlp"},
            github_route_mode="mirror:jsdelivr",
        )
        assets = [{
            "name": "yt-dlp.exe",
            "browser_download_url": "https://github.com/yt-dlp/yt-dlp/releases/download/2026.08.19/yt-dlp.exe",
            "digest": "sha256:" + "a" * 64,
        }]
        with patch("app.core.update_service.requests.get", return_value=Response()) as get, patch.object(
            worker, "_release_assets_for_tag", return_value=(assets, "2026.08.19")
        ), patch("app.core.update_service.write_component_cache"):
            payload = worker._fetch_latest_payload_from_jsdelivr(
                "yt-dlp/yt-dlp", {"User-Agent": "test"}, route
            )
        self.assertEqual(
            get.call_args.args[0],
            "https://data.jsdelivr.com/v1/package/gh/yt-dlp/yt-dlp",
        )
        self.assertEqual(payload["tag_name"], "2026.08.19")
        self.assertEqual(payload["assets"], assets)
        self.assertTrue(payload["_metadata_third_party"])

    def test_jsdelivr_selection_uses_other_routes_for_release_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = UpdateService(directory)
            service.set_download_routes("mirror:jsdelivr")
            official = "https://github.com/yt-dlp/yt-dlp/releases/download/v1/yt-dlp.exe"
            candidates = service._asset_download_candidates(
                official, "sha256:" + "a" * 64
            )
        self.assertTrue(candidates)
        self.assertFalse(any("cdn.jsdelivr.net" in item["url"] for item in candidates))
        self.assertTrue(any(item["third_party"] for item in candidates))

    def test_auto_asset_candidates_keep_official_first_then_fastest_mirror(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = UpdateService(directory)
            service.route_probe_results = {
                "direct": {"asset_ok": True, "asset_latency_ms": 900},
                "mirror:gh-proxy": {"asset_ok": True, "asset_latency_ms": 450},
                "mirror:ghfast": {
                    "asset_ok": True,
                    "asset_latency_ms": 80,
                    "asset_kind": "prefix",
                },
            }
            official = (
                "https://github.com/yt-dlp/yt-dlp/releases/download/v1/"
                "yt-dlp.exe"
            )
            candidates = service._asset_download_candidates(
                official,
                "sha256:" + "a" * 64,
            )

        self.assertEqual(candidates[0]["route_id"], "direct")
        self.assertEqual(candidates[1]["route_id"], "mirror:ghfast")

    def test_custom_route_probe_auto_detects_host_replacement_rule(self) -> None:
        route = GithubDownloadRoute(
            "custom:test", "Custom", "https://mirror.example/", True, kind="auto"
        )

        class Response:
            def __init__(self, url: str):
                self.url = url

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def raise_for_status(self):
                if "https://github.com" in self.url or "https://api.github.com" in self.url:
                    raise RuntimeError("prefix rule rejected")

        with patch(
            "app.core.update_service.requests.get",
            side_effect=lambda url, **_kwargs: Response(url),
        ):
            result = GithubRouteProbeWorker("", (route,))._probe_route(route)

        self.assertEqual(result["detected_kind"], "host")
        self.assertTrue(result["metadata_ok"])
        self.assertTrue(result["asset_ok"])

    def test_custom_route_probe_selects_metadata_and_asset_rules_independently(self) -> None:
        route = GithubDownloadRoute(
            "custom:mixed", "Mixed", "https://mirror.example/", True, kind="auto"
        )
        worker = GithubRouteProbeWorker("", (route,))

        def probe(url: str, _accept: str) -> tuple[bool, int, str]:
            is_prefix = "/https://" in url
            is_metadata = "api.github.com" in url or "/repos/" in url
            if is_metadata and is_prefix:
                return True, 20, ""
            if not is_metadata and not is_prefix:
                return True, 30, ""
            return False, 0, "unsupported rule"

        with patch.object(worker, "_probe_request", side_effect=probe):
            result = worker._probe_route(route)

        self.assertTrue(result["metadata_ok"])
        self.assertTrue(result["asset_ok"])
        self.assertEqual(result["metadata_kind"], "prefix")
        self.assertEqual(result["asset_kind"], "host")
        self.assertEqual(result["detected_kind"], "auto")
        self.assertEqual(result["status"], "可用（元数据与附件）")

    def test_detected_route_profiles_are_persistable_and_reloadable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = UpdateService(directory)
            service.route_probe_results = {
                "custom:test": {
                    "detected_kind": "host",
                    "metadata_kind": "host",
                    "asset_kind": "host",
                    "metadata_ok": True,
                    "asset_ok": True,
                    "latency_ms": 123,
                }
            }
            stored = service.serialized_route_profiles()
            restored = UpdateService(Path(directory) / "restored")
            restored.set_download_routes("auto", "", stored)
        self.assertEqual(restored.route_probe_results["custom:test"]["asset_kind"], "host")
        self.assertTrue(restored.route_probe_results["custom:test"]["metadata_ok"])

    def test_host_replacement_payload_urls_are_restored_to_official_github(self) -> None:
        route = GithubDownloadRoute(
            "custom:host", "Host", "https://mirror.example/", True, kind="host"
        )
        payload = {
            "html_url": "https://mirror.example/owner/repo/releases/tag/v1",
            "assets": [{
                "browser_download_url": (
                    "https://mirror.example/owner/repo/releases/download/v1/tool.exe"
                ),
            }],
        }

        normalized = UpdateWorker._canonicalize_routed_payload(payload, route)

        self.assertEqual(
            normalized["html_url"],
            "https://github.com/owner/repo/releases/tag/v1",
        )
        self.assertEqual(
            normalized["assets"][0]["browser_download_url"],
            "https://github.com/owner/repo/releases/download/v1/tool.exe",
        )

    def test_unprobed_custom_route_generates_both_supported_url_rules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = UpdateService(directory)
            service.set_download_routes("custom:missing", "https://mirror.example/")
            custom_id = next(
                route.id
                for route in service.available_download_routes()
                if route.base_url == "https://mirror.example/"
            )
            service.github_route_mode = custom_id
            official = "https://github.com/yt-dlp/yt-dlp/releases/download/v1/yt-dlp.exe"
            candidates = service._asset_download_candidates(
                official, "sha256:" + "a" * 64
            )
        custom_urls = [item["url"] for item in candidates if item["route_id"] == custom_id]
        self.assertIn("https://mirror.example/" + official, custom_urls)
        self.assertIn(
            "https://mirror.example/yt-dlp/yt-dlp/releases/download/v1/yt-dlp.exe",
            custom_urls,
        )

    def test_html_fallback_extracts_sha256_from_same_asset_row(self) -> None:
        digest = "a" * 64
        html = (
            '<li><a href="/denoland/deno/releases/download/v1/deno.zip">deno.zip</a>'
            f'<span>sha256:{digest}</span></li>'
        )

        class Response:
            text = html

            def raise_for_status(self):
                return None

        with patch("app.core.update_service.requests.get", return_value=Response()):
            assets = release_assets_from_html("denoland/deno", "v1", {})
        self.assertEqual(assets[0]["digest"], f"sha256:{digest}")

    def test_third_party_route_requires_official_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = UpdateService(directory)
            service.set_download_routes("mirror:gh-proxy")
            official = "https://github.com/yt-dlp/yt-dlp/releases/download/v1/yt-dlp.exe"
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                service._asset_download_candidates(official, "")

    def test_sau_source_snapshot_can_use_selected_proxy_without_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = UpdateService(directory)
            service.set_download_routes("mirror:gh-proxy")
            official = (
                "https://github.com/dreammis/social-auto-upload/archive/"
                + "a" * 40
                + ".zip"
            )
            candidates = service._asset_download_candidates(
                official,
                "",
                allow_unverified_third_party=True,
            )

        self.assertTrue(candidates)
        self.assertTrue(candidates[0]["third_party"])
        self.assertIn(official, candidates[0]["url"])

    def test_sau_source_snapshot_download_allows_proxy_then_relies_on_installer_validation(self) -> None:
        class Response:
            url = "https://mirror.example/archive.zip"

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size: int):
                self.chunk_size = chunk_size
                yield b"source-snapshot"

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "snapshot.zip"
            worker = AssetDownloadWorker(
                [{
                    "url": "https://mirror.example/archive.zip",
                    "name": "Custom proxy",
                    "third_party": True,
                }],
                target,
                allow_unverified_third_party=True,
                allow_source_archive=True,
            )
            completed: list[str] = []
            worker.finished.connect(completed.append)
            with patch("app.core.update_download.requests.get", return_value=Response()):
                worker.run()

            self.assertEqual(completed, [str(target)])
            self.assertEqual(target.read_bytes(), b"source-snapshot")

    def test_release_metadata_can_be_fetched_through_proxy_without_forwarding_token(self) -> None:
        captured = {}

        class Response:
            status_code = 200
            headers = {}

            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "tag_name": "v1.2.3",
                    "assets": [],
                    "html_url": "https://github.com/yt-dlp/yt-dlp/releases/tag/v1.2.3",
                }

        def fake_get(url, **kwargs):
            captured["url"] = url
            captured["headers"] = kwargs.get("headers") or {}
            return Response()

        worker = UpdateWorker(
            {"yt-dlp": "yt-dlp/yt-dlp"},
            github_route_mode="mirror:gh-proxy",
        )
        with patch("app.core.update_service.read_component_cache", return_value=None), patch(
            "app.core.update_service.write_component_cache"
        ), patch("app.core.update_service.requests.get", side_effect=fake_get):
            payload = worker._fetch_latest_payload(
                "yt-dlp/yt-dlp",
                {"User-Agent": "test", "Authorization": "Bearer secret"},
            )

        self.assertEqual(
            captured["url"],
            "https://gh-proxy.com/https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest",
        )
        self.assertNotIn("Authorization", captured["headers"])
        self.assertTrue(payload["_metadata_third_party"])
        self.assertIn("同步延迟", payload["_metadata_warning"])

    def test_auto_metadata_route_falls_back_when_direct_github_is_unreachable(self) -> None:
        class Response:
            status_code = 200
            headers = {}

            def raise_for_status(self):
                return None

            def json(self):
                return {"tag_name": "v2.0.0", "assets": []}

        requested = []

        def fake_get(url, **_kwargs):
            requested.append(url)
            if url.startswith("https://api.github.com/"):
                raise OSError("GitHub unreachable")
            return Response()

        worker = UpdateWorker({"yt-dlp": "yt-dlp/yt-dlp"})
        with patch("app.core.update_service.read_component_cache", return_value=None), patch(
            "app.core.update_service.write_component_cache"
        ), patch("app.core.update_service.requests.get", side_effect=fake_get):
            payload = worker._fetch_latest_payload("yt-dlp/yt-dlp", {"User-Agent": "test"})

        self.assertTrue(requested[0].startswith("https://api.github.com/"))
        self.assertTrue(requested[1].startswith("https://gh-proxy.com/https://api.github.com/"))
        self.assertEqual(payload["tag_name"], "v2.0.0")
        self.assertTrue(payload["_metadata_third_party"])

    def test_auto_metadata_check_keeps_official_first_and_only_uses_fastest_fallback(self) -> None:
        worker = UpdateWorker(
            {"yt-dlp": "yt-dlp/yt-dlp"},
            route_probe_results={
                "direct": {"metadata_ok": True, "metadata_latency_ms": 900},
                "mirror:gh-proxy": {"metadata_ok": True, "metadata_latency_ms": 450},
                "mirror:ghfast": {
                    "metadata_ok": True,
                    "metadata_latency_ms": 80,
                    "metadata_kind": "prefix",
                },
                "mirror:idayer": {"metadata_ok": True, "metadata_latency_ms": 120},
            },
        )

        routes = worker._fast_metadata_routes()

        self.assertEqual([route.id for route in routes], ["direct", "mirror:ghfast"])

    def test_official_metadata_success_returns_without_waiting_for_mirrors(self) -> None:
        class Response:
            status_code = 200
            headers = {}

            def raise_for_status(self):
                return None

            def json(self):
                return {"tag_name": "v3.0.0", "assets": []}

        requested = []

        def fake_get(url, **_kwargs):
            requested.append(url)
            return Response()

        worker = UpdateWorker(
            {"yt-dlp": "yt-dlp/yt-dlp"},
            route_probe_results={
                "mirror:ghfast": {
                    "metadata_ok": True,
                    "metadata_latency_ms": 1,
                    "metadata_kind": "prefix",
                },
            },
        )
        with patch("app.core.update_service.read_component_cache", return_value=None), patch(
            "app.core.update_service.write_component_cache"
        ), patch("app.core.update_service.requests.get", side_effect=fake_get):
            payload = worker._fetch_latest_payload("yt-dlp/yt-dlp", {"User-Agent": "test"})

        self.assertEqual(payload["tag_name"], "v3.0.0")
        self.assertEqual(requested, [
            "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"
        ])

    def test_forbidden_metadata_uses_public_release_page_without_rate_limit_header(self) -> None:
        class Response:
            status_code = 403
            headers = {}

        worker = UpdateWorker(
            {"yt-dlp": "yt-dlp/yt-dlp"},
            github_route_mode="direct",
        )
        route = next(route for route in github_download_routes() if route.id == "direct")
        fallback = {"tag_name": "v4.0.0", "assets": []}
        with patch(
            "app.core.update_service.read_component_cache", return_value=None
        ), patch(
            "app.core.update_service.requests.get", return_value=Response()
        ), patch.object(
            worker, "_fetch_rate_limit_fallback", return_value=fallback
        ) as public_page:
            payload = worker._fetch_latest_payload_from_route(
                "yt-dlp/yt-dlp",
                {"User-Agent": "test"},
                route,
            )

        self.assertEqual(payload, fallback)
        public_page.assert_called_once()

    def test_empty_tags_response_is_not_accepted_as_a_latest_version(self) -> None:
        class Response:
            headers = {}

            def __init__(self, status_code: int, payload):
                self.status_code = status_code
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        worker = UpdateWorker(
            {"yt-dlp": "yt-dlp/yt-dlp"},
            github_route_mode="direct",
        )
        responses = [Response(404, {}), Response(200, [])]
        with patch("app.core.update_service.read_component_cache", return_value=None), patch(
            "app.core.update_service.write_component_cache"
        ) as write_cache, patch(
            "app.core.update_service.requests.get", side_effect=responses
        ):
            with self.assertRaisesRegex(RuntimeError, "Tags.*版本"):
                worker._fetch_latest_payload(
                    "yt-dlp/yt-dlp",
                    {"User-Agent": "test"},
                )

        write_cache.assert_not_called()

    def test_non_object_release_payload_is_not_cached_or_returned(self) -> None:
        class Response:
            status_code = 200
            headers = {}

            def raise_for_status(self):
                return None

            def json(self):
                return []

        worker = UpdateWorker(
            {"yt-dlp": "yt-dlp/yt-dlp"},
            github_route_mode="direct",
        )
        with patch("app.core.update_service.read_component_cache", return_value=None), patch(
            "app.core.update_service.write_component_cache"
        ) as write_cache, patch(
            "app.core.update_service.requests.get", return_value=Response()
        ):
            with self.assertRaisesRegex(RuntimeError, "版本数据"):
                worker._fetch_latest_payload(
                    "yt-dlp/yt-dlp",
                    {"User-Agent": "test"},
                )

        write_cache.assert_not_called()

    def test_not_modified_response_reuses_only_a_valid_cached_version(self) -> None:
        class Response:
            status_code = 304
            headers = {}

        cached = {
            "endpoint": "latest",
            "etag": '"release-etag"',
            "payload": {
                "tag_name": "v4.0.0",
                "assets": [],
                "_metadata_route": "direct",
            },
        }
        worker = UpdateWorker(
            {"yt-dlp": "yt-dlp/yt-dlp"},
            github_route_mode="direct",
        )
        with patch(
            "app.core.update_service.read_component_cache", return_value=cached
        ), patch("app.core.update_service.write_component_cache") as touch, patch(
            "app.core.update_service.requests.get", return_value=Response()
        ) as get:
            payload = worker._fetch_latest_payload(
                "yt-dlp/yt-dlp",
                {"User-Agent": "test"},
            )

        self.assertEqual(payload["tag_name"], "v4.0.0")
        self.assertEqual(get.call_args.kwargs["headers"]["If-None-Match"], '"release-etag"')
        self.assertEqual(touch.call_count, 1)
        self.assertEqual(touch.call_args.args[1], "yt-dlp/yt-dlp")
        self.assertEqual(touch.call_args.args[2]["tag_name"], "v4.0.0")

    def test_background_route_probe_is_started_only_once_per_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = UpdateService(directory)
            with patch.object(service, "probe_download_routes") as probe:
                service.start_background_route_probe()
                service.start_background_route_probe()
                self.assertEqual(probe.call_count, 1)

                service.set_download_routes("direct", "", "{}")
                service.start_background_route_probe()
                self.assertEqual(probe.call_count, 2)

    def test_failed_background_probe_can_retry_after_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = UpdateService(directory)
            with patch.object(service, "probe_download_routes", return_value=True) as probe:
                service.start_background_route_probe()
                # The probe method is mocked, so it never publishes the real
                # runtime that normally clears this flag on thread cleanup.
                service._background_route_probe_started = False
                service.start_background_route_probe()
                self.assertEqual(probe.call_count, 1)

                service._background_route_probe_last_attempt -= 61
                service.start_background_route_probe()
                self.assertEqual(probe.call_count, 2)

    def test_fresh_result_for_one_route_does_not_hide_missing_routes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = UpdateService(directory)
            routes = service.available_download_routes()
            direct = next(route for route in routes if route.id == "direct")
            service.route_probe_results = {
                direct.id: {
                    "id": direct.id,
                    "url": "https://github.com/",
                    "tested_at": int(__import__("time").time()),
                }
            }
            with patch.object(service, "probe_download_routes", return_value=True) as probe:
                service.start_background_route_probe()

            probe.assert_called_once()
            probed_routes = probe.call_args.kwargs["routes"]
            self.assertNotIn(direct.id, {route.id for route in probed_routes})
            self.assertEqual(len(probed_routes), len(routes) - 1)

    def test_partial_route_probe_merge_preserves_fresh_results(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = UpdateService(directory)
            service.route_probe_results = {
                "direct": {"id": "direct", "url": "https://github.com/", "tested_at": 100}
            }
            service._route_probe_completed([
                {"id": "mirror:gh-proxy", "url": "https://gh-proxy.com/", "tested_at": 200}
            ])

            self.assertIn("direct", service.route_probe_results)
            self.assertIn("mirror:gh-proxy", service.route_probe_results)

    def test_route_probe_cancellation_does_not_wait_for_inflight_socket(self) -> None:
        worker = GithubRouteProbeWorker(
            "https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest",
            github_download_routes()[:1],
        )
        started = threading.Event()
        release = threading.Event()

        def blocking_probe(_route):
            started.set()
            release.wait(5)
            return {"id": "blocked"}

        with patch.object(worker, "_probe_route", side_effect=blocking_probe):
            caller = threading.Thread(target=worker.run)
            caller.start()
            self.assertTrue(started.wait(1))
            worker.cancel()
            caller.join(0.5)
            release.set()

        self.assertFalse(caller.is_alive())

    def test_third_party_failure_falls_back_to_official_and_verifies_digest(self) -> None:
        payload = b"verified"
        digest = hashlib.sha256(payload).hexdigest()

        class Response:
            def __init__(self, url: str, fail: bool = False):
                self.url = url
                self.fail = fail

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def raise_for_status(self):
                if self.fail:
                    raise RuntimeError("mirror unavailable")

            def iter_content(self, chunk_size=0):
                yield payload

        official = "https://github.com/yt-dlp/yt-dlp/releases/download/v1/yt-dlp.exe"
        candidates = [
            {"url": "https://proxy.example/" + official, "name": "Proxy", "third_party": True},
            {"url": official, "name": "GitHub 直连", "third_party": False},
        ]
        responses = [Response(candidates[0]["url"], True), Response(official)]
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "yt-dlp.exe"
            worker = AssetDownloadWorker(candidates, target, "sha256:" + digest)
            finished = []
            errors = []
            worker.finished.connect(finished.append)
            worker.failed.connect(errors.append)
            with patch("app.core.update_download.requests.get", side_effect=responses):
                worker.run()
            self.assertFalse(errors)
            self.assertEqual(finished, [str(target)])
            self.assertEqual(target.read_bytes(), payload)

    def test_user_supplied_http_route_can_download_verified_asset(self) -> None:
        payload = b"verified-http"
        digest = hashlib.sha256(payload).hexdigest()

        class Response:
            url = "http://proxy.example/tool.exe"

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size=0):
                yield payload

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "tool.exe"
            worker = AssetDownloadWorker(
                [{
                    "url": "http://proxy.example/tool.exe",
                    "name": "User HTTP Proxy",
                    "third_party": True,
                }],
                target,
                "sha256:" + digest,
            )
            errors = []
            worker.failed.connect(errors.append)
            with patch("app.core.update_download.requests.get", return_value=Response()):
                worker.run()

            self.assertFalse(errors)
            self.assertEqual(target.read_bytes(), payload)


if __name__ == "__main__":
    unittest.main()
