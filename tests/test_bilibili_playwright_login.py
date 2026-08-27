from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from app.integrations.social_auto_upload import runtime


class BilibiliPlaywrightLoginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = runtime._load_upstream()
        cls.browser_login = __import__(
            "uploader.bilibili_uploader.browser_login",
            fromlist=["browser_login"],
        )
        cls.biliup_runtime = __import__(
            "uploader.bilibili_uploader.runtime",
            fromlist=["runtime"],
        )

    def test_bilibili_login_no_longer_requires_an_interactive_terminal(self) -> None:
        expected = {"success": True, "message": "ok"}
        with patch.object(
            self.module,
            "login_bilibili_with_playwright",
            new=unittest.mock.AsyncMock(return_value=expected),
        ) as login:
            result = runtime._run(self.module.login_bilibili_account("work", headless=False))
        self.assertEqual(result, expected)
        login.assert_awaited_once()
        self.assertEqual(login.call_args.args[0], "work")
        self.assertFalse(login.call_args.kwargs["headless"])

    def test_web_cookie_exchange_matches_biliup_request_contract(self) -> None:
        calls: list[tuple[str, dict, dict | None]] = []

        def post_json(_session, url, form, headers):
            calls.append((url, dict(form), headers))
            if url.endswith("/auth_code"):
                return {"code": 0, "data": {"auth_code": "auth-code"}}
            if url.endswith("/confirm"):
                return {"code": 0}
            return {
                "code": 0,
                "data": {
                    "cookie_info": {"cookies": [{"name": "SESSDATA", "value": "secret"}]},
                    "sso": ["bilibili.com"],
                    "token_info": {
                        "access_token": "access-secret",
                        "expires_in": 3600,
                        "mid": 42,
                        "refresh_token": "refresh-secret",
                    },
                },
            }

        with patch.object(self.browser_login.time, "time", return_value=1_700_000_000):
            result = self.browser_login.exchange_web_cookies_for_login_info(
                "sess-secret",
                "csrf-secret",
                post_json=post_json,
            )

        self.assertEqual(result["platform"], "BiliTV")
        self.assertEqual(len(calls), 3)
        auth_form = calls[0][1]
        unsigned = {key: value for key, value in auth_form.items() if key != "sign"}
        encoded = urlencode(sorted((str(key), str(value)) for key, value in unsigned.items()))
        expected_sign = hashlib.md5(
            f"{encoded}{self.browser_login._BILITV_APP_SECRET}".encode("utf-8")
        ).hexdigest()
        self.assertEqual(auth_form["sign"], expected_sign)
        self.assertEqual(calls[1][1], {
            "auth_code": "auth-code",
            "csrf": "csrf-secret",
            "scanning_type": 3,
        })
        self.assertEqual(calls[1][2]["Cookie"], "SESSDATA=sess-secret; bili_jct=csrf-secret")
        self.assertEqual(calls[2][1]["auth_code"], "auth-code")
        self.assertIn("sign", calls[2][1])

    def test_login_info_is_written_atomically_without_leaving_temporary_files(self) -> None:
        payload = {
            "cookie_info": {"cookies": []},
            "sso": [],
            "token_info": {
                "access_token": "access-secret",
                "expires_in": 3600,
                "mid": 42,
                "refresh_token": "refresh-secret",
            },
            "platform": "BiliTV",
        }
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "cookies" / "bilibili_work.json"
            self.browser_login._atomic_write_login_info(destination, payload)
            self.assertEqual(json.loads(destination.read_text(encoding="utf-8")), payload)
            self.assertEqual(list(destination.parent.iterdir()), [destination])

    def test_biliup_binary_stays_under_the_portable_runtime_home(self) -> None:
        expected_root = Path(self.module.BASE_DIR) / "tools" / "biliup"
        self.assertEqual(self.biliup_runtime.get_biliup_runtime_root(), expected_root)
        self.assertTrue(
            self.biliup_runtime.build_biliup_runtime_path("Windows").is_relative_to(expected_root)
        )


if __name__ == "__main__":
    unittest.main()
