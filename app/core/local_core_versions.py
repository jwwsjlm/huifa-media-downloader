from __future__ import annotations

import threading
from collections.abc import Callable

from PySide6.QtCore import QObject, Signal, Slot

from app.core.transcode_service import compiled_transcode_encoders
from app.core.update_service import installed_component_details, run_disposable_jobs


ComponentDetails = tuple[str, str, str]
ComponentCheck = tuple[str, str, str]


class LocalCoreVersionWorker(QObject):
    """Probe local download runtimes without blocking the UI thread."""

    completed = Signal(object)

    def __init__(
        self,
        deno_path: str = "",
        ffmpeg_path: str = "",
        ffprobe_path: str = "",
    ) -> None:
        super().__init__()
        self.deno_path = str(deno_path or "").strip()
        self.ffmpeg_path = str(ffmpeg_path or "").strip()
        self.ffprobe_path = str(ffprobe_path or "").strip()
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    @staticmethod
    def _checks(
        deno_path: str,
        ffmpeg_path: str,
        ffprobe_path: str,
    ) -> tuple[ComponentCheck, ...]:
        return (
            ("yt-dlp", "", ""),
            ("yt-dlp-ejs", "", ""),
            ("Deno", deno_path, ""),
            ("FFmpeg", ffmpeg_path, ""),
            ("FFprobe", ffmpeg_path, ffprobe_path),
        )

    @staticmethod
    def _probe(check: ComponentCheck) -> tuple[str, ComponentDetails]:
        name, configured_path, configured_ffprobe_path = check
        try:
            if configured_ffprobe_path:
                details = installed_component_details(
                    name,
                    configured_path,
                    configured_ffprobe_path,
                )
            else:
                details = installed_component_details(name, configured_path)
            normalized = tuple(str(value or "") for value in details)
        except Exception as exc:
            normalized = ("检测失败", str(exc), "")
        return name, normalized

    @Slot()
    def run(self) -> None:
        results: dict[str, object] = {}
        checks = self._checks(self.deno_path, self.ffmpeg_path, self.ffprobe_path)

        def collect(_index: int, value: tuple[str, ComponentDetails]) -> None:
            name, details = value
            results[name] = details

        orchestration_error = ""
        try:
            jobs: list[Callable[[], tuple[str, ComponentDetails]]] = [
                lambda check=check: self._probe(check)
                for check in checks
            ]
            run_disposable_jobs(
                jobs,
                max_workers=len(checks),
                cancel_event=self._cancelled,
                thread_name_prefix="local-core-version",
                on_result=collect,
            )
        except InterruptedError:
            pass
        except Exception as exc:
            orchestration_error = str(exc)
            for name, _configured_path, _configured_ffprobe_path in checks:
                results.setdefault(name, ("检测失败", orchestration_error, ""))

        if not self._cancelled.is_set() and not orchestration_error:
            ffmpeg_details = results.get("FFmpeg")
            ffmpeg_runtime = (
                str(ffmpeg_details[2] or "")
                if isinstance(ffmpeg_details, tuple) and len(ffmpeg_details) >= 3
                else ""
            )
            try:
                # Startup lists compiled encoders; real transcoding verifies the selected encoder.
                encoders = compiled_transcode_encoders(
                    ffmpeg_runtime,
                    self._cancelled,
                )
                results["__video_encoders__"] = {"items": encoders, "error": ""}
            except InterruptedError:
                pass
            except Exception as exc:
                results["__video_encoders__"] = {"items": (), "error": str(exc)}
        elif orchestration_error:
            results["__video_encoders__"] = {
                "items": (),
                "error": orchestration_error,
            }

        self.completed.emit(results)
