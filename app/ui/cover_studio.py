from __future__ import annotations

import threading
from functools import partial
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QSize, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.adapters.openai_cover_provider import OpenAICoverGenerationProvider
from app.core.cover_service import (
    COVER_PRESETS,
    CoverExportOptions,
    CoverFitMode,
    CoverPresetId,
    CoverServiceError,
)
from app.core.qt_lifecycle import delete_unstarted_worker
from app.storage.models import MediaItem
from app.ui.i18n import format_text as ui_format
from app.ui.i18n import runtime_text, text as ui_text
from app.ui.cover_export_paths import (
    default_cover_export_path,
    normalized_jpeg_target,
)
from app.ui.media_presentation import format_file_size

if TYPE_CHECKING:
    from app.ui.main_window import MainWindow


COVER_PRESET_LABEL_KEYS = {
    CoverPresetId.LANDSCAPE_16_9: "Landscape 16:9",
    CoverPresetId.PORTRAIT_9_16: "Douyin Portrait 9:16",
    CoverPresetId.PORTRAIT_3_4: "WeChat Channels Portrait 3:4",
    CoverPresetId.LANDSCAPE_4_3: "WeChat / General Landscape 4:3",
    CoverPresetId.SQUARE_1_1: "Square 1:1",
}

COVER_PRESET_HINTS = {
    CoverPresetId.LANDSCAPE_16_9: (
        "For landscape video, YouTube and standard player covers."
    ),
    CoverPresetId.PORTRAIT_9_16: (
        "For Douyin and full-screen vertical short-video covers."
    ),
    CoverPresetId.PORTRAIT_3_4: (
        "For WeChat Channels portrait covers and common 3:4 social artwork."
    ),
    CoverPresetId.LANDSCAPE_4_3: (
        "For WeChat and traditional 4:3 landscape covers."
    ),
    CoverPresetId.SQUARE_1_1: (
        "For square feeds, card artwork and general social platforms."
    ),
}


def populate_cover_preset_combo(combo: QComboBox) -> None:
    """Populate a cover preset selector with consistent platform hints."""

    combo.clear()
    for preset_id, preset in COVER_PRESETS.items():
        preset_name = ui_text(COVER_PRESET_LABEL_KEYS[preset_id])
        combo.addItem(
            f"{preset_name} ({preset.width}×{preset.height})",
            preset_id.value,
        )
        combo.setItemData(
            combo.count() - 1,
            ui_text(COVER_PRESET_HINTS[preset_id]),
            Qt.ToolTipRole,
        )

    def update_tooltip(index: int) -> None:
        combo.setToolTip(str(combo.itemData(index, Qt.ToolTipRole) or ""))

    combo.currentIndexChanged.connect(update_tooltip)
    update_tooltip(combo.currentIndex())


class CoverGenerationWorker(QObject):
    progress = Signal(int, str)
    generated = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, provider: OpenAICoverGenerationProvider, request):
        super().__init__()
        self.provider = provider
        self.request = request
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    @Slot()
    def run(self) -> None:
        try:
            results = self.provider.generate(
                self.request,
                cancel_event=self.cancel_event,
                progress=lambda value, message: self.progress.emit(value, message),
            )
            if self.cancel_event.is_set():
                self.failed.emit("AI 封面创作已取消")
            elif results:
                self.generated.emit(results[0])
            else:
                self.failed.emit("AI 图像服务没有返回封面")
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            try:
                self.provider.close()
            except Exception:
                # Cleanup failures must never suppress the completion signal;
                # otherwise the owning QThread cannot quit and the dialog
                # remains permanently busy.
                pass
            finally:
                self.finished.emit()


