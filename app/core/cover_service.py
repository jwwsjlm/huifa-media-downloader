from __future__ import annotations

import math
import mimetypes
import os
import re
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

import requests
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QMimeData, QSize, Qt
from PySide6.QtGui import QColor, QImage, QImageReader, QImageWriter, QPainter


DEFAULT_MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_PIXELS = 50_000_000
MAX_COVER_DIMENSION = 16_384
MAX_GENERATION_INPUT_BYTES = 40 * 1024 * 1024
DEFAULT_URL_TIMEOUT = (5.0, 20.0)


class CoverServiceError(RuntimeError):
    """Base error raised by the cover workflow."""


class CoverValidationError(CoverServiceError):
    """The caller supplied invalid cover data or options."""


class CoverLoadError(CoverServiceError):
    """A local or remote cover could not be loaded safely."""


class CoverSaveError(CoverServiceError):
    """A cover could not be encoded or saved."""


class CoverFitMode(str, Enum):
    CROP = "crop"
    PAD = "pad"


class CoverPresetId(str, Enum):
    LANDSCAPE_16_9 = "landscape_16_9"
    PORTRAIT_9_16 = "portrait_9_16"
    PORTRAIT_3_4 = "portrait_3_4"
    LANDSCAPE_4_3 = "landscape_4_3"
    SQUARE_1_1 = "square_1_1"


class CoverSourceKind(str, Enum):
    LOCAL = "local"
    URL = "url"
    MEMORY = "memory"


@dataclass(frozen=True, slots=True)
class CoverPreset:
    id: CoverPresetId
    label: str
    width: int
    height: int

    @property
    def aspect_ratio(self) -> float:
        return self.width / self.height


COVER_PRESETS: Mapping[CoverPresetId, CoverPreset] = MappingProxyType(
    {
        CoverPresetId.LANDSCAPE_16_9: CoverPreset(
            CoverPresetId.LANDSCAPE_16_9, "横版 16:9", 1280, 720
        ),
        CoverPresetId.PORTRAIT_9_16: CoverPreset(
            CoverPresetId.PORTRAIT_9_16, "抖音竖屏 9:16", 1080, 1920
        ),
        CoverPresetId.PORTRAIT_3_4: CoverPreset(
            CoverPresetId.PORTRAIT_3_4, "微信视频号竖版 3:4", 1080, 1440
        ),
        CoverPresetId.LANDSCAPE_4_3: CoverPreset(
            CoverPresetId.LANDSCAPE_4_3, "微信/通用横版 4:3", 1440, 1080
        ),
        CoverPresetId.SQUARE_1_1: CoverPreset(
            CoverPresetId.SQUARE_1_1, "方形 1:1", 1080, 1080
        ),
    }
)


def resolve_cover_preset(preset: CoverPresetId | CoverPreset | str) -> CoverPreset:
    if isinstance(preset, CoverPreset):
        return preset
    try:
        preset_id = preset if isinstance(preset, CoverPresetId) else CoverPresetId(str(preset))
    except ValueError as exc:
        available = ", ".join(item.value for item in CoverPresetId)
        raise CoverValidationError(f"未知封面预设：{preset}；可用值：{available}") from exc
    return COVER_PRESETS[preset_id]


