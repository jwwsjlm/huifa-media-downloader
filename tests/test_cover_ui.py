from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QThread, Qt
from PySide6.QtGui import QColor, QImage, QImageReader, QPixmapCache
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QWidget

from app.core.cover_service import CoverService
from app.storage.models import MediaItem
from app.ui.cover_studio import CoverGenerationWorker, CoverStudioDialog
from app.ui.cover_workflow_controller import (
    CoverWorkflowController,
    cover_options_from_settings,
)
from app.ui.completed_page import CompletedPage
from app.ui.media_presentation import thumbnail_pixmap


APP = QApplication.instance() or QApplication([])


class FakeSettings:
    def __init__(self, **values: str):
        self.values = values

    def get(self, key: str) -> str:
        return str(self.values.get(key, ""))

    def get_int(self, key: str, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
        try:
            value = int(self.values.get(key, default))
        except (TypeError, ValueError):
            value = int(default)
        if minimum is not None:
            value = max(minimum, value)
        if maximum is not None:
            value = min(maximum, value)
        return value

    def set(self, key: str, value: str) -> None:
        self.values[key] = str(value)

    def set_many(self, values: dict[str, str]) -> dict[str, str]:
        normalized = {str(key): str(value) for key, value in values.items()}
        self.values.update(normalized)
        return normalized


def save_image(path: Path, width: int = 320, height: int = 180) -> None:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    image.fill(QColor("#336699"))
    if not image.save(str(path), "PNG"):
        raise AssertionError(f"cannot save fixture image: {path}")


class CoverUiTests(unittest.TestCase):
    def test_cover_worker_finishes_even_when_provider_cleanup_fails(self) -> None:
        class Provider:
            def generate(self, *_args, **_kwargs):
                return []

            def close(self) -> None:
                raise RuntimeError("provider cleanup failed")

        worker = CoverGenerationWorker(Provider(), object())
        finished: list[bool] = []
        worker.finished.connect(lambda: finished.append(True))

        worker.run()

        self.assertEqual(finished, [True])

    def test_cover_generation_thread_start_failure_restores_controls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cover_path = Path(directory) / "cover.png"
            save_image(cover_path)
            service = CoverService()
            provider = SimpleNamespace(close_calls=0)
            provider.close = lambda: setattr(provider, "close_calls", provider.close_calls + 1)

            class FakeWindow(QWidget):
                def __init__(self):
                    super().__init__()
                    self.cover_service = service
                    self.secure_store = SimpleNamespace(get=lambda _key: "test-key")
                    self.app_settings = FakeSettings(
                        cover_preset="landscape_16_9",
                        cover_fit_mode="crop",
                        cover_jpeg_quality="90",
                        cover_focus_x="50",
                        cover_focus_y="50",
                        cover_ai_model="gpt-image-2",
                        cover_ai_api_url="",
                    )

            window = FakeWindow()
            dialog = CoverStudioDialog(
                MediaItem(id=1, title="demo", thumbnail_path=str(cover_path)),
                window,
            )
            dialog.prompt.setPlainText("test prompt")
            try:
                with patch.object(
                    service,
                    "prepare_generation_request",
                    return_value=object(),
                ), patch(
                    "app.ui.cover_studio.OpenAICoverGenerationProvider",
                    return_value=provider,
                ), patch(
                    "app.ui.cover_studio.QThread.start",
                    side_effect=RuntimeError("thread resource exhausted"),
                ):
                    dialog.generate_cover()

                self.assertIsNone(dialog.generation_thread)
                self.assertIsNone(dialog.generation_worker)
                self.assertTrue(dialog.ai_button.isEnabled())
                self.assertTrue(dialog.ai_progress.isHidden())
                self.assertIn("thread resource exhausted", dialog.status.text())
                self.assertEqual(provider.close_calls, 1)
                dialog.resize(900, 700)
                dialog.show()
                APP.processEvents()
                self.assertIn("thread resource exhausted", dialog.status.text())
            finally:
                dialog.deleteLater()
                window.deleteLater()
                service.close()
                APP.processEvents()

    def test_cover_generation_wiring_failure_never_publishes_partial_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cover_path = Path(directory) / "cover.png"
            save_image(cover_path)
            service = CoverService()
            provider = SimpleNamespace(close_calls=0)
            provider.close = lambda: setattr(provider, "close_calls", provider.close_calls + 1)

            class FakeWindow(QWidget):
                def __init__(self):
                    super().__init__()
                    self.cover_service = service
                    self.secure_store = SimpleNamespace(get=lambda _key: "test-key")
                    self.app_settings = FakeSettings(
                        cover_preset="landscape_16_9",
                        cover_fit_mode="crop",
                        cover_jpeg_quality="90",
                        cover_focus_x="50",
                        cover_focus_y="50",
                        cover_ai_model="gpt-image-2",
                        cover_ai_api_url="",
                    )

            window = FakeWindow()
            dialog = CoverStudioDialog(
                MediaItem(id=1, title="demo", thumbnail_path=str(cover_path)),
                window,
            )
            dialog.prompt.setPlainText("test prompt")
            try:
                with patch.object(
                    service,
                    "prepare_generation_request",
                    return_value=object(),
                ), patch(
                    "app.ui.cover_studio.OpenAICoverGenerationProvider",
                    return_value=provider,
                ), patch(
                    "app.ui.cover_studio.CoverGenerationWorker.moveToThread",
                    side_effect=RuntimeError("signal wiring failed"),
                ), patch(
                    "app.ui.cover_studio.delete_unstarted_worker",
                ) as delete_worker, patch(
                    "app.ui.cover_studio.QThread.start",
                ) as start_thread:
                    dialog.generate_cover()

                start_thread.assert_not_called()
                delete_worker.assert_called_once()
                self.assertIsNone(dialog.generation_thread)
                self.assertIsNone(dialog.generation_worker)
                self.assertTrue(dialog.ai_button.isEnabled())
                self.assertTrue(dialog.ai_progress.isHidden())
                self.assertIn("signal wiring failed", dialog.status.text())
                self.assertEqual(provider.close_calls, 1)
            finally:
                dialog.deleteLater()
                window.deleteLater()
                service.close()
                APP.processEvents()

    def test_cover_generation_thread_construction_failure_closes_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cover_path = Path(directory) / "cover.png"
            save_image(cover_path)
            service = CoverService()
            provider = SimpleNamespace(close_calls=0)
            provider.close = lambda: setattr(provider, "close_calls", provider.close_calls + 1)

            class FakeWindow(QWidget):
                def __init__(self):
                    super().__init__()
                    self.cover_service = service
                    self.secure_store = SimpleNamespace(get=lambda _key: "test-key")
                    self.app_settings = FakeSettings(
                        cover_preset="landscape_16_9",
                        cover_fit_mode="crop",
                        cover_jpeg_quality="90",
                        cover_focus_x="50",
                        cover_focus_y="50",
                        cover_ai_model="gpt-image-2",
                        cover_ai_api_url="",
                    )

            window = FakeWindow()
            dialog = CoverStudioDialog(
                MediaItem(id=1, title="demo", thumbnail_path=str(cover_path)),
                window,
            )
            dialog.prompt.setPlainText("test prompt")
            try:
                with patch.object(
                    service,
                    "prepare_generation_request",
                    return_value=object(),
                ), patch(
                    "app.ui.cover_studio.OpenAICoverGenerationProvider",
                    return_value=provider,
                ), patch(
                    "app.ui.cover_studio.QThread",
                    side_effect=RuntimeError("thread construction failed"),
                ):
                    dialog.generate_cover()

                self.assertIsNone(dialog.generation_thread)
                self.assertIsNone(dialog.generation_worker)
                self.assertIn("thread construction failed", dialog.status.text())
                self.assertEqual(provider.close_calls, 1)
            finally:
                dialog.deleteLater()
                window.deleteLater()
                service.close()
                APP.processEvents()

    def test_cover_generation_cleanup_waits_for_queued_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cover_path = Path(directory) / "cover.png"
            save_image(cover_path)
            service = CoverService()

            class FakeWindow(QWidget):
                def __init__(self):
                    super().__init__()
                    self.cover_service = service
                    self.app_settings = FakeSettings(
                        cover_preset="landscape_16_9",
                        cover_fit_mode="crop",
                        cover_jpeg_quality="90",
                        cover_focus_x="50",
                        cover_focus_y="50",
                    )

            window = FakeWindow()
            dialog = CoverStudioDialog(
                MediaItem(id=1, title="demo", thumbnail_path=str(cover_path)),
                window,
            )
            thread = QThread(dialog)
            dialog.generation_thread = thread
            dialog.generation_worker = object()  # type: ignore[assignment]
            dialog.ai_button.setEnabled(False)
            dialog.ai_progress.show()
            try:
                dialog._defer_generation_thread_finish(thread)
                dialog.on_generation_failed("late provider failure")
                APP.processEvents()

                self.assertIn("late provider failure", dialog.status.text())
                self.assertIsNone(dialog.generation_thread)
                self.assertIsNone(dialog.generation_worker)
                self.assertTrue(dialog.ai_button.isEnabled())
                self.assertTrue(dialog.ai_progress.isHidden())
            finally:
                dialog.deleteLater()
                window.deleteLater()
                service.close()
                APP.processEvents()

    def test_card_thumbnail_uses_reduced_decode_and_shared_pixmap_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cover_path = Path(directory) / "large-cover.png"
            save_image(cover_path, 1200, 900)
            real_reader = QImageReader

            class TrackingReader:
                read_count = 0
                scaled_sizes = []

                def __init__(self, path: str):
                    self.reader = real_reader(path)

                def setAutoTransform(self, enabled: bool) -> None:
                    self.reader.setAutoTransform(enabled)

                def size(self):
                    return self.reader.size()

                def setScaledSize(self, size) -> None:
                    self.scaled_sizes.append((size.width(), size.height()))
                    self.reader.setScaledSize(size)

                def read(self):
                    type(self).read_count += 1
                    return self.reader.read()

            QPixmapCache.clear()
            with patch("app.ui.media_presentation.QImageReader", TrackingReader):
                first = thumbnail_pixmap(str(cover_path), 116, 68)
                second = thumbnail_pixmap(str(cover_path), 116, 68)

            self.assertFalse(first.isNull())
            self.assertEqual(first.size(), second.size())
            self.assertLessEqual(first.width(), 116)
            self.assertLessEqual(first.height(), 68)
            self.assertEqual(TrackingReader.read_count, 1)
            self.assertEqual(len(TrackingReader.scaled_sizes), 1)
            self.assertLessEqual(TrackingReader.scaled_sizes[0][0], 116)
            self.assertLessEqual(TrackingReader.scaled_sizes[0][1], 68)
            QPixmapCache.clear()

    def test_default_cover_options_include_saved_crop_focus(self) -> None:
        settings = FakeSettings(
            cover_preset="portrait_9_16",
            cover_fit_mode="crop",
            cover_jpeg_quality="86",
            cover_focus_x="25",
            cover_focus_y="80",
        )

        options = cover_options_from_settings(settings)

        self.assertEqual((options.width, options.height), (1080, 1920))
        self.assertEqual(options.quality, 86)
        self.assertAlmostEqual(options.focus_x, 0.25)
        self.assertAlmostEqual(options.focus_y, 0.80)

    def test_new_platform_presets_are_available_to_default_exports(self) -> None:
        cases = (
            ("portrait_3_4", (1080, 1440)),
            ("landscape_4_3", (1440, 1080)),
        )
        for preset, expected_size in cases:
            with self.subTest(preset=preset):
                settings = FakeSettings(
                    cover_preset=preset,
                    cover_fit_mode="crop",
                    cover_jpeg_quality="90",
                    cover_focus_x="50",
                    cover_focus_y="50",
                )
                options = cover_options_from_settings(settings)
                self.assertEqual((options.width, options.height), expected_size)

    def test_completed_copy_uses_default_cover_transform(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cover_path = Path(directory) / "cover.png"
            save_image(cover_path, 320, 180)
            service = CoverService()
            parent = QWidget()
            workflow = CoverWorkflowController(
                parent,
                FakeSettings(
                    cover_preset="square_1_1",
                    cover_fit_mode="crop",
                    cover_jpeg_quality="90",
                    cover_focus_x="25",
                    cover_focus_y="50",
                ),
                service,
            )
            window = SimpleNamespace(
                cover_workflow=workflow,
            )
            page = CompletedPage(window)
            captured: list[object] = []
            fake_clipboard = SimpleNamespace(setMimeData=lambda value: captured.append(value))
            try:
                with patch.object(QApplication, "clipboard", return_value=fake_clipboard):
                    page.copy_cover(MediaItem(id=1, thumbnail_path=str(cover_path)))
                self.assertEqual(len(captured), 1)
                copied = captured[0].imageData()
                self.assertEqual((copied.width(), copied.height()), (1080, 1080))
                self.assertIn("1080×1080", page.summary.text())
            finally:
                page.close()
                parent.close()
                service.close()

    def test_cover_studio_exposes_focus_and_protects_unsaved_ai_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cover_path = Path(directory) / "cover.png"
            save_image(cover_path)
            service = CoverService()

            class FakeWindow(QWidget):
                def __init__(self):
                    super().__init__()
                    self.cover_service = service
                    self.app_settings = FakeSettings(
                        cover_preset="landscape_16_9",
                        cover_fit_mode="crop",
                        cover_jpeg_quality="90",
                        cover_focus_x="20",
                        cover_focus_y="75",
                    )

            window = FakeWindow()
            dialog = CoverStudioDialog(MediaItem(id=1, title="demo", thumbnail_path=str(cover_path)), window)
            try:
                self.assertEqual(dialog.preset.count(), 5)
                options = dialog.export_options()
                self.assertAlmostEqual(options.focus_x, 0.20)
                self.assertAlmostEqual(options.focus_y, 0.75)
                wechat_index = dialog.preset.findData("portrait_3_4")
                self.assertGreaterEqual(wechat_index, 0)
                dialog.preset.setCurrentIndex(wechat_index)
                APP.processEvents()
                wechat_options = dialog.export_options()
                self.assertEqual((wechat_options.width, wechat_options.height), (1080, 1440))
                self.assertIn("微信视频号", dialog.preset.toolTip())
                dialog.current = service.load_local(cover_path)
                dialog._generated_dirty = True
                dialog.show()
                APP.processEvents()
                with patch.object(QMessageBox, "question", return_value=QMessageBox.No):
                    dialog.reject()
                self.assertTrue(dialog.isVisible())
                with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
                    dialog.reject()
                APP.processEvents()
                self.assertFalse(dialog.isVisible())
            finally:
                dialog.deleteLater()
                window.deleteLater()
                service.close()
                APP.processEvents()

    def test_cover_studio_protects_unconfirmed_crop_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cover_path = Path(directory) / "cover.png"
            save_image(cover_path)
            service = CoverService()

            class FakeWindow(QWidget):
                def __init__(self):
                    super().__init__()
                    self.cover_service = service
                    self.app_settings = FakeSettings(
                        cover_preset="landscape_16_9",
                        cover_fit_mode="crop",
                        cover_jpeg_quality="90",
                        cover_focus_x="50",
                        cover_focus_y="50",
                    )

            window = FakeWindow()
            dialog = CoverStudioDialog(
                MediaItem(id=1, title="demo", thumbnail_path=str(cover_path)),
                window,
            )
            try:
                dialog.show()
                APP.processEvents()
                dialog.focus_x.setValue(35)
                dialog.quality.setValue(88)
                self.assertTrue(dialog._options_dirty)

                with patch.object(
                    QMessageBox,
                    "question",
                    return_value=QMessageBox.No,
                ):
                    dialog.reject()
                self.assertTrue(dialog.isVisible())

                dialog.confirm_crop_position()
                self.assertFalse(dialog._options_dirty)
                self.assertEqual(window.app_settings.get("cover_focus_x"), "35.0")
                self.assertEqual(window.app_settings.get("cover_jpeg_quality"), "88")

                with patch.object(QMessageBox, "question") as question:
                    dialog.reject()
                question.assert_not_called()
                APP.processEvents()
                self.assertFalse(dialog.isVisible())
            finally:
                dialog.deleteLater()
                window.deleteLater()
                service.close()
                APP.processEvents()

    def test_cover_studio_keeps_crop_dirty_when_atomic_save_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cover_path = Path(directory) / "cover.png"
            save_image(cover_path)
            service = CoverService()

            class FakeWindow(QWidget):
                def __init__(self):
                    super().__init__()
                    self.cover_service = service
                    self.app_settings = FakeSettings(
                        cover_preset="landscape_16_9",
                        cover_fit_mode="crop",
                        cover_jpeg_quality="90",
                        cover_focus_x="50",
                        cover_focus_y="50",
                    )

            window = FakeWindow()
            dialog = CoverStudioDialog(
                MediaItem(id=1, title="demo", thumbnail_path=str(cover_path)),
                window,
            )
            try:
                dialog.focus_x.setValue(35)
                with patch.object(
                    window.app_settings,
                    "set_many",
                    side_effect=OSError("settings disk unavailable"),
                ), patch.object(QMessageBox, "warning") as warning:
                    dialog.confirm_crop_position()

                self.assertTrue(dialog._options_dirty)
                self.assertEqual(window.app_settings.get("cover_focus_x"), "50")
                warning.assert_called_once()
                self.assertIn("settings disk unavailable", warning.call_args.args[2])
            finally:
                dialog.deleteLater()
                window.deleteLater()
                service.close()
                APP.processEvents()

    def test_cover_studio_drag_moves_crop_without_changing_target_ratio(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cover_path = Path(directory) / "wide.png"
            save_image(cover_path, 1600, 900)
            service = CoverService()

            class FakeWindow(QWidget):
                def __init__(self):
                    super().__init__()
                    self.cover_service = service
                    self.app_settings = FakeSettings(
                        cover_preset="portrait_9_16",
                        cover_fit_mode="crop",
                        cover_jpeg_quality="90",
                        cover_focus_x="50",
                        cover_focus_y="50",
                    )

            window = FakeWindow()
            dialog = CoverStudioDialog(MediaItem(id=1, title="demo", thumbnail_path=str(cover_path)), window)
            try:
                dialog.show()
                APP.processEvents()
                before = dialog.export_options()
                self.assertEqual((before.width, before.height), (1080, 1920))
                center = dialog.preview.rect().center()
                QTest.mousePress(dialog.preview, Qt.LeftButton, pos=center)
                QTest.mouseMove(dialog.preview, QPoint(center.x() + 90, center.y()))
                QTest.mouseRelease(dialog.preview, Qt.LeftButton, pos=QPoint(center.x() + 90, center.y()))
                APP.processEvents()
                after = dialog.export_options()
                self.assertEqual((after.width, after.height), (1080, 1920))
                self.assertLess(after.focus_x, before.focus_x)
                self.assertAlmostEqual(after.focus_y, before.focus_y, places=2)
                dialog.confirm_crop_position()
                self.assertEqual(window.app_settings.get("cover_preset"), "portrait_9_16")
                self.assertIn("50", window.app_settings.get("cover_focus_y"))
            finally:
                dialog.deleteLater()
                window.deleteLater()
                service.close()
                APP.processEvents()

    def test_cover_tools_menu_opens_below_its_button(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cover_path = Path(directory) / "cover.png"
            save_image(cover_path)
            media = MediaItem(id=7, title="demo", thumbnail_path=str(cover_path))
            positions = []

            class FakeMenu:
                def __init__(self, _parent=None):
                    pass

                def addAction(self, _label):
                    return object()

                def addSeparator(self):
                    pass

                def exec(self, position):
                    positions.append(position)
                    return None

            class FakePage(QWidget):
                def __init__(self):
                    super().__init__()
                    self.button = QPushButton("cover", self)
                    self.button.resize(120, 32)
                    self.button.move(40, 50)
                    self.cards = {7: SimpleNamespace(cover_button=self.button)}
                    self.window = SimpleNamespace(
                        db=SimpleNamespace(
                            get_media=lambda media_id: media if media_id == 7 else None,
                        ),
                        cover_workflow=SimpleNamespace(
                            open_studio=lambda _media: None,
                            save_as_jpeg=lambda _media: None,
                        )
                    )

                def copy_cover(self, _media):
                    pass


            page = FakePage()
            page.show()
            APP.processEvents()
            expected = page.button.mapToGlobal(QPoint(0, page.button.height()))
            try:
                with patch("app.ui.completed_page.QMenu", FakeMenu):
                    CompletedPage.show_cover_menu(page, 7)
                self.assertEqual(positions, [expected])
            finally:
                page.deleteLater()
                APP.processEvents()


if __name__ == "__main__":
    unittest.main()