class CoverCropPreview(QLabel):
    focus_changed = Signal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._crop_enabled = False
        self._source_size = QSize()
        self._target_size = QSize()
        self._focus = (0.5, 0.5)
        self._drag_origin = None
        self._drag_focus = self._focus

    def configure_crop(
        self,
        source_size: QSize,
        target_size: QSize,
        focus_x: float,
        focus_y: float,
        enabled: bool,
    ) -> None:
        self._source_size = QSize(source_size)
        self._target_size = QSize(target_size)
        self._focus = (
            max(0.0, min(1.0, float(focus_x))),
            max(0.0, min(1.0, float(focus_y))),
        )
        self._crop_enabled = bool(
            enabled and source_size.isValid() and target_size.isValid()
        )
        self.setCursor(
            Qt.CursorShape.OpenHandCursor
            if self._crop_enabled
            else Qt.CursorShape.ArrowCursor
        )

    def mousePressEvent(self, event) -> None:
        if self._crop_enabled and event.button() == Qt.MouseButton.LeftButton:
            self._drag_origin = event.position()
            self._drag_focus = self._focus
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_origin is None or not self._crop_enabled:
            super().mouseMoveEvent(event)
            return
        pixmap = self.pixmap()
        if pixmap is None or pixmap.isNull():
            return
        source_w, source_h = self._source_size.width(), self._source_size.height()
        target_w, target_h = self._target_size.width(), self._target_size.height()
        scale = max(target_w / source_w, target_h / source_h)
        overflow_x = max(0.0, source_w * scale - target_w)
        overflow_y = max(0.0, source_h * scale - target_h)
        display_scale_x = pixmap.width() / max(1.0, target_w)
        display_scale_y = pixmap.height() / max(1.0, target_h)
        delta = event.position() - self._drag_origin
        focus_x, focus_y = self._drag_focus
        if overflow_x > 0:
            focus_x -= delta.x() / (overflow_x * display_scale_x)
        if overflow_y > 0:
            focus_y -= delta.y() / (overflow_y * display_scale_y)
        focus_x = max(0.0, min(1.0, focus_x))
        focus_y = max(0.0, min(1.0, focus_y))
        self._focus = (focus_x, focus_y)
        self.focus_changed.emit(focus_x, focus_y)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if (
            self._drag_origin is not None
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self._drag_origin = None
            self.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class CoverStudioDialog(QDialog):
    def __init__(self, media: MediaItem, window: MainWindow):
        super().__init__(window)
        self.window = window
        self.media = media
        self.service = window.cover_service
        self.source = self.service.load_local(media.thumbnail_path)
        self.current = self.source
        self._generated_dirty = False
        self._options_dirty = False
        self._close_confirmed = False
        self.generation_thread: QThread | None = None
        self.generation_worker: CoverGenerationWorker | None = None
        self._deferred_generation_finishes: set[QThread] = set()

        root = self._build_dialog_shell()
        root.addLayout(self._build_editor_body(), 1)
        self._build_status_area(root)
        root.addLayout(self._build_action_buttons())
        self._connect_controls()
        self.update_focus_controls()
        self.refresh_preview()

    def _build_dialog_shell(self) -> QVBoxLayout:
        self.setWindowTitle(ui_text("Cover Studio"))
        self.resize(850, 650)
        root = QVBoxLayout(self)
        heading = QLabel(self.media.title or ui_text("Cover Studio"))
        heading.setObjectName("pageTitle")
        heading.setToolTip(self.media.title)
        root.addWidget(heading)
        return root

    def _build_editor_body(self) -> QHBoxLayout:
        body = QHBoxLayout()
        body.addWidget(self._build_preview(), 1)
        body.addLayout(self._build_export_controls())
        return body

    def _build_preview(self) -> CoverCropPreview:
        self.preview = CoverCropPreview()
        self.preview.setObjectName("coverStudioPreview")
        self.preview.setMinimumSize(470, 360)
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setToolTip(ui_text(
            "Drag the image to choose the crop position; the selected "
            "aspect ratio stays unchanged."
        ))
        return self.preview

    def _build_export_controls(self) -> QFormLayout:
        controls = QFormLayout()
        controls.addRow(ui_text("Preset"), self._build_preset_control())
        controls.addRow(ui_text("Fit Mode"), self._build_fit_control())
        controls.addRow(ui_text("Crop Focus"), self._build_focus_row())
        controls.addRow(ui_text("JPG Quality"), self._build_quality_control())
        controls.addRow(
            ui_text("Output Format"),
            QLabel(ui_text("JPG (compatible with all publishing platforms)")),
        )
        controls.addRow(ui_text("AI Prompt"), self._build_prompt_control())
        return controls

    def _build_preset_control(self) -> QComboBox:
        self.preset = QComboBox()
        populate_cover_preset_combo(self.preset)
        saved_preset = (
            self.window.app_settings.get("cover_preset")
            or CoverPresetId.LANDSCAPE_16_9.value
        )
        self.preset.setCurrentIndex(max(0, self.preset.findData(saved_preset)))
        return self.preset

    def _build_fit_control(self) -> QComboBox:
        self.fit = QComboBox()
        self.fit.addItem(ui_text("Smart crop to fill"), CoverFitMode.CROP.value)
        self.fit.addItem(
            ui_text("Keep full image with padding"),
            CoverFitMode.PAD.value,
        )
        self.fit.setCurrentIndex(
            max(
                0,
                self.fit.findData(
                    self.window.app_settings.get("cover_fit_mode") or "crop"
                ),
            )
        )
        return self.fit

    def _build_focus_row(self) -> QWidget:
        self.focus_x = QDoubleSpinBox()
        self.focus_x.setRange(0, 100)
        self.focus_x.setDecimals(1)
        self.focus_x.setSingleStep(0.5)
        self.focus_x.setSuffix(" %")
        self.focus_x.setValue(self._saved_focus("cover_focus_x"))
        self.focus_x.setToolTip(ui_text("0% left, 50% center, 100% right"))
        self.focus_y = QDoubleSpinBox()
        self.focus_y.setRange(0, 100)
        self.focus_y.setDecimals(1)
        self.focus_y.setSingleStep(0.5)
        self.focus_y.setSuffix(" %")
        self.focus_y.setValue(self._saved_focus("cover_focus_y"))
        self.focus_y.setToolTip(ui_text("0% top, 50% center, 100% bottom"))

        focus_row = QWidget()
        focus_layout = QHBoxLayout(focus_row)
        focus_layout.setContentsMargins(0, 0, 0, 0)
        focus_layout.setSpacing(6)
        focus_layout.addWidget(QLabel(ui_text("Horizontal")))
        focus_layout.addWidget(self.focus_x)
        focus_layout.addWidget(QLabel(ui_text("Vertical")))
        focus_layout.addWidget(self.focus_y)
        return focus_row

    def _build_quality_control(self) -> QSpinBox:
        self.quality = QSpinBox()
        self.quality.setRange(50, 100)
        self.quality.setSuffix(" %")
        self.quality.setValue(
            self.window.app_settings.get_int("cover_jpeg_quality", 90, 50, 100)
        )
        return self.quality

    def _build_prompt_control(self) -> QTextEdit:
        self.prompt = QTextEdit()
        self.prompt.setPlaceholderText(
            ui_text(
                "Describe how to improve the cover, e.g. preserve the subject, "
                "improve composition and readability for short-video platforms."
            )
        )
        self.prompt.setPlainText(
            ui_text(
                "Preserve the original subject and brand details, improve "
                "composition, lighting, clarity and click appeal without adding "
                "misleading text or people."
            )
        )
        self.prompt.setMinimumHeight(130)
        return self.prompt

    def _saved_focus(self, key: str) -> float:
        try:
            return max(
                0.0,
                min(100.0, float(self.window.app_settings.get(key) or 50)),
            )
        except (TypeError, ValueError):
            return 50.0

    def _build_status_area(self, root: QVBoxLayout) -> None:
        self.status = QLabel(
            ui_text(
                "Copy or save the JPG, or use GPT Image to generate a variation."
            )
        )
        self.status.setObjectName("mutedText")
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.ai_progress = QProgressBar()
        self.ai_progress.setRange(0, 100)
        self.ai_progress.hide()
        root.addWidget(self.ai_progress)

    def _build_action_buttons(self) -> QHBoxLayout:
        buttons = QHBoxLayout()
        self.reset_button = QPushButton(ui_text("Restore Original"))
        self.reset_button.clicked.connect(self.reset_source)
        self.copy_button = QPushButton(ui_text("Copy Cover"))
        self.copy_button.clicked.connect(self.copy_cover)
        self.save_button = QPushButton(ui_text("Save JPG"))
        self.save_button.clicked.connect(self.save_cover)
        self.confirm_crop_button = QPushButton(ui_text("Confirm Crop Position"))
        self.confirm_crop_button.clicked.connect(self.confirm_crop_position)
        self.ai_button = QPushButton(ui_text("AI Variation"))
        self.ai_button.setObjectName("primaryButton")
        self.ai_button.clicked.connect(self.generate_cover)
        close_button = QPushButton(ui_text("Close"))
        close_button.clicked.connect(self.reject)
        buttons.addWidget(self.reset_button)
        buttons.addStretch(1)
        buttons.addWidget(self.copy_button)
        buttons.addWidget(self.confirm_crop_button)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.ai_button)
        buttons.addWidget(close_button)
        return buttons

    def _connect_controls(self) -> None:
        self.preset.currentIndexChanged.connect(self.options_changed)
        self.fit.currentIndexChanged.connect(self.options_changed)
        self.focus_x.valueChanged.connect(self.options_changed)
        self.focus_y.valueChanged.connect(self.options_changed)
        self.quality.valueChanged.connect(self.options_changed)
        self.preview.focus_changed.connect(self.apply_drag_focus)

    def export_options(self) -> CoverExportOptions:
        return CoverExportOptions.from_preset(
            str(self.preset.currentData()),
            quality=self.quality.value(),
            fit_mode=str(self.fit.currentData()),
            focus_x=self.focus_x.value() / 100.0,
            focus_y=self.focus_y.value() / 100.0,
        )

    def options_changed(self, *_args) -> None:
        self.update_focus_controls()
        self._options_dirty = True
        self.refresh_preview()

    def update_focus_controls(self) -> None:
        enabled = str(self.fit.currentData()) == CoverFitMode.CROP.value
        self.focus_x.setEnabled(enabled)
        self.focus_y.setEnabled(enabled)
        options = self.export_options()
        self.preview.configure_crop(
            QSize(self.current.width, self.current.height),
            QSize(options.width, options.height),
            options.focus_x,
            options.focus_y,
            enabled,
        )

    def apply_drag_focus(self, focus_x: float, focus_y: float) -> None:
        self.focus_x.blockSignals(True)
        self.focus_y.blockSignals(True)
        try:
            self.focus_x.setValue(round(focus_x * 100, 1))
            self.focus_y.setValue(round(focus_y * 100, 1))
        finally:
            self.focus_x.blockSignals(False)
            self.focus_y.blockSignals(False)
        self._options_dirty = True
        self.refresh_preview()

    def confirm_crop_position(self) -> None:
        try:
            self.window.app_settings.set_many({
                "cover_preset": str(self.preset.currentData()),
                "cover_fit_mode": str(self.fit.currentData()),
                "cover_focus_x": str(self.focus_x.value()),
                "cover_focus_y": str(self.focus_y.value()),
                "cover_jpeg_quality": str(self.quality.value()),
            })
        except Exception as exc:
            QMessageBox.warning(self, ui_text("Save Failed"), runtime_text(exc))
            return
        self._options_dirty = False
        self.status.setText(
            ui_format(
                "Crop position confirmed: horizontal {x}%, vertical {y}%",
                x=self.focus_x.value(),
                y=self.focus_y.value(),
            )
        )

    def refresh_preview(self, *_args, update_status: bool = True) -> None:
        try:
            image = self.service.render(self.current, self.export_options())
        except CoverServiceError as exc:
            if update_status:
                self.status.setText(runtime_text(exc))
            return
        pixmap = QPixmap.fromImage(image)
        self.preview.setPixmap(
            pixmap.scaled(
                self.preview.size(),
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            )
        )
        if not update_status:
            return
        source_label = (
            ui_text("AI result")
            if self.current is not self.source
            else ui_text("Original cover")
        )
        self.status.setText(
            ui_format(
                "Preview: {width}×{height} · JPG quality {quality}% · {source}",
                width=image.width(),
                height=image.height(),
                quality=self.quality.value(),
                source=source_label,
            )
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Resizing only repaints the preview. It must not erase an active AI
        # progress message or the final provider/thread error.
        self.refresh_preview(update_status=False)

    def reset_source(self) -> None:
        if not self._confirm_discard_generated(ui_text("Restore Original")):
            return
        self.current = self.source
        self._generated_dirty = False
        self.refresh_preview()

    def copy_cover(self) -> None:
        try:
            clipboard_data = self.service.prepare_clipboard(
                self.current,
                self.export_options(),
            )
            QApplication.clipboard().setMimeData(clipboard_data.to_mime_data())
            self.status.setText(
                ui_format(
                    "Copied a {width}×{height} cover to the clipboard",
                    width=clipboard_data.width,
                    height=clipboard_data.height,
                )
            )
        except CoverServiceError as exc:
            QMessageBox.warning(
                self,
                ui_text("Copy Failed"),
                runtime_text(exc),
            )

    def save_cover(self) -> None:
        preset = COVER_PRESETS[CoverPresetId(str(self.preset.currentData()))]
        default = default_cover_export_path(
            self.media,
            width=preset.width,
            height=preset.height,
        )
        target, _ = QFileDialog.getSaveFileName(
            self,
            ui_text("Save JPG Cover"),
            str(default),
            ui_text("JPG Images (*.jpg *.jpeg)"),
        )
        if not target:
            return
        try:
            target_path = normalized_jpeg_target(target)
            result = self.service.save_jpeg(
                self.current,
                target_path,
                self.export_options(),
            )
        except (CoverServiceError, OSError, ValueError) as exc:
            QMessageBox.warning(
                self,
                ui_text("Save Failed"),
                runtime_text(exc),
            )
            return
        self._generated_dirty = False
        self._options_dirty = False
        self.status.setText(
            ui_format(
                "Saved: {path} ({width}×{height}, {size})",
                path=result.path,
                width=result.width,
                height=result.height,
                size=format_file_size(result.byte_size),
            )
        )

    def generate_cover(self) -> None:
        if self.generation_thread is not None:
            return
        if not self._confirm_discard_generated(
            ui_text("Generate and replace the current preview")
        ):
            return
        prepared = self._prepare_generation_runtime()
        if prepared is None:
            return
        provider, request = prepared
        runtime = self._wire_generation_runtime(provider, request)
        if runtime is None:
            return
        thread, worker = runtime
        self._start_generation_runtime(thread, worker, provider)

    def _prepare_generation_runtime(
        self,
    ) -> tuple[OpenAICoverGenerationProvider, object] | None:
        api_key = self.window.secure_store.get("openai_api_key") or ""
        if not api_key:
            QMessageBox.information(
                self,
                ui_text("API Key Required"),
                ui_text("Save an OpenAI API Key in Settings → AI Cover first."),
            )
            return
        prompt = self.prompt.toPlainText().strip()
        if not prompt:
            QMessageBox.warning(
                self,
                ui_text("Prompt Required"),
                ui_text("Enter an AI cover prompt."),
            )
            return
        try:
            request = self.service.prepare_generation_request(
                self.current,
                prompt,
                options=self.export_options(),
            )
            provider = OpenAICoverGenerationProvider(
                api_key,
                model=(
                    self.window.app_settings.get("cover_ai_model")
                    or "gpt-image-2"
                ),
                endpoint=self.window.app_settings.get("cover_ai_api_url"),
            )
        except Exception as exc:
            QMessageBox.warning(
                self,
                ui_text("Unable to Start AI Variation"),
                runtime_text(exc),
            )
            return None
        return provider, request

    def _wire_generation_runtime(
        self,
        provider: OpenAICoverGenerationProvider,
        request: object,
    ) -> tuple[QThread, CoverGenerationWorker] | None:
        thread: QThread | None = None
        worker: CoverGenerationWorker | None = None
        try:
            thread = QThread(self)
            worker = CoverGenerationWorker(provider, request)
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.progress.connect(self.on_generation_progress, Qt.QueuedConnection)
            worker.generated.connect(self.on_generated, Qt.QueuedConnection)
            worker.failed.connect(self.on_generation_failed, Qt.QueuedConnection)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            thread.finished.connect(self.generation_finished, Qt.QueuedConnection)
        except Exception as exc:
            self._safe_close_provider(provider)
            if worker is not None and thread is not None:
                delete_unstarted_worker(worker, thread)
            elif thread is not None:
                thread.deleteLater()
            self.on_generation_failed(str(exc))
            return None
        return thread, worker

    def _start_generation_runtime(
        self,
        thread: QThread,
        worker: CoverGenerationWorker,
        provider: OpenAICoverGenerationProvider,
    ) -> None:
        # Publish ownership only after the complete signal graph exists. A
        # partially wired runtime must never make the dialog look busy forever.
        self.generation_thread = thread
        self.generation_worker = worker
        self.ai_button.setEnabled(False)
        self.ai_progress.setValue(0)
        self.ai_progress.show()
        try:
            thread.start()
        except Exception as exc:
            self.generation_thread = None
            self.generation_worker = None
            self._deferred_generation_finishes.discard(thread)
            self._safe_close_provider(provider)
            delete_unstarted_worker(worker, thread)
            self.ai_button.setEnabled(True)
            self.ai_progress.hide()
            self.on_generation_failed(str(exc))

    @staticmethod
    def _safe_close_provider(provider: object) -> None:
        try:
            provider.close()
        except Exception:
            pass

    @Slot(int, str)
    def on_generation_progress(self, value: int, message: str) -> None:
        self.ai_progress.setValue(value)
        self.status.setText(runtime_text(message))

    @Slot(object)
    def on_generated(self, generated) -> None:
        try:
            self.current = self.service.load_bytes(
                generated.image_bytes,
                source=f"{generated.provider_id}:generated",
                content_type=generated.mime_type,
            )
        except CoverServiceError as exc:
            self.on_generation_failed(str(exc))
            return
        self.refresh_preview()
        self._generated_dirty = True
        self.status.setText(
            ui_text(
                "AI cover generated. Preview it, then copy or save as JPG; "
                "the original cover is not overwritten automatically."
            )
        )

    @Slot(str)
    def on_generation_failed(self, error: str) -> None:
        self.status.setText(
            ui_format("AI variation failed: {error}", error=runtime_text(error))
        )

    @Slot()
    def generation_finished(self) -> None:
        thread = self.sender()
        if isinstance(thread, QThread):
            self._defer_generation_thread_finish(thread)

    def _defer_generation_thread_finish(self, thread: QThread) -> None:
        """Apply the final generated/error signal before restoring controls."""

        if thread in self._deferred_generation_finishes:
            return
        self._deferred_generation_finishes.add(thread)
        QTimer.singleShot(
            0,
            partial(self._complete_generation_thread_finish, thread),
        )

    def _complete_generation_thread_finish(self, thread: QThread) -> None:
        self._deferred_generation_finishes.discard(thread)
        if self.generation_thread is thread:
            self.generation_thread = None
            self.generation_worker = None
            self.ai_button.setEnabled(True)
            self.ai_progress.hide()
        try:
            thread.deleteLater()
        except RuntimeError:
            pass

    def reject(self) -> None:
        if self.generation_worker is not None:
            self.generation_worker.cancel()
            self.status.setText(ui_text("Canceling the AI cover request…"))
            return
        if (
            not self._close_confirmed
            and not self._confirm_discard_changes(ui_text("Close Cover Studio"))
        ):
            return
        super().reject()

    def closeEvent(self, event) -> None:
        if self.generation_worker is not None:
            self.generation_worker.cancel()
            self.status.setText(ui_text("Canceling the AI cover request…"))
            event.ignore()
            return
        if not self._confirm_discard_changes(ui_text("Close Cover Studio")):
            event.ignore()
            return
        self._close_confirmed = True
        try:
            super().closeEvent(event)
        finally:
            self._close_confirmed = False

    def _confirm_discard_generated(self, action: str) -> bool:
        if not self._generated_dirty:
            return True
        answer = QMessageBox.question(
            self,
            ui_text("AI Cover Not Saved"),
            ui_format(
                "The current AI cover or crop adjustments have not been "
                "saved.\n\nContinue with {action}?",
                action=action,
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return answer == QMessageBox.Yes

    def _confirm_discard_changes(self, action: str) -> bool:
        if not self._generated_dirty and not self._options_dirty:
            return True
        answer = QMessageBox.question(
            self,
            ui_text("Cover Changes Not Saved"),
            ui_format(
                "The current cover or crop adjustments have not been saved.\n\n"
                "Continue with {action}?",
                action=action,
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return answer == QMessageBox.Yes