def _validated_dimension(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CoverValidationError(f"{name}必须是整数")
    if value <= 0 or value > MAX_COVER_DIMENSION:
        raise CoverValidationError(f"{name}必须在 1 到 {MAX_COVER_DIMENSION} 之间")
    return value


@dataclass(frozen=True, slots=True)
class CoverExportOptions:
    width: int
    height: int
    quality: int = 90
    fit_mode: CoverFitMode = CoverFitMode.CROP
    background_color: str = "#000000"
    focus_x: float = 0.5
    focus_y: float = 0.5
    optimized: bool = True
    progressive: bool = True

    def __post_init__(self) -> None:
        _validated_dimension(self.width, "封面宽度")
        _validated_dimension(self.height, "封面高度")
        if self.width * self.height > DEFAULT_MAX_PIXELS:
            raise CoverValidationError(f"封面像素总数不能超过 {DEFAULT_MAX_PIXELS:,}")
        if isinstance(self.quality, bool) or not isinstance(self.quality, int) or not 1 <= self.quality <= 100:
            raise CoverValidationError("JPG 质量必须在 1 到 100 之间")
        try:
            fit_mode = self.fit_mode if isinstance(self.fit_mode, CoverFitMode) else CoverFitMode(str(self.fit_mode))
        except ValueError as exc:
            raise CoverValidationError("缩放模式必须是 crop（裁剪）或 pad（留白）") from exc
        object.__setattr__(self, "fit_mode", fit_mode)
        color = QColor(str(self.background_color))
        if not color.isValid():
            raise CoverValidationError(f"无效的封面背景颜色：{self.background_color}")
        # JPG has no alpha channel. Normalizing to an opaque color makes the
        # preview, clipboard preparation, and final file deterministic.
        color.setAlpha(255)
        object.__setattr__(self, "background_color", color.name(QColor.NameFormat.HexRgb))
        for value, name in ((self.focus_x, "水平裁剪焦点"), (self.focus_y, "垂直裁剪焦点")):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise CoverValidationError(f"{name}必须是 0 到 1 之间的数字")
            if not 0.0 <= float(value) <= 1.0:
                raise CoverValidationError(f"{name}必须在 0 到 1 之间")
        if not isinstance(self.optimized, bool) or not isinstance(self.progressive, bool):
            raise CoverValidationError("JPG 优化和渐进式选项必须是布尔值")

    @classmethod
    def from_preset(
        cls,
        preset: CoverPresetId | CoverPreset | str,
        *,
        quality: int = 90,
        fit_mode: CoverFitMode | str = CoverFitMode.CROP,
        background_color: str = "#000000",
        focus_x: float = 0.5,
        focus_y: float = 0.5,
        optimized: bool = True,
        progressive: bool = True,
    ) -> "CoverExportOptions":
        resolved = resolve_cover_preset(preset)
        return cls(
            width=resolved.width,
            height=resolved.height,
            quality=quality,
            fit_mode=fit_mode,
            background_color=background_color,
            focus_x=focus_x,
            focus_y=focus_y,
            optimized=optimized,
            progressive=progressive,
        )


@dataclass(frozen=True, slots=True)
class LoadedCover:
    _image: QImage = field(repr=False)
    source: str
    source_kind: CoverSourceKind
    source_format: str = ""
    content_type: str = ""
    byte_size: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self._image, QImage) or self._image.isNull():
            raise CoverValidationError("封面图像为空")
        object.__setattr__(self, "_image", self._image.copy())

    @property
    def width(self) -> int:
        return self._image.width()

    @property
    def height(self) -> int:
        return self._image.height()

    @property
    def image(self) -> QImage:
        """Return a detached image so callers cannot mutate the source asset."""

        return self._image.copy()


@dataclass(frozen=True, slots=True)
class CoverExportResult:
    path: Path
    width: int
    height: int
    byte_size: int
    quality: int


@dataclass(frozen=True, slots=True)
class ClipboardImageData:
    """Clipboard-ready PNG data without touching the global clipboard."""

    png_bytes: bytes
    width: int
    height: int

    def __post_init__(self) -> None:
        if not self.png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            raise CoverValidationError("剪贴板图像不是有效的 PNG 数据")

    def to_qimage(self) -> QImage:
        image = QImage.fromData(self.png_bytes, "PNG")
        if image.isNull():
            raise CoverValidationError("无法恢复剪贴板 PNG 图像")
        return image

    def to_mime_data(self) -> QMimeData:
        """Create a fresh QMimeData for QApplication.clipboard().setMimeData()."""

        mime_data = QMimeData()
        mime_data.setData("image/png", QByteArray(self.png_bytes))
        mime_data.setImageData(self.to_qimage())
        return mime_data


_SECRET_OPTION_KEYS = frozenset(
    {
        "apikey",
        "auth",
        "authentication",
        "authorization",
        "bearer",
        "clientsecret",
        "credential",
        "credentials",
        "idtoken",
        "password",
        "refreshtoken",
        "secret",
        "sessiontoken",
        "token",
        "accesstoken",
    }
)


