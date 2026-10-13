from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from PySide6.QtCore import Property, QObject, Signal, Slot

from app.adapters.openai_cover_provider import (
    OpenAICoverGenerationError,
    normalize_openai_image_endpoint,
)
from app.core.cookie_sources import (
    COOKIE_SOURCE_FILE,
    normalize_cookie_browser,
    normalize_cookie_source,
)
from app.core.download_options import DownloadOptions
from app.core.download_performance import (
    effective_download_performance,
    normalize_download_performance_mode,
)
from app.core.download_service import validate_filename_template
from app.core.github_mirrors import normalize_github_route, parse_custom_mirror_urls
from app.core.paths import resolve_portable_path
from app.core.subtitles import normalize_subtitle_language
from app.core.transcode_service import (
    normalize_transcode_encoder,
    transcode_encoder_codec,
    transcode_encoder_device,
)
from app.core.update_service import normalize_ffmpeg_build_channel
from app.core.ytdlp_core_selection import normalize_ytdlp_core_mode
from app.core.ytdlp_ejs import normalize_ytdlp_ejs_source


class SettingsModel(QObject):
    """Keep QML settings data and runtime service configuration in one place."""

    changed = Signal()
    secretChanged = Signal()
    message = Signal(str)
    downloadDirectorySaved = Signal(str)

    def __init__(self, runtime: Any, parent=None) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._values: dict[str, str] = {}
        self._download_options: dict[str, Any] = {}
        self.refresh()

    @Property("QVariantMap", notify=changed)
    def values(self) -> dict[str, str]:
        return dict(self._values)

    @Property("QVariantMap", notify=changed)
    def downloadOptions(self) -> dict[str, Any]:
        return dict(self._download_options)

    @Property(bool, notify=secretChanged)
    def hasOpenAiKey(self) -> bool:
        return bool(self._runtime.secure_store.get("openai_api_key"))

    @Slot()
    def refresh(self) -> None:
        settings = self._runtime.app_settings
        self._values = {
            key: settings.get(key)
            for key in settings.defaults
        }
        try:
            raw_options = json.loads(self._values.get("download_options_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            raw_options = {}
        self._download_options = DownloadOptions.from_mapping(
            raw_options if isinstance(raw_options, dict) else {}
        ).to_dict()
        self.changed.emit()

    @Slot("QVariantMap", "QVariantMap", str, result=bool)
    def save(
        self,
        values: dict[str, Any],
        download_options: dict[str, Any],
        openai_key: str = "",
    ) -> bool:
        settings = self._runtime.app_settings
        previous_download_dir = str(self._values.get("download_dir") or "")
        requested = values if isinstance(values, dict) else {}
        option_values = download_options if isinstance(download_options, dict) else {}
        merged = dict(self._values)
        merged.update({
            key: self._string(value)
            for key, value in requested.items()
            if key in settings.defaults and key != "download_options_json"
        })
        secret = str(openai_key or "").strip()
        previous_secret: str | None = None
        secret_updated = False
        try:
            self._validate(merged)
            normalized_options = self._normalize_options(option_values, merged)
            merged["download_options_json"] = json.dumps(
                normalized_options,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if secret:
                previous_secret = self._runtime.secure_store.get("openai_api_key")
                self._runtime.secure_store.set("openai_api_key", secret)
                secret_updated = True
            settings.set_many(merged)
        except Exception as exc:
            if secret_updated:
                try:
                    if previous_secret is None:
                        self._runtime.secure_store.delete("openai_api_key")
                    else:
                        self._runtime.secure_store.set("openai_api_key", previous_secret)
                except Exception:
                    pass
            self.message.emit(f"保存设置失败：{exc}")
            return False
        warning = ""
        try:
            self._apply_runtime_settings()
        except Exception as exc:
            warning = f"设置已保存，但部分设置无法立即生效：{exc}"
        if secret_updated:
            self.secretChanged.emit()
        self.refresh()
        self.message.emit(warning or "设置已保存")
        if str(merged.get("download_dir") or "") != previous_download_dir:
            self.downloadDirectorySaved.emit(str(merged.get("download_dir") or ""))
        return True

    @Slot(result=bool)
    def clearOpenAiKey(self) -> bool:
        try:
            self._runtime.secure_store.delete("openai_api_key")
        except Exception as exc:
            self.message.emit(f"无法清除 API 密钥：{exc}")
            return False
        self.secretChanged.emit()
        self.message.emit("已清除 OpenAI API 密钥")
        return True

    def _validate(self, values: dict[str, str]) -> None:
        download_dir = values.get("download_dir", "").strip()
        if not download_dir:
            raise ValueError("下载目录不能为空")
        self._validate_directory(download_dir, "下载目录")
        temporary_dir = values.get("processing_temp_dir", "").strip()
        if temporary_dir:
            self._validate_directory(temporary_dir, "处理临时目录")
        values["filename_template"] = validate_filename_template(
            values.get("filename_template", "")
        )
        proxy = values.get("proxy", "").strip()
        if proxy:
            parsed = urlparse(proxy)
            if parsed.scheme.lower() not in {"http", "https", "socks4", "socks5", "socks5h"} or not parsed.netloc:
                raise ValueError("代理地址必须是完整的 HTTP、HTTPS 或 SOCKS 地址")
        source = normalize_cookie_source(values.get("download_cookie_source"))
        values["download_cookie_source"] = source
        values["download_cookie_browser"] = normalize_cookie_browser(
            values.get("download_cookie_browser")
        )
        if source == COOKIE_SOURCE_FILE:
            cookie_file = values.get("download_cookie_file", "").strip()
            path = resolve_portable_path(cookie_file) if cookie_file else None
            if path is None or not path.is_file():
                raise ValueError("请选择有效的 Netscape Cookie 文件")
        values["download_performance_mode"] = normalize_download_performance_mode(
            values.get("download_performance_mode")
        )
        values["max_concurrent"] = str(self._bounded_int(values.get("max_concurrent"), 3, 1, 8))
        values["fragment_concurrent"] = str(self._bounded_int(values.get("fragment_concurrent"), 12, 1, 32))
        values["request_delay"] = str(self._bounded_float(values.get("request_delay"), 0, 0, 60))
        values["transcode_encoder"] = normalize_transcode_encoder(
            values.get("transcode_encoder")
        )
        values["transcode_codec"] = transcode_encoder_codec(values["transcode_encoder"])
        values["transcode_device"] = transcode_encoder_device(values["transcode_encoder"])
        values["subtitle_language"] = normalize_subtitle_language(
            values.get("subtitle_language")
        )
        values["ffmpeg_build_channel"] = normalize_ffmpeg_build_channel(
            values.get("ffmpeg_build_channel")
        )
        values["ytdlp_core_mode"] = normalize_ytdlp_core_mode(values.get("ytdlp_core_mode"))
        values["ytdlp_ejs_source"] = normalize_ytdlp_ejs_source(values.get("ytdlp_ejs_source"))
        values["github_download_route"] = normalize_github_route(
            values.get("github_download_route")
        )
        values["github_mirror_urls"] = "\n".join(
            parse_custom_mirror_urls(values.get("github_mirror_urls"))
        )
        values["cover_focus_x"] = str(self._bounded_int(values.get("cover_focus_x"), 50, 0, 100))
        values["cover_focus_y"] = str(self._bounded_int(values.get("cover_focus_y"), 50, 0, 100))
        values["cover_jpeg_quality"] = str(self._bounded_int(values.get("cover_jpeg_quality"), 90, 50, 100))
        values["prepend_cover_frames"] = str(self._bounded_int(values.get("prepend_cover_frames"), 3, 1, 300))
        values["cover_ai_model"] = values.get("cover_ai_model", "").strip() or "gpt-image-2"
        api_url = values.get("cover_ai_api_url", "").strip()
        try:
            values["cover_ai_api_url"] = normalize_openai_image_endpoint(api_url) if api_url else ""
        except OpenAICoverGenerationError as exc:
            raise ValueError(str(exc)) from exc

    def _normalize_options(
        self,
        requested: dict[str, Any],
        values: dict[str, str],
    ) -> dict[str, Any]:
        source = dict(self._download_options)
        source.update(requested)
        source["processing_temp_dir"] = values.get("processing_temp_dir", "")
        source["organize_task_folder"] = self._bool(values.get("organize_task_folder"))
        return DownloadOptions.from_mapping(source).to_dict()

    def _apply_runtime_settings(self) -> None:
        runtime = self._runtime
        settings = runtime.app_settings
        tasks, fragments, delay = effective_download_performance(settings)
        runtime.download_service.configure_performance(
            max_concurrent=tasks,
            fragment_concurrent=fragments,
            request_delay=delay,
        )
        runtime.download_service.configure_runtime_settings(
            ytdlp_core_mode=settings.get("ytdlp_core_mode"),
            deno_path=settings.get("deno_path"),
            ffprobe_path=settings.get("ffprobe_path"),
            ytdlp_ejs_source=settings.get("ytdlp_ejs_source"),
            cover_convert_jpeg=settings.get_bool(
                "download_cover_convert_jpeg",
                False,
            ),
            cover_jpeg_quality=settings.get_int(
                "cover_jpeg_quality",
                90,
                50,
                100,
            ),
        )
        runtime.update_service.set_tool_overrides({
            "ffmpeg": settings.get("ffmpeg_path"),
            "ffprobe": settings.get("ffprobe_path"),
            "deno": settings.get("deno_path"),
        })
        runtime.update_service.set_ffmpeg_build_channel(
            settings.get("ffmpeg_build_channel")
        )
        runtime.update_service.set_download_routes(
            settings.get("github_download_route"),
            settings.get("github_mirror_urls"),
            settings.get("github_route_profiles"),
        )

    @staticmethod
    def _validate_directory(raw_value: str, label: str) -> None:
        path = resolve_portable_path(raw_value)
        if path.exists() and not path.is_dir():
            raise ValueError(f"{label}不是文件夹：{raw_value}")
        path.mkdir(parents=True, exist_ok=True)
        try:
            handle, probe = tempfile.mkstemp(prefix=".huifa-write-test-", dir=path)
            os.close(handle)
            Path(probe).unlink()
        except OSError as exc:
            raise ValueError(f"{label}不可写：{raw_value}（{exc}）") from exc

    @staticmethod
    def _string(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value if value is not None else "")

    @staticmethod
    def _bool(value: object) -> bool:
        return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}

    @staticmethod
    def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError, OverflowError):
            parsed = default
        return max(minimum, min(parsed, maximum))

    @staticmethod
    def _bounded_float(value: object, default: float, minimum: float, maximum: float) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError, OverflowError):
            parsed = float(default)
        if parsed != parsed or parsed in (float("inf"), float("-inf")):
            parsed = float(default)
        return max(float(minimum), min(parsed, float(maximum)))
