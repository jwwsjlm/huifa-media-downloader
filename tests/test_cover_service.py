from __future__ import annotations

import tempfile
import unittest
from dataclasses import fields
from pathlib import Path

from PySide6.QtGui import QColor, QImage, QImageReader

from app.core.cover_service import (
    COVER_PRESETS,
    ClipboardImageData,
    CoverExportOptions,
    CoverFitMode,
    CoverGenerationRequest,
    CoverLoadError,
    CoverPresetId,
    CoverSaveError,
    CoverService,
    CoverSourceKind,
    CoverValidationError,
)


class FakeResponse:
    def __init__(
        self,
        payload: bytes,
        *,
        url: str = "https://cdn.example.test/cover.png",
        content_type: str = "image/png",
        content_length: int | None = None,
    ) -> None:
        self.payload = payload
        self.url = url
        self.headers = {"Content-Type": content_type}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.closed = False

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, chunk_size: int = 0):
        step = max(1, min(chunk_size or len(self.payload), 7))
        for offset in range(0, len(self.payload), step):
            yield self.payload[offset : offset + step]

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def solid_image(width: int, height: int, color: str) -> QImage:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor(color))
    return image


def split_image() -> QImage:
    image = QImage(200, 100, QImage.Format.Format_RGB32)
    image.fill(QColor("red"))
    for x in range(100, 200):
        for y in range(100):
            image.setPixelColor(x, y, QColor("blue"))
    return image