def _normalized_option_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).casefold())


def _is_secret_option_key(key: object) -> bool:
    normalized = _normalized_option_key(key)
    return normalized in _SECRET_OPTION_KEYS or normalized.endswith(
        (
            "apikey",
            "accesstoken",
            "authtoken",
            "bearertoken",
            "clientsecret",
            "credential",
            "credentials",
            "idtoken",
            "password",
            "refreshtoken",
            "sessiontoken",
        )
    )


def _contains_secret_option(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if _is_secret_option_key(key) or _contains_secret_option(item):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_contains_secret_option(item) for item in value)
    return False


def _freeze_json_value(value: object, path: str = "provider_options") -> object:
    """Validate and detach provider-facing metadata as immutable JSON data."""

    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise CoverValidationError(f"{path} 的键必须是非空字符串")
            frozen[key] = _freeze_json_value(item, f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(item, f"{path}[]") for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CoverValidationError(f"{path} 不允许 NaN 或无限值")
        return value
    raise CoverValidationError(f"{path} 包含不可序列化的值：{type(value).__name__}")


@dataclass(frozen=True, slots=True)
class CoverGenerationRequest:
    """Provider-neutral request for a future image-generation adapter.

    Authentication is intentionally absent. A provider implementation should
    receive credentials through its constructor from the application's secure
    store and must never add them to this serializable request model.
    """

    source_png: bytes = field(repr=False)
    prompt: str
    width: int
    height: int
    count: int = 1
    provider_options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.source_png, bytes) or not self.source_png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise CoverValidationError("AI 二创输入必须是 PNG 图像数据")
        if len(self.source_png) > MAX_GENERATION_INPUT_BYTES:
            raise CoverValidationError("AI 二创输入图像过大")
        prompt = str(self.prompt or "").strip()
        if not prompt:
            raise CoverValidationError("AI 二创提示词不能为空")
        if len(prompt) > 8_000:
            raise CoverValidationError("AI 二创提示词不能超过 8000 个字符")
        object.__setattr__(self, "prompt", prompt)
        _validated_dimension(self.width, "AI 输出宽度")
        _validated_dimension(self.height, "AI 输出高度")
        if self.width * self.height > DEFAULT_MAX_PIXELS:
            raise CoverValidationError(f"AI 输出像素总数不能超过 {DEFAULT_MAX_PIXELS:,}")
        if isinstance(self.count, bool) or not isinstance(self.count, int) or not 1 <= self.count <= 4:
            raise CoverValidationError("AI 二创数量必须在 1 到 4 之间")
        options = dict(self.provider_options or {})
        if _contains_secret_option(options):
            raise CoverValidationError("provider_options 不允许包含 API Key、Token 或密码")
        object.__setattr__(self, "provider_options", _freeze_json_value(options))


@dataclass(frozen=True, slots=True)
class GeneratedCover:
    image_bytes: bytes = field(repr=False)
    mime_type: str
    provider_id: str
    revised_prompt: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.image_bytes:
            raise CoverValidationError("AI 二创结果图像为空")
        if len(self.image_bytes) > MAX_GENERATION_INPUT_BYTES:
            raise CoverValidationError("AI 二创结果图像过大")
        mime_type = str(self.mime_type or "").split(";", 1)[0].strip().lower()
        if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
            raise CoverValidationError(f"不支持的 AI 二创图像格式：{self.mime_type}")
        provider_id = str(self.provider_id or "").strip()
        if not provider_id:
            raise CoverValidationError("AI provider 标识不能为空")
        object.__setattr__(self, "mime_type", mime_type)
        object.__setattr__(self, "provider_id", provider_id)
        metadata = dict(self.metadata or {})
        if _contains_secret_option(metadata):
            raise CoverValidationError("AI 二创结果元数据不允许包含密钥或 Token")
        object.__setattr__(self, "metadata", _freeze_json_value(metadata, "metadata"))


GenerationProgressCallback = Callable[[int, str], None]


