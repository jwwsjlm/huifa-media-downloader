from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.core.component_update_cache import (
    read_component_cache,
    write_component_cache,
)


class ComponentUpdateCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "component-cache.json"

    def test_payload_is_bounded_and_does_not_store_unrelated_fields(self) -> None:
        self.assertTrue(write_component_cache(
            self.path,
            "owner/repository",
            {
                "tag_name": "v1.0.0",
                "body": "x" * 100_100,
                "Authorization": "secret",
                "assets": [
                    {"name": f"asset-{index}.zip", "private": "secret"}
                    for index in range(110)
                ],
            },
            endpoint="latest",
            schema_version=1,
            ttl_seconds=60,
        ))
        document = json.loads(self.path.read_text(encoding="utf-8"))
        payload = document["entries"]["owner/repository"]["payload"]

        self.assertEqual(len(payload["body"]), 100_000)
        self.assertEqual(len(payload["assets"]), 100)
        self.assertNotIn("Authorization", payload)
        self.assertNotIn("private", payload["assets"][0])

    def test_write_and_read_round_trip(self) -> None:
        self.assertTrue(write_component_cache(
            self.path,
            "Owner/Repository",
            {"tag_name": "v1.2.3", "assets": []},
            endpoint="latest",
            schema_version=1,
            ttl_seconds=60,
            response_headers={"ETag": '"release-1"'},
        ))

        entry = read_component_cache(
            self.path,
            "owner/repository",
            schema_version=1,
        )

        self.assertIsNotNone(entry)
        self.assertEqual(entry["etag"], '"release-1"')
        self.assertEqual(entry["payload"]["tag_name"], "v1.2.3")

    def test_corrupt_existing_timestamp_cannot_permanently_block_cache_updates(self) -> None:
        entries = {
            f"owner/repository-{index}": {
                "repo": f"owner/repository-{index}",
                "payload": {"tag_name": f"v{index}", "assets": []},
                "checked_at": "not-a-number",
            }
            for index in range(65)
        }
        self.path.write_text(json.dumps({
            "schema_version": 1,
            "entries": entries,
        }), encoding="utf-8")

        self.assertTrue(write_component_cache(
            self.path,
            "owner/new-repository",
            {"tag_name": "v2.0.0", "assets": []},
            endpoint="latest",
            schema_version=1,
            ttl_seconds=60,
        ))

        document = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(document["entries"]), 64)
        self.assertIn("owner/new-repository", document["entries"])


if __name__ == "__main__":
    unittest.main()
