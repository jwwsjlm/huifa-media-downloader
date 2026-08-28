from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from PySide6.QtCore import QObject, Signal, Slot

from app.core.disk_capacity import (
    CapacityEstimate,
    DiskCapacityError,
    DiskCapacityErrorCode,
    DiskReservationManager,
)
from app.core.disk_capacity_lease import DiskReservationLease
from app.core.download_progress import bounded_percent
from app.core.media_validation import (
    MediaValidationError,
    MediaValidationErrorCode,
    validate_media_file,
)
from app.core.processing_workspace import (
    cleanup_processing_workspace,
    processing_temp_workspace,
    same_storage_volume,
)
from app.core.media_probe import (
    VideoStreamInfo,
    probe_video_stream,
    validate_transcode_topology,
    video_stream_info_from_probe_payload,
)
from app.core.transcode_service import (
    PreparedTranscode,
    PublishedTranscode,
    normalize_transcode_encoder,
    prepare_transcode_media,
    transcode_encoder_codec,
    transcode_encoder_device,
)


_PROGRESS_EMIT_INTERVAL_SECONDS = 0.15


def transcode_capacity_estimate(path: str | Path) -> CapacityEstimate:
    """Reserve source-sized temporary output plus a conservative margin."""
    size = max(1, Path(path).stat().st_size)
    margin = max(256 * 1024 * 1024, size // 4)
    temporary_bytes = size + margin
    return CapacityEstimate(
        known=True,
        download_bytes=0,
        final_bytes=temporary_bytes,
        peak_bytes=temporary_bytes,
        margin_bytes=margin,
        entry_count=1,
        merge_entry_count=0,
        sources=("transcode-source-size",),
    )


@dataclass(slots=True)
class _ConversionRunState:
    prepared: PreparedTranscode | None = None
    published: PublishedTranscode | None = None
    workspace: Path | None = None


class CompletedMediaTranscodeWorker(QObject):
    """Prepare, validate and atomically publish one completed-media conversion."""

    progress = Signal(str, object)
    completed = Signal(str, object)
    skipped = Signal(str, object)
    failed = Signal(str, str)
    canceled = Signal(str)
    finished = Signal()

    def __init__(
        self,
        task_id: str,
        media_path: str,
        ffmpeg_path: str,
        ffprobe_path: str,
        encoder: str,
        disk_capacity_manager: DiskReservationManager | None = None,
        disk_lease: DiskReservationLease | None = None,
        processing_temp_dir: str = "",
        output_container: str = "auto",
    ):
        super().__init__()
        self.task_id = str(task_id)
        self.media_path = str(media_path)
        self.ffmpeg_path = str(ffmpeg_path)
        self.ffprobe_path = str(ffprobe_path)
        self.encoder = normalize_transcode_encoder(encoder)
        manager = disk_capacity_manager or (
            disk_lease.manager if disk_lease is not None else DiskReservationManager()
        )
        self.disk_capacity_manager = manager
        self._disk_lease = disk_lease or DiskReservationLease(manager)
        self.processing_temp_dir = str(processing_temp_dir or "").strip()
        self.output_container = str(output_container or "auto").strip()
        self._cancel = threading.Event()
        self._last_progress_emit_at = 0.0
        self._last_progress_stage = ""

    @Slot()
    def cancel(self) -> None:
        self._cancel.set()

    def _progress(self, stage: str, text: str, percent: float, **details: Any) -> None:
        normalized_percent = bounded_percent(percent)
        now = time.monotonic()
        stage_changed = stage != self._last_progress_stage
        if (
            not stage_changed
            and normalized_percent < 100.0
            and self._last_progress_emit_at
            and now - self._last_progress_emit_at < _PROGRESS_EMIT_INTERVAL_SECONDS
        ):
            return
        self._last_progress_emit_at = now
        self._last_progress_stage = stage
        payload = {
            "stage": stage,
            "stage_text": text,
            "stage_progress": normalized_percent,
        }
        payload.update(details)
        self.progress.emit(self.task_id, payload)

    @contextmanager
    def _conversion_workspace(self) -> Iterator[Path | None]:
        """Own the temporary directory and both conversion reservations."""
        workspace = processing_temp_workspace(
            self.processing_temp_dir,
            self.task_id,
            "manual-transcode",
        )
        estimate = transcode_capacity_estimate(self.media_path)
        temp_capacity_target = workspace or Path(self.media_path)
        reservation_keys: list[str] = []
        try:
            temporary_key = f"manual-transcode\x1f{self.task_id}\x1ftemp"
            self._disk_lease.acquire(
                temporary_key,
                temp_capacity_target,
                estimate,
                cancel_event=self._cancel,
            )
            reservation_keys.append(temporary_key)
            if not same_storage_volume(temp_capacity_target, self.media_path):
                final_key = f"manual-transcode\x1f{self.task_id}\x1ffinal"
                self._disk_lease.acquire(
                    final_key,
                    self.media_path,
                    estimate,
                    cancel_event=self._cancel,
                )
                reservation_keys.append(final_key)
            yield workspace
        finally:
            if reservation_keys:
                try:
                    self._disk_lease.release_keys(reservation_keys)
                except Exception:
                    # Service-level cleanup retains the same lease as fallback.
                    pass

    @staticmethod
    def _discard_prepared(prepared: PreparedTranscode | None) -> None:
        if prepared is None:
            return
        try:
            prepared.discard()
        except Exception:
            pass

    @staticmethod
    def _rollback_published(published: PublishedTranscode | None) -> None:
        if published is None:
            return
        try:
            published.rollback()
        except Exception:
            pass

    def _raise_if_canceled(self) -> None:
        if self._cancel.is_set():
            raise InterruptedError("用户取消格式转换")

    def _is_cancellation_error(self, error: Exception) -> bool:
        if self._cancel.is_set() or isinstance(error, InterruptedError):
            return True
        if isinstance(error, DiskCapacityError):
            return error.code == DiskCapacityErrorCode.CANCELLED
        if isinstance(error, MediaValidationError):
            return error.code == MediaValidationErrorCode.CANCELLED
        return False

    def _report_transcode_progress(self, percent: float, encoder: str) -> None:
        self._progress(
            "transcoding",
            f"正在转换视频格式 · {encoder}",
            percent,
            transcode_encoder=encoder,
        )

    def _execute_conversion(
        self,
        target_codec: str,
        source_info: VideoStreamInfo,
        state: _ConversionRunState,
    ) -> None:
        self._progress(
            "transcoding",
            f"正在转换视频格式 · {self.encoder}",
            0,
            transcode_encoder=self.encoder,
        )
        self._progress("waiting_disk", "正在为格式转换预留磁盘空间", 0)
        with self._conversion_workspace() as workspace:
            state.workspace = workspace
            state.prepared = prepare_transcode_media(
                self.media_path,
                self.ffmpeg_path,
                target_codec,
                transcode_encoder_device(self.encoder),
                encoder=self.encoder,
                duration_seconds=source_info.duration_seconds,
                cancel_event=self._cancel,
                progress=self._report_transcode_progress,
                preserve_source=False,
                source_info=source_info,
                temporary_dir=str(workspace or ""),
                output_container=self.output_container,
            )
            self._progress("verifying", "正在校验转换后的媒体文件", 0)
            validation = validate_media_file(
                state.prepared.temporary_path,
                self.ffprobe_path,
                require_video=True,
                require_audio=False,
                cancel_event=self._cancel,
            )
            converted_info = (
                video_stream_info_from_probe_payload(validation.probe_payload)
                if validation.probe_payload
                else probe_video_stream(
                    state.prepared.temporary_path,
                    self.ffprobe_path,
                )
            )
            validate_transcode_topology(source_info, converted_info)
            self._raise_if_canceled()
            state.published = state.prepared.commit()
            self._raise_if_canceled()

    def _cleanup_run_state(self, state: _ConversionRunState) -> None:
        self._rollback_published(state.published)
        self._discard_prepared(state.prepared)
        try:
            self._disk_lease.release_all()
        except Exception:
            pass
        try:
            cleanup_processing_workspace(state.workspace)
        except Exception:
            pass

    @Slot()
    def run(self) -> None:
        state = _ConversionRunState()
        try:
            self._raise_if_canceled()
            target_codec = transcode_encoder_codec(self.encoder)
            if target_codec == "original":
                self.skipped.emit(self.task_id, {
                    "reason": "keep_original",
                    "codec": "original",
                    "encoder": self.encoder,
                })
                return
            self._progress("verifying", "正在读取当前视频格式", 0)
            info = probe_video_stream(self.media_path, self.ffprobe_path)
            self._raise_if_canceled()
            if info.codec == target_codec:
                self.skipped.emit(self.task_id, {
                    "reason": "already_target",
                    "codec": target_codec,
                    "encoder": self.encoder,
                })
                return
            self._execute_conversion(target_codec, info, state)
            published = state.published
            if published is None:
                raise RuntimeError("格式转换完成但未生成可提交文件")
            self.completed.emit(self.task_id, {
                "old_path": self.media_path,
                "new_path": str(published.final_path),
                "encoder": published.encoder,
                "codec": target_codec,
                "device": transcode_encoder_device(published.encoder),
                "publication": published,
            })
            # DownloadService now owns publication finalization or rollback.
            state.published = None
            state.prepared = None
        except Exception as exc:
            if self._is_cancellation_error(exc):
                self.canceled.emit(self.task_id)
            else:
                self.failed.emit(self.task_id, str(exc))
        finally:
            try:
                self._cleanup_run_state(state)
            finally:
                self.finished.emit()
