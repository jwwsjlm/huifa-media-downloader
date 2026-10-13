from __future__ import annotations

import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QObject, Property, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QGuiApplication

from app.adapters.openai_cover_provider import OpenAICoverGenerationProvider
from app.core.cover_service import (
    COVER_PRESETS,
    CoverExportOptions,
    CoverFitMode,
    CoverPresetId,
    CoverServiceError,
    LoadedCover,
)
from app.core.paths import data_dir
from app.core.qt_lifecycle import delete_unstarted_worker
from app.core.cover_paths import default_cover_export_path, normalized_jpeg_target


class _GenerationWorker(QObject):
    progress = Signal(int, str)
    generated = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(self, provider: OpenAICoverGenerationProvider, request: object) -> None:
        super().__init__()
        self._provider = provider
        self._request = request
        self._cancel = threading.Event()

    @Slot()
    def run(self) -> None:
        try:
            generated = self._provider.generate(
                self._request,
                cancel_event=self._cancel,
                progress=lambda value, message: self.progress.emit(value, message),
            )
            if self._cancel.is_set():
                self.failed.emit("AI 封面创作已取消")
            elif generated:
                self.generated.emit(generated[0])
            else:
                self.failed.emit("AI 图像服务没有返回封面")
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            try:
                self._provider.close()
            except Exception:
                pass
            self.finished.emit()

    def cancel(self) -> None:
        self._cancel.set()


