from __future__ import annotations

import unittest

from app.core.redaction import redact_secret_text, redact_url


class RedactionTests(unittest.TestCase):
    def test_embedded_ipv6_url_keeps_host_brackets(self) -> None:
        self.assertEqual(
            redact_secret_text("GET http://[::1]"),
            "GET http://[::1]",
        )
        self.assertEqual(
            redact_secret_text("See [http://[::1]?token=private]."),
            "See [http://[::1]].",
        )

    def test_url_redaction_removes_credentials_query_and_fragment(self) -> None:
        self.assertEqual(
            redact_url("https://user:pass@example.com/video?q=private#fragment"),
            "https://example.com/video",
        )

    def test_json_secret_replacement_is_literal(self) -> None:
        self.assertEqual(
            redact_secret_text(
                '{"token": "private"}',
                replacement=r"\1literal",
                redact_urls=False,
            ),
            r'{"token": \1literal}',
        )


if __name__ == "__main__":
    unittest.main()