class CoverServiceTests(unittest.TestCase):
    def test_commercial_presets_have_expected_ratios_and_sizes(self) -> None:
        landscape = COVER_PRESETS[CoverPresetId.LANDSCAPE_16_9]
        portrait = COVER_PRESETS[CoverPresetId.PORTRAIT_9_16]
        wechat_portrait = COVER_PRESETS[CoverPresetId.PORTRAIT_3_4]
        landscape_4_3 = COVER_PRESETS[CoverPresetId.LANDSCAPE_4_3]
        square = COVER_PRESETS[CoverPresetId.SQUARE_1_1]
        self.assertEqual(len(COVER_PRESETS), 5)
        self.assertEqual((landscape.width, landscape.height), (1280, 720))
        self.assertEqual((portrait.width, portrait.height), (1080, 1920))
        self.assertEqual((wechat_portrait.width, wechat_portrait.height), (1080, 1440))
        self.assertEqual((landscape_4_3.width, landscape_4_3.height), (1440, 1080))
        self.assertEqual((square.width, square.height), (1080, 1080))
        self.assertAlmostEqual(landscape.aspect_ratio, 16 / 9)
        self.assertAlmostEqual(portrait.aspect_ratio, 9 / 16)
        self.assertAlmostEqual(wechat_portrait.aspect_ratio, 3 / 4)
        self.assertAlmostEqual(landscape_4_3.aspect_ratio, 4 / 3)
        self.assertEqual(square.aspect_ratio, 1)

    def test_every_cover_preset_renders_to_its_exact_export_size(self) -> None:
        service = CoverService()
        try:
            source = solid_image(640, 360, "#336699")
            for preset_id, preset in COVER_PRESETS.items():
                with self.subTest(preset=preset_id.value):
                    rendered = service.render(
                        source,
                        CoverExportOptions.from_preset(preset_id),
                    )
                    self.assertEqual(
                        (rendered.width(), rendered.height()),
                        (preset.width, preset.height),
                    )
        finally:
            service.close()

    def test_local_cover_loads_with_metadata_and_is_detached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cover.png"
            self.assertTrue(solid_image(320, 180, "#336699").save(str(path), "PNG"))
            service = CoverService()
            try:
                cover = service.load(path)
            finally:
                service.close()
            self.assertEqual(cover.source_kind, CoverSourceKind.LOCAL)
            self.assertEqual((cover.width, cover.height), (320, 180))
            self.assertEqual(cover.source_format, "png")
            self.assertGreater(cover.byte_size, 0)

            changed = cover.image
            changed.fill(QColor("red"))
            self.assertEqual(cover.image.pixelColor(0, 0), QColor("#336699"))

    def test_remote_cover_streams_with_timeout_proxy_and_redirect_metadata(self) -> None:
        encoder = CoverService()
        try:
            png = encoder.prepare_clipboard(solid_image(64, 32, "green")).png_bytes
        finally:
            encoder.close()
        response = FakeResponse(png, url="https://images.example.test/final.png", content_length=len(png))
        session = FakeSession(response)
        service = CoverService(session=session)
        cover = service.load_url(
            "https://example.test/original.png",
            proxy="http://127.0.0.1:7890",
            request_headers={"Referer": "https://example.test/"},
        )
        self.assertEqual(cover.source_kind, CoverSourceKind.URL)
        self.assertEqual(cover.source, "https://images.example.test/final.png")
        self.assertEqual((cover.width, cover.height), (64, 32))
        self.assertTrue(response.closed)
        called_url, kwargs = session.calls[0]
        self.assertEqual(called_url, "https://example.test/original.png")
        self.assertTrue(kwargs["stream"])
        self.assertTrue(kwargs["allow_redirects"])
        self.assertEqual(kwargs["timeout"], (5.0, 20.0))
        self.assertEqual(kwargs["proxies"]["https"], "http://127.0.0.1:7890")
        self.assertEqual(kwargs["headers"]["Referer"], "https://example.test/")

    def test_remote_cover_rejects_non_image_and_oversized_content(self) -> None:
        html_response = FakeResponse(b"<html></html>", content_type="text/html")
        with self.assertRaisesRegex(CoverLoadError, "不是图像"):
            CoverService(session=FakeSession(html_response)).load_url("https://example.test/page")
        self.assertTrue(html_response.closed)

        large_response = FakeResponse(b"x", content_length=1025)
        with self.assertRaisesRegex(CoverLoadError, "超过"):
            CoverService(session=FakeSession(large_response), max_download_bytes=1024).load_url(
                "https://example.test/large.png"
            )
        self.assertTrue(large_response.closed)

    def test_source_loader_rejects_unsupported_uri_schemes_and_empty_images(self) -> None:
        service = CoverService()
        try:
            with self.assertRaisesRegex(CoverLoadError, "HTTP"):
                service.load("file:///C:/secret/cover.jpg")
            with self.assertRaisesRegex(CoverLoadError, "为空"):
                service.load_bytes(b"")
            with self.assertRaisesRegex(CoverLoadError, "无法解析"):
                service.load_bytes(b"not an image")
        finally:
            service.close()

    def test_crop_uses_aspect_fill_and_configurable_focus(self) -> None:
        service = CoverService()
        try:
            centered = service.render(
                split_image(),
                CoverExportOptions(100, 100, fit_mode=CoverFitMode.CROP),
            )
            left_focused = service.render(
                split_image(),
                CoverExportOptions(100, 100, fit_mode=CoverFitMode.CROP, focus_x=0),
            )
        finally:
            service.close()
        self.assertEqual(centered.size().toTuple(), (100, 100))
        self.assertEqual(centered.pixelColor(0, 50), QColor("red"))
        self.assertEqual(centered.pixelColor(99, 50), QColor("blue"))
        self.assertEqual(left_focused.pixelColor(99, 50), QColor("red"))

    def test_pad_keeps_aspect_ratio_and_uses_configured_background(self) -> None:
        service = CoverService()
        try:
            rendered = service.render(
                solid_image(100, 100, "red"),
                CoverExportOptions(
                    200,
                    100,
                    fit_mode=CoverFitMode.PAD,
                    background_color="#ffffff",
                ),
            )
        finally:
            service.close()
        self.assertEqual(rendered.pixelColor(0, 50), QColor("white"))
        self.assertEqual(rendered.pixelColor(49, 50), QColor("white"))
        self.assertEqual(rendered.pixelColor(50, 50), QColor("red"))
        self.assertEqual(rendered.pixelColor(149, 50), QColor("red"))
        self.assertEqual(rendered.pixelColor(150, 50), QColor("white"))

    def test_jpeg_export_is_atomic_configurable_and_decodable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target_without_suffix = root / "thumbnail"
            options = CoverExportOptions.from_preset(
                CoverPresetId.LANDSCAPE_16_9,
                quality=82,
                fit_mode=CoverFitMode.PAD,
                background_color="#101010",
            )
            service = CoverService()
            try:
                result = service.save_jpeg(solid_image(300, 300, "#cc5500"), target_without_suffix, options)
                with self.assertRaisesRegex(CoverSaveError, "已存在"):
                    service.save_jpeg(
                        solid_image(300, 300, "red"),
                        result.path,
                        options,
                        overwrite=False,
                    )
            finally:
                service.close()
            self.assertEqual(result.path.suffix, ".jpg")
            self.assertEqual((result.width, result.height), (1280, 720))
            self.assertEqual(result.quality, 82)
            self.assertEqual(result.byte_size, result.path.stat().st_size)
            reader = QImageReader(str(result.path))
            self.assertIn(bytes(reader.format()).lower(), {b"jpeg", b"jpg"})
            decoded = reader.read()
            self.assertFalse(decoded.isNull())
            self.assertEqual(decoded.size().toTuple(), (1280, 720))
            # QImageReader keeps its Windows file handle until destruction.
            del reader
            self.assertEqual(list(root.glob("*.tmp")), [])

    def test_clipboard_preparation_does_not_touch_global_clipboard(self) -> None:
        service = CoverService()
        try:
            payload = service.prepare_clipboard(
                solid_image(120, 80, "purple"),
                CoverExportOptions(60, 60, fit_mode=CoverFitMode.PAD),
            )
        finally:
            service.close()
        self.assertIsInstance(payload, ClipboardImageData)
        self.assertEqual((payload.width, payload.height), (60, 60))
        self.assertEqual(payload.to_qimage().size().toTuple(), (60, 60))
        mime_data = payload.to_mime_data()
        self.assertTrue(mime_data.hasImage())
        self.assertTrue(mime_data.hasFormat("image/png"))

    def test_generation_request_is_provider_neutral_immutable_and_has_no_credentials(self) -> None:
        service = CoverService()
        try:
            request = service.prepare_generation_request(
                solid_image(80, 40, "orange"),
                "  保留主体，增强标题可读性  ",
                options=CoverExportOptions(160, 90),
                count=2,
                provider_options={"model": "gpt-image", "style": {"strength": 0.7}},
            )
        finally:
            service.close()
        self.assertEqual(request.prompt, "保留主体，增强标题可读性")
        self.assertEqual((request.width, request.height, request.count), (160, 90, 2))
        self.assertEqual(request.provider_options["model"], "gpt-image")
        with self.assertRaises(TypeError):
            request.provider_options["model"] = "changed"  # type: ignore[index]
        with self.assertRaises(TypeError):
            request.provider_options["style"]["strength"] = 1  # type: ignore[index]

        with self.assertRaisesRegex(CoverValidationError, "API Key"):
            CoverGenerationRequest(
                request.source_png,
                "prompt",
                160,
                90,
                provider_options={"auth": {"api_key": "must-not-live-here"}},
            )
        with self.assertRaisesRegex(CoverValidationError, "API Key"):
            CoverGenerationRequest(
                request.source_png,
                "prompt",
                160,
                90,
                provider_options={"openai_api_key": "must-not-live-here"},
            )
        # Non-secret generation controls that merely contain the word token
        # must remain usable by future providers.
        token_budget_request = CoverGenerationRequest(
            request.source_png,
            "prompt",
            160,
            90,
            provider_options={"max_tokens": 1024},
        )
        self.assertEqual(token_budget_request.provider_options["max_tokens"], 1024)
        request_fields = {item.name.casefold() for item in fields(CoverGenerationRequest)}
        self.assertFalse(request_fields & {"api_key", "token", "password", "authorization"})

    def test_export_option_validation_is_actionable(self) -> None:
        with self.assertRaisesRegex(CoverValidationError, "质量"):
            CoverExportOptions(1280, 720, quality=0)
        with self.assertRaisesRegex(CoverValidationError, "背景颜色"):
            CoverExportOptions(1280, 720, background_color="not-a-color")
        with self.assertRaisesRegex(CoverValidationError, "裁剪焦点"):
            CoverExportOptions(1280, 720, focus_x=2)
        with self.assertRaisesRegex(CoverValidationError, "未知封面预设"):
            CoverExportOptions.from_preset("unknown")


if __name__ == "__main__":
    unittest.main()