class CoverStudioModel(QObject):
    """QML-facing editor over the existing safe cover transformation service."""

    changed = Signal()

    def __init__(self, runtime: Any, settings_model: Any, parent=None) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._settings_model = settings_model
        self._media = None
        self._source: LoadedCover | None = None
        self._current: LoadedCover | None = None
        self._preset = CoverPresetId.LANDSCAPE_16_9.value
        self._fit_mode = CoverFitMode.CROP.value
        self._focus_x = 50.0
        self._focus_y = 50.0
        self._quality = 90
        self._status = ""
        self._preview_url = QUrl()
        self._preview_paths: list[Path] = []
        self._preview_session = uuid4().hex
        self._revision = 0
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(40)
        self._preview_timer.timeout.connect(self._render_preview)
        self._dirty = False
        self._thread: QThread | None = None
        self._worker: _GenerationWorker | None = None
        self._deferred_threads: set[QThread] = set()

    @Property(bool, notify=changed)
    def active(self) -> bool:
        return self._media is not None and self._current is not None

    @Property(bool, notify=changed)
    def busy(self) -> bool:
        return self._worker is not None

    @Property(bool, notify=changed)
    def dirty(self) -> bool:
        return self._dirty

    @Property(str, notify=changed)
    def title(self) -> str:
        return str(getattr(self._media, "title", "") or "")

    @Property(QUrl, notify=changed)
    def previewUrl(self) -> QUrl:
        return self._preview_url

    @Property(str, notify=changed)
    def status(self) -> str:
        return self._status

    @Property(str, notify=changed)
    def preset(self) -> str:
        return self._preset

    @Property(str, notify=changed)
    def fitMode(self) -> str:
        return self._fit_mode

    @Property(float, notify=changed)
    def focusX(self) -> float:
        return self._focus_x

    @Property(float, notify=changed)
    def focusY(self) -> float:
        return self._focus_y

    @Property(int, notify=changed)
    def quality(self) -> int:
        return self._quality

    @Property(str, notify=changed)
    def defaultOutputPath(self) -> str:
        if self._media is None:
            return ""
        options = self._options()
        return str(
            default_cover_export_path(
                self._media,
                width=options.width,
                height=options.height,
            )
        )

    @Property("QVariantList", constant=True)
    def presets(self) -> list[dict[str, object]]:
        return [
            {
                "id": preset_id.value,
                "label": preset.label,
                "width": preset.width,
                "height": preset.height,
            }
            for preset_id, preset in COVER_PRESETS.items()
        ]

    @Slot(int, result=bool)
    def openMedia(self, media_id: int) -> bool:
        if self.busy:
            self._set_status("AI 封面创作仍在进行，请先取消或等待完成")
            return False
        media = self._runtime.db.get_media(int(media_id or 0))
        if media is None:
            self._set_status("找不到已完成媒体")
            return False
        source_path = Path(str(media.thumbnail_path or ""))
        if not source_path.is_file():
            self._set_status("当前视频没有可用的本地封面图")
            return False
        try:
            source = self._runtime.cover_service.load_local(source_path)
        except CoverServiceError as exc:
            self._set_status(str(exc))
            return False
        self._clear_preview_files()
        self._media = media
        self._source = source
        self._current = source
        settings = self._runtime.app_settings
        self._preset = self._valid_preset(settings.get("cover_preset"))
        self._fit_mode = self._valid_fit_mode(settings.get("cover_fit_mode"))
        self._focus_x = self._bounded_focus(settings.get("cover_focus_x"))
        self._focus_y = self._bounded_focus(settings.get("cover_focus_y"))
        self._quality = self._bounded_quality(settings.get("cover_jpeg_quality"))
        self._dirty = False
        self._set_status("拖动预览可调整裁切焦点；原始封面不会被自动覆盖")
        self._render_preview()
        return self.active

    @Slot()
    def close(self) -> None:
        self._preview_timer.stop()
        self.cancelGeneration()
        self._media = None
        self._source = None
        self._current = None
        self._preview_url = QUrl()
        self._dirty = False
        self._clear_preview_files()
        self.changed.emit()

    @Slot(str)
    def setPreset(self, value: str) -> None:
        self._change_options(preset=self._valid_preset(value))

    @Slot(str)
    def setFitMode(self, value: str) -> None:
        self._change_options(fit_mode=self._valid_fit_mode(value))

    @Slot(float)
    def setFocusX(self, value: float) -> None:
        self._change_options(focus_x=self._bounded_focus(value))

    @Slot(float)
    def setFocusY(self, value: float) -> None:
        self._change_options(focus_y=self._bounded_focus(value))

    @Slot(int)
    def setQuality(self, value: int) -> None:
        self._change_options(quality=self._bounded_quality(value))

    @Slot()
    def saveOptions(self) -> None:
        if not self.active:
            return
        try:
            self._runtime.app_settings.set_many({
                "cover_preset": self._preset,
                "cover_fit_mode": self._fit_mode,
                "cover_focus_x": str(round(self._focus_x, 1)),
                "cover_focus_y": str(round(self._focus_y, 1)),
                "cover_jpeg_quality": str(self._quality),
            })
            self._runtime.download_service.configure_runtime_settings(
                cover_jpeg_quality=self._quality,
            )
            self._settings_model.refresh()
        except Exception as exc:
            self._set_status(f"无法保存封面设置：{exc}")
            return
        self._dirty = False
        self._set_status("裁切位置和 JPG 质量已保存")

    @Slot()
    def restoreOriginal(self) -> None:
        if self.busy or self._source is None:
            return
        self._current = self._source
        self._dirty = True
        self._set_status("已恢复原始封面预览")
        self._render_preview()

    @Slot(result=bool)
    def copyCover(self) -> bool:
        current = self._current
        if current is None:
            return False
        try:
            payload = self._runtime.cover_service.prepare_clipboard(
                current,
                self._options(),
            )
            QGuiApplication.clipboard().setMimeData(payload.to_mime_data())
        except CoverServiceError as exc:
            self._set_status(f"复制封面失败：{exc}")
            return False
        self._set_status(f"已复制 {payload.width}×{payload.height} 封面到剪贴板")
        return True

    @Slot(str, result=str)
    def saveJpeg(self, target: str) -> str:
        current = self._current
        if current is None:
            return ""
        requested = str(target or "").strip() or self.defaultOutputPath
        try:
            result = self._runtime.cover_service.save_jpeg(
                current,
                normalized_jpeg_target(requested),
                self._options(),
            )
        except (CoverServiceError, OSError, ValueError) as exc:
            self._set_status(f"保存封面失败：{exc}")
            return ""
        self._dirty = False
        self._set_status(
            f"已保存：{result.path}（{result.width}×{result.height}，{result.byte_size} 字节）"
        )
        return str(result.path)

    @Slot(str, result=bool)
    def generate(self, prompt: str) -> bool:
        if self.busy:
            return False
        current = self._current
        if current is None:
            return False
        api_key = str(self._runtime.secure_store.get("openai_api_key") or "").strip()
        if not api_key:
            self._set_status("请先在设置中保存 OpenAI API 密钥")
            return False
        prompt = str(prompt or "").strip()
        if not prompt:
            self._set_status("请输入 AI 封面提示词")
            return False
        provider: OpenAICoverGenerationProvider | None = None
        thread: QThread | None = None
        worker: _GenerationWorker | None = None
        try:
            request = self._runtime.cover_service.prepare_generation_request(
                current,
                prompt,
                options=self._options(),
            )
            provider = OpenAICoverGenerationProvider(
                api_key,
                model=self._runtime.app_settings.get("cover_ai_model") or "gpt-image-2",
                endpoint=self._runtime.app_settings.get("cover_ai_api_url"),
            )
            thread = QThread(self)
            worker = _GenerationWorker(provider, request)
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.progress.connect(self._generation_progress)
            worker.generated.connect(self._generated)
            worker.failed.connect(self._generation_failed)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            thread.finished.connect(self._generation_finished)
        except Exception as exc:
            if provider is not None:
                try:
                    provider.close()
                except Exception:
                    pass
            if worker is not None and thread is not None:
                delete_unstarted_worker(worker, thread)
            elif thread is not None:
                thread.deleteLater()
            self._set_status(f"无法启动 AI 封面创作：{exc}")
            return False
        if thread is None or worker is None or provider is None:
            self._set_status("无法启动 AI 封面创作")
            return False
        self._thread = thread
        self._worker = worker
        self._set_status("正在准备 AI 封面创作…")
        try:
            thread.start()
        except Exception as exc:
            self._thread = None
            self._worker = None
            try:
                provider.close()
            except Exception:
                pass
            delete_unstarted_worker(worker, thread)
            self._set_status(f"无法启动 AI 封面创作：{exc}")
            return False
        self.changed.emit()
        return True

    @Slot()
    def cancelGeneration(self) -> None:
        worker = self._worker
        if worker is None:
            return
        try:
            worker.cancel()
        except RuntimeError:
            return
        self._set_status("正在取消 AI 封面创作…")

    @Slot()
    def requestShutdown(self) -> None:
        self.cancelGeneration()

    def _change_options(
        self,
        *,
        preset: str | None = None,
        fit_mode: str | None = None,
        focus_x: float | None = None,
        focus_y: float | None = None,
        quality: int | None = None,
    ) -> None:
        if not self.active or self.busy:
            return
        changed = False
        for name, value in (
            ("_preset", preset),
            ("_fit_mode", fit_mode),
            ("_focus_x", focus_x),
            ("_focus_y", focus_y),
            ("_quality", quality),
        ):
            if value is not None and getattr(self, name) != value:
                setattr(self, name, value)
                changed = True
        if not changed:
            return
        self._dirty = True
        self.changed.emit()
        self._preview_timer.start()

    def _options(self) -> CoverExportOptions:
        return CoverExportOptions.from_preset(
            self._preset,
            quality=self._quality,
            fit_mode=self._fit_mode,
            focus_x=self._focus_x / 100.0,
            focus_y=self._focus_y / 100.0,
        )

    def _render_preview(self) -> None:
        current = self._current
        if current is None:
            return
        try:
            image = self._runtime.cover_service.render(current, self._options())
            self._revision += 1
            folder = data_dir() / "temp"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"cover-preview-{self._preview_session}-{self._revision}.png"
            if not image.save(str(path), "PNG"):
                raise CoverServiceError("无法写入封面预览")
        except (CoverServiceError, OSError, ValueError) as exc:
            self._set_status(f"无法生成封面预览：{exc}")
            return
        previous_paths = self._preview_paths
        self._preview_paths = [path]
        self._preview_url = QUrl.fromLocalFile(str(path))
        self._status = f"预览：{image.width()}×{image.height()} · JPG 质量 {self._quality}%"
        for previous_path in previous_paths:
            try:
                previous_path.unlink(missing_ok=True)
            except OSError:
                self._preview_paths.append(previous_path)
        self.changed.emit()

    @Slot(int, str)
    def _generation_progress(self, value: int, message: str) -> None:
        if self.active:
            self._set_status(f"{max(0, min(100, int(value)))}% · {message}")

    @Slot(object)
    def _generated(self, generated: object) -> None:
        if not self.active:
            return
        try:
            image_bytes = getattr(generated, "image_bytes")
            mime_type = getattr(generated, "mime_type")
            provider_id = getattr(generated, "provider_id")
            self._current = self._runtime.cover_service.load_bytes(
                image_bytes,
                source=f"{provider_id}:generated",
                content_type=mime_type,
            )
        except (CoverServiceError, AttributeError, TypeError) as exc:
            self._set_status(f"无法读取 AI 生成封面：{exc}")
            return
        self._dirty = True
        self._render_preview()
        self._set_status("AI 封面已生成；请确认后复制或保存为 JPG")

    @Slot(str)
    def _generation_failed(self, error: str) -> None:
        if self.active:
            self._set_status(f"AI 封面创作失败：{error or '未知错误'}")

    @Slot()
    def _generation_finished(self) -> None:
        thread = self.sender()
        if not isinstance(thread, QThread) or thread in self._deferred_threads:
            return
        self._deferred_threads.add(thread)
        QTimer.singleShot(0, lambda: self._complete_generation_finish(thread))

    def _complete_generation_finish(self, thread: QThread) -> None:
        self._deferred_threads.discard(thread)
        if self._thread is thread:
            self._thread = None
            self._worker = None
            self.changed.emit()
        try:
            thread.deleteLater()
        except RuntimeError:
            pass

    @staticmethod
    def _valid_preset(value: object) -> str:
        try:
            return CoverPresetId(str(value or "")).value
        except ValueError:
            return CoverPresetId.LANDSCAPE_16_9.value

    @staticmethod
    def _valid_fit_mode(value: object) -> str:
        try:
            return CoverFitMode(str(value or "")).value
        except ValueError:
            return CoverFitMode.CROP.value

    @staticmethod
    def _bounded_focus(value: object) -> float:
        try:
            return max(0.0, min(100.0, float(value)))
        except (TypeError, ValueError):
            return 50.0

    @staticmethod
    def _bounded_quality(value: object) -> int:
        try:
            return max(50, min(100, int(float(value))))
        except (TypeError, ValueError):
            return 90

    def _set_status(self, value: str) -> None:
        value = str(value or "")
        if value == self._status:
            return
        self._status = value
        self.changed.emit()

    def _clear_preview_files(self) -> None:
        for path in self._preview_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        self._preview_paths = []
