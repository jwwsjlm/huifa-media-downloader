from __future__ import annotations

import unittest

from app.core.media_probe import (
    MediaStreamInfo,
    TranscodeError,
    VideoStreamInfo,
    validate_transcode_topology,
)


class MediaProbeTests(unittest.TestCase):
    def test_topology_rejects_swapped_language_and_disposition_metadata(self) -> None:
        source = VideoStreamInfo(
            "h264", 1920, 1080, 10.0,
            audio_stream_count=2,
            streams=(
                MediaStreamInfo("audio", "aac", "eng", ("default",)),
                MediaStreamInfo("audio", "aac", "jpn", ("comment",)),
            ),
        )
        output = VideoStreamInfo(
            "h264", 1920, 1080, 10.0,
            audio_stream_count=2,
            streams=(
                MediaStreamInfo("audio", "aac", "eng", ("comment",)),
                MediaStreamInfo("audio", "aac", "jpn", ("default",)),
            ),
        )

        with self.assertRaisesRegex(TranscodeError, "对应关系不完整"):
            validate_transcode_topology(source, output)

    def test_topology_rejects_attachment_or_data_stream_loss(self) -> None:
        source = VideoStreamInfo(
            "h264", 1920, 1080, 10.0,
            attachment_stream_count=1,
            data_stream_count=1,
        )
        output = VideoStreamInfo("h264", 1920, 1080, 10.0)

        with self.assertRaisesRegex(TranscodeError, "附件流数量不完整"):
            validate_transcode_topology(source, output)


if __name__ == "__main__":
    unittest.main()