class CoverService:
    """Load, transform, export, and prepare cover images for UI workflows."""

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES,
        max_pixels: int = DEFAULT_MAX_PIXELS,
        url_timeout: tuple[float, float] = DEFAULT_URL_TIMEOUT,
    ) -> None:
        if max_download_bytes <= 0:
            raise CoverValidationError("远程封面大小上限必须大于 0")
        if max_pixels <= 0:
            raise CoverValidationError("封面像素上限必须大于 0")
        if (
            not isinstance(url_timeout, tuple)
            or len(url_timeout) != 2
            or any(isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0 for value in url_timeout)
        ):
            raise CoverValidationError("远程封面连接和读取超时必须是两个正数")
        self._session = session or requests.Session()
        self._owns_session = session is None
        self.max_download_bytes = int(max_download_bytes)
        self.max_pixels = int(max_pixels)
        self.url_timeout = url_timeout

    def close(self) -> None:
        if self._owns_session:
            self._session.close()

    def __enter__(self) -> "CoverService":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def load(
        self,
        source: str | os.PathLike[str],
        *,
        proxy: str = "",
        request_headers: Mapping[str, str] | None = None,
    ) -> LoadedCover:
        if isinstance(source, os.PathLike):
            return self.load_local(source)
        text = str(source or "").strip()
        if not text:
            raise CoverLoadError("封面来源不能为空")
        parsed = urlsplit(text)
        if parsed.scheme.lower() in {"http", "https"}:
            return self.load_url(text, proxy=proxy, request_headers=request_headers)
        # urlsplit treats a Windows drive letter as a URI scheme.
        is_windows_path = bool(re.match(r"^[A-Za-z]:[\\/]", text))
        if parsed.scheme and not is_windows_path:
            raise CoverLoadError("封面地址仅支持 HTTP 或 HTTPS")
        return self.load_local(text)

    def load_local(self, source: str | os.PathLike[str]) -> LoadedCover:
        path = Path(source).expanduser()
        try:
            if not path.exists():
                raise CoverLoadError(f"封面文件不存在：{path}")
            if not path.is_file():
                raise CoverLoadError(f"封面路径不是文件：{path}")
            byte_size = path.stat().st_size
            if byte_size <= 0:
                raise CoverLoadError("封面文件为空")
            if byte_size > self.max_download_bytes:
                raise CoverLoadError(f"封面文件超过 {self.max_download_bytes // (1024 * 1024)} MB 上限")
            payload = path.read_bytes()
        except CoverLoadError:
            raise
        except OSError as exc:
            raise CoverLoadError(f"无法读取封面文件：{exc}") from exc
        content_type = mimetypes.guess_type(path.name)[0] or ""
        return self.load_bytes(
            payload,
            source=str(path.resolve()),
            source_kind=CoverSourceKind.LOCAL,
            content_type=content_type,
        )

    def load_url(
        self,
        url: str,
        *,
        proxy: str = "",
        request_headers: Mapping[str, str] | None = None,
    ) -> LoadedCover:
        normalized_url = self._validate_http_url(url)
        headers = {
            "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8,*/*;q=0.2",
            "User-Agent": "HuifaVideoDownloader/cover-loader",
        }
        headers.update({str(key): str(value) for key, value in (request_headers or {}).items()})
        kwargs: dict[str, Any] = {
            "stream": True,
            "timeout": self.url_timeout,
            "allow_redirects": True,
            "headers": headers,
        }
        if proxy:
            kwargs["proxies"] = {"http": proxy, "https": proxy}

        response: requests.Response | None = None
        try:
            response = self._session.get(normalized_url, **kwargs)
            response.raise_for_status()
            resolved_url = self._validate_http_url(str(response.url or normalized_url))
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if content_type.startswith("text/") or content_type in {
                "application/json",
                "application/xml",
                "application/xhtml+xml",
            }:
                raise CoverLoadError(f"远程地址返回的不是图像：{content_type}")
            content_length = str(response.headers.get("Content-Length") or "").strip()
            if content_length:
                try:
                    announced_size = int(content_length)
                except ValueError:
                    announced_size = 0
                if announced_size > self.max_download_bytes:
                    raise CoverLoadError(
                        f"远程封面超过 {self.max_download_bytes // (1024 * 1024)} MB 上限"
                    )
            chunks: list[bytes] = []
            downloaded = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                downloaded += len(chunk)
                if downloaded > self.max_download_bytes:
                    raise CoverLoadError(
                        f"远程封面超过 {self.max_download_bytes // (1024 * 1024)} MB 上限"
                    )
                chunks.append(bytes(chunk))
            if not chunks:
                raise CoverLoadError("远程封面内容为空")
            return self.load_bytes(
                b"".join(chunks),
                source=resolved_url,
                source_kind=CoverSourceKind.URL,
                content_type=content_type,
            )
        except CoverServiceError:
            raise
        except requests.RequestException as exc:
            raise CoverLoadError(f"下载远程封面失败：{exc}") from exc
        finally:
            if response is not None:
                response.close()

    def load_bytes(
        self,
        payload: bytes | bytearray | memoryview,
        *,
        source: str = "<memory>",
        source_kind: CoverSourceKind = CoverSourceKind.MEMORY,
        content_type: str = "",
    ) -> LoadedCover:
        data = bytes(payload)
        if not data:
            raise CoverLoadError("封面图像数据为空")
        if len(data) > self.max_download_bytes:
            raise CoverLoadError(f"封面图像超过 {self.max_download_bytes // (1024 * 1024)} MB 上限")

        byte_array = QByteArray(data)
        buffer = QBuffer(byte_array)
        if not buffer.open(QIODevice.OpenModeFlag.ReadOnly):
            raise CoverLoadError("无法打开封面图像数据")
        try:
            reader = QImageReader(buffer)
            reader.setAutoTransform(True)
            reader.setDecideFormatFromContent(True)
            declared_size = reader.size()
            if declared_size.isValid():
                self._validate_image_size(declared_size.width(), declared_size.height())
            source_format = bytes(reader.format()).decode("ascii", errors="replace").lower()
            image = reader.read()
            if image.isNull():
                detail = reader.errorString() or "不支持的图片格式"
                raise CoverLoadError(f"无法解析封面图像：{detail}")
            self._validate_image_size(image.width(), image.height())
        finally:
            buffer.close()
        return LoadedCover(
            image,
            source=str(source),
            source_kind=source_kind,
            source_format=source_format,
            content_type=str(content_type or "").lower(),
            byte_size=len(data),
        )

    def render(self, source: LoadedCover | QImage, options: CoverExportOptions) -> QImage:
        image = self._source_image(source)
        target_size = QSize(options.width, options.height)
        if options.fit_mode is CoverFitMode.CROP:
            scaled = image.scaled(
                target_size,
                aspectMode=Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                mode=Qt.TransformationMode.SmoothTransformation,
            )
            overflow_x = max(0, scaled.width() - options.width)
            overflow_y = max(0, scaled.height() - options.height)
            offset_x = round(overflow_x * float(options.focus_x))
            offset_y = round(overflow_y * float(options.focus_y))
            draw_x = -offset_x
            draw_y = -offset_y
        else:
            scaled = image.scaled(
                target_size,
                aspectMode=Qt.AspectRatioMode.KeepAspectRatio,
                mode=Qt.TransformationMode.SmoothTransformation,
            )
            draw_x = (options.width - scaled.width()) // 2
            draw_y = (options.height - scaled.height()) // 2

        rendered = QImage(options.width, options.height, QImage.Format.Format_RGB32)
        rendered.fill(QColor(options.background_color))
        painter = QPainter(rendered)
        try:
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawImage(draw_x, draw_y, scaled)
        finally:
            painter.end()
        rendered.setDevicePixelRatio(1.0)
        return rendered

    def save_jpeg(
        self,
        source: LoadedCover | QImage,
        destination: str | os.PathLike[str],
        options: CoverExportOptions,
        *,
        overwrite: bool = True,
    ) -> CoverExportResult:
        target = Path(destination).expanduser()
        if not target.name:
            raise CoverSaveError("JPG 保存路径不能为空")
        if not target.suffix:
            target = target.with_suffix(".jpg")
        elif target.suffix.lower() not in {".jpg", ".jpeg"}:
            raise CoverSaveError("封面保存格式必须是 .jpg 或 .jpeg")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CoverSaveError(f"无法创建封面保存目录：{exc}") from exc
        if target.exists() and not overwrite:
            raise CoverSaveError(f"封面文件已存在：{target}")

        rendered = self.render(source, options)
        payload = self._encode_image(
            rendered,
            b"JPEG",
            quality=options.quality,
            optimized=options.optimized,
            progressive=options.progressive,
        )
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{target.stem}-",
                suffix=".tmp",
                dir=str(target.parent),
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if target.exists() and not overwrite:
                raise CoverSaveError(f"封面文件已存在：{target}")
            os.replace(temporary_path, target)
            temporary_path = None
        except CoverServiceError:
            raise
        except OSError as exc:
            raise CoverSaveError(f"保存 JPG 封面失败：{exc}") from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
        resolved_target = target.resolve()
        return CoverExportResult(
            path=resolved_target,
            width=options.width,
            height=options.height,
            byte_size=resolved_target.stat().st_size,
            quality=options.quality,
        )

    def prepare_clipboard(
        self,
        source: LoadedCover | QImage,
        options: CoverExportOptions | None = None,
    ) -> ClipboardImageData:
        image = self.render(source, options) if options is not None else self._source_image(source)
        payload = self._encode_image(image, b"PNG")
        return ClipboardImageData(payload, image.width(), image.height())

    def prepare_generation_request(
        self,
        source: LoadedCover | QImage,
        prompt: str,
        *,
        options: CoverExportOptions | None = None,
        count: int = 1,
        provider_options: Mapping[str, Any] | None = None,
    ) -> CoverGenerationRequest:
        image = self.render(source, options) if options is not None else self._source_image(source)
        source_png = self._encode_image(image, b"PNG")
        return CoverGenerationRequest(
            source_png=source_png,
            prompt=prompt,
            width=image.width(),
            height=image.height(),
            count=count,
            provider_options=provider_options or {},
        )

    def _source_image(self, source: LoadedCover | QImage) -> QImage:
        if isinstance(source, LoadedCover):
            image = source.image
        elif isinstance(source, QImage):
            image = source.copy()
        else:
            raise CoverValidationError("封面来源必须是 LoadedCover 或 QImage")
        if image.isNull():
            raise CoverValidationError("封面图像为空")
        self._validate_image_size(image.width(), image.height())
        return image

    def _validate_image_size(self, width: int, height: int) -> None:
        if width <= 0 or height <= 0:
            raise CoverLoadError("封面图像尺寸无效")
        if width > MAX_COVER_DIMENSION or height > MAX_COVER_DIMENSION:
            raise CoverLoadError(f"封面任一边不能超过 {MAX_COVER_DIMENSION} 像素")
        if width * height > self.max_pixels:
            raise CoverLoadError(f"封面像素总数不能超过 {self.max_pixels:,}")

    @staticmethod
    def _validate_http_url(url: str) -> str:
        text = str(url or "").strip()
        try:
            parsed = urlsplit(text)
            port = parsed.port
        except ValueError as exc:
            raise CoverLoadError("远程封面地址无效") from exc
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise CoverLoadError("远程封面地址仅支持 HTTP 或 HTTPS")
        if parsed.username or parsed.password:
            raise CoverLoadError("远程封面地址不能包含用户名或密码")
        if port is not None and not 1 <= port <= 65535:
            raise CoverLoadError("远程封面端口无效")
        return text

    @staticmethod
    def _encode_image(
        image: QImage,
        image_format: bytes,
        *,
        quality: int = -1,
        optimized: bool = False,
        progressive: bool = False,
    ) -> bytes:
        byte_array = QByteArray()
        buffer = QBuffer(byte_array)
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
            raise CoverSaveError("无法创建封面编码缓冲区")
        try:
            writer = QImageWriter(buffer, image_format)
            if quality >= 0:
                writer.setQuality(quality)
            writer.setOptimizedWrite(bool(optimized))
            writer.setProgressiveScanWrite(bool(progressive))
            if not writer.write(image):
                detail = writer.errorString() or "未知编码错误"
                raise CoverSaveError(f"封面图像编码失败：{detail}")
            return bytes(byte_array)
        finally:
            buffer.close()
