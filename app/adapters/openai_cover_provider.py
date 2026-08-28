from __future__ import annotations

import base64
import binascii
import json
import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

from app.core.cover_service import (
    CoverGenerationRequest,
    CoverServiceError,
    GeneratedCover,
    GenerationProgressCallback,
    MAX_GENERATION_INPUT_BYTES,
)
from app.core.redaction import redact_secret_text


class OpenAICoverGenerationError(CoverServiceError):
    """A GPT Image request failed without exposing credentials."""


DEFAULT_OPENAI_IMAGE_ENDPOINT = "https://api.openai.com/v1/images/edits"
_RESPONSE_CHUNK_BYTES = 64 * 1024
_RESPONSE_METADATA_ALLOWANCE_BYTES = 1024 * 1024
_ERROR_RESPONSE_LIMIT_BYTES = 1024 * 1024
_ERROR_TEXT_PREVIEW_BYTES = 8 * 1024
_MAX_ENCODED_IMAGE_BYTES = 4 * ((MAX_GENERATION_INPUT_BYTES + 2) // 3)


@dataclass(frozen=True, slots=True)
class _GenerationParameters:
    size: str
    quality: str
    input_fidelity: str


@dataclass(frozen=True, slots=True)
class _ApiResponseDocument:
    payload: Any | None
    text_preview: str = ""


def normalize_openai_image_endpoint(value: str | None) -> str:
    """Accept an API base URL or a complete Images Edits endpoint."""
    raw = str(value or "").strip()
    if not raw:
        return DEFAULT_OPENAI_IMAGE_ENDPOINT
    if len(raw) > 2048:
        raise OpenAICoverGenerationError("OpenAI API URL 过长")
    try:
        parsed = urlsplit(raw)
    except ValueError:
        raise OpenAICoverGenerationError("OpenAI API URL 格式不正确") from None
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise OpenAICoverGenerationError("OpenAI API URL 必须是完整的 HTTP 或 HTTPS 地址")
    if parsed.username or parsed.password:
        raise OpenAICoverGenerationError("OpenAI API URL 不能包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise OpenAICoverGenerationError("OpenAI API URL 不能包含查询参数或片段")
    path = re.sub(r"/{2,}", "/", parsed.path or "").rstrip("/")
    lowered = path.casefold()
    if not path:
        path = "/v1/images/edits"
    elif lowered.endswith("/images/edits"):
        pass
    elif lowered.endswith("/v1"):
        path += "/images/edits"
    else:
        path += "/images/edits"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def _redact_secret_text(value: object, api_key: str) -> str:
    """Return useful diagnostics without copying credentials into errors/logs."""

    return redact_secret_text(
        value,
        explicit_secrets=(api_key,),
        redact_urls=True,
    ).strip()


class OpenAICoverGenerationProvider:
    """Minimal GPT Image edit adapter for official or compatible APIs.

    Credentials are injected at construction time and never copied into the
    serializable generation request, settings.ini, task database or logs.
    """

    provider_id = "openai-gpt-image"

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "gpt-image-2",
        session: requests.Session | None = None,
        endpoint: str = DEFAULT_OPENAI_IMAGE_ENDPOINT,
    ) -> None:
        self._api_key = str(api_key or "").strip()
        if not self._api_key:
            raise OpenAICoverGenerationError("尚未配置 OpenAI API Key")
        self.model = str(model or "gpt-image-2").strip()
        if not self.model:
            raise OpenAICoverGenerationError("GPT Image 模型名称不能为空")
        self.endpoint = normalize_openai_image_endpoint(endpoint)
        self._session = session or requests.Session()
        self._owns_session = session is None
        self._closed = False

    def close(self) -> None:
        if not self._owns_session or self._closed:
            return
        self._closed = True
        self._session.close()

    def generate(
        self,
        request: CoverGenerationRequest,
        *,
        cancel_event: threading.Event | None = None,
        progress: GenerationProgressCallback | None = None,
    ) -> Sequence[GeneratedCover]:
        if cancel_event is not None and cancel_event.is_set():
            return ()
        if progress:
            progress(5, "正在准备原始封面")
        parameters = self._generation_parameters(request)
        headers = {"Authorization": f"Bearer {self._api_key}"}
        files = {"image": ("source-cover.png", request.source_png, "image/png")}
        response: requests.Response | None = None
        try:
            if progress:
                progress(15, "正在调用 GPT Image 创作封面")
            response = self._session.post(
                self.endpoint,
                headers=headers,
                data=self._request_data(request, parameters),
                files=files,
                timeout=(10, 300),
                stream=True,
            )
            if cancel_event is not None and cancel_event.is_set():
                return ()
            response_limit = (
                _ERROR_RESPONSE_LIMIT_BYTES
                if response.status_code >= 400
                else self._response_size_limit(request.count)
            )
            document = self._read_response_document(
                response,
                max_bytes=response_limit,
                cancel_event=cancel_event,
            )
            if document is None:
                return ()
            if response.status_code >= 400:
                raise OpenAICoverGenerationError(
                    self._api_error(response, document)
                )
            payload = document.payload
            if payload is None:
                raise OpenAICoverGenerationError("无法解析 GPT Image 响应") from None
            generated = self._generated_covers(
                payload,
                requested_count=request.count,
                parameters=parameters,
                cancel_event=cancel_event,
            )
            if generated is None:
                return ()
            if progress:
                progress(100, "AI 封面创作完成")
            return generated
        except OpenAICoverGenerationError:
            raise
        except requests.RequestException as exc:
            detail = self._safe_error_text(exc) or exc.__class__.__name__
            raise OpenAICoverGenerationError(f"连接 OpenAI 图像服务失败：{detail}") from None
        except (TypeError, ValueError):
            raise OpenAICoverGenerationError("无法解析 GPT Image 响应") from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    # A close failure must not mask the request result or leak
                    # implementation-specific transport details to the UI.
                    pass

    def _generation_parameters(
        self,
        request: CoverGenerationRequest,
    ) -> _GenerationParameters:
        return _GenerationParameters(
            size=self._api_size(request.width, request.height),
            quality=request.quality,
            input_fidelity=request.input_fidelity,
        )

    def _request_data(
        self,
        request: CoverGenerationRequest,
        parameters: _GenerationParameters,
    ) -> dict[str, str]:
        return {
            "model": self.model,
            "prompt": request.prompt,
            "n": str(request.count),
            "size": parameters.size,
            "quality": parameters.quality,
            "input_fidelity": parameters.input_fidelity,
            "output_format": "png",
        }

    @staticmethod
    def _response_size_limit(requested_count: int) -> int:
        return (
            max(1, int(requested_count)) * _MAX_ENCODED_IMAGE_BYTES
            + _RESPONSE_METADATA_ALLOWANCE_BYTES
        )

    def _read_response_document(
        self,
        response: requests.Response,
        *,
        max_bytes: int,
        cancel_event: threading.Event | None,
    ) -> _ApiResponseDocument | None:
        announced_size = self._announced_response_size(response)
        if announced_size is not None and announced_size > max_bytes:
            raise OpenAICoverGenerationError("GPT Image 响应过大")

        body = bytearray()
        for chunk in response.iter_content(
            chunk_size=_RESPONSE_CHUNK_BYTES,
            decode_unicode=False,
        ):
            if cancel_event is not None and cancel_event.is_set():
                return None
            if not chunk:
                continue
            if not isinstance(chunk, (bytes, bytearray)):
                raise OpenAICoverGenerationError("无法解析 GPT Image 响应")
            if len(body) + len(chunk) > max_bytes:
                raise OpenAICoverGenerationError("GPT Image 响应过大")
            body.extend(chunk)
        if cancel_event is not None and cancel_event.is_set():
            return None

        try:
            payload: Any = json.loads(body)
        except (TypeError, ValueError, UnicodeDecodeError):
            preview = bytes(body[:_ERROR_TEXT_PREVIEW_BYTES]).decode(
                "utf-8",
                errors="replace",
            ).strip()
            return _ApiResponseDocument(None, preview)
        return _ApiResponseDocument(payload)

    @staticmethod
    def _announced_response_size(response: requests.Response) -> int | None:
        raw_value = response.headers.get("content-length")
        try:
            size = int(str(raw_value).strip())
        except (TypeError, ValueError):
            return None
        return size if size >= 0 else None

    def _generated_covers(
        self,
        payload: Any,
        *,
        requested_count: int,
        parameters: _GenerationParameters,
        cancel_event: threading.Event | None,
    ) -> tuple[GeneratedCover, ...] | None:
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list) or not items:
            raise OpenAICoverGenerationError("GPT Image 没有返回图像")
        if len(items) > requested_count:
            raise OpenAICoverGenerationError("GPT Image 返回的图像数量超过请求数量")

        generated: list[GeneratedCover] = []
        for item in items:
            if cancel_event is not None and cancel_event.is_set():
                return None
            if not isinstance(item, dict) or not item.get("b64_json"):
                raise OpenAICoverGenerationError(
                    "GPT Image 返回格式不受支持（缺少 b64_json）"
                )
            encoded_image = item.pop("b64_json")
            image_bytes = self._decode_png_image(encoded_image)
            generated.append(
                GeneratedCover(
                    image_bytes=image_bytes,
                    mime_type="image/png",
                    provider_id=self.provider_id,
                )
            )
        return tuple(generated)

    @staticmethod
    def _decode_png_image(encoded_image: object) -> bytes:
        try:
            if isinstance(encoded_image, str):
                if len(encoded_image) > _MAX_ENCODED_IMAGE_BYTES:
                    raise OverflowError
                encoded_bytes = encoded_image.encode("ascii")
            elif isinstance(encoded_image, (bytes, bytearray)):
                if len(encoded_image) > _MAX_ENCODED_IMAGE_BYTES:
                    raise OverflowError
                encoded_bytes = bytes(encoded_image)
            else:
                raise TypeError
            image_bytes = base64.b64decode(encoded_bytes, validate=True)
            if (
                not image_bytes
                or len(image_bytes) > MAX_GENERATION_INPUT_BYTES
                or not image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
            ):
                raise ValueError
            return image_bytes
        except (
            binascii.Error,
            UnicodeEncodeError,
            OverflowError,
            ValueError,
            TypeError,
        ):
            raise OpenAICoverGenerationError(
                "GPT Image 返回了无效的图像数据"
            ) from None

    @staticmethod
    def _api_size(width: int, height: int) -> str:
        ratio = width / max(1, height)
        if ratio > 1.15:
            return "1536x1024"
        if ratio < 0.87:
            return "1024x1536"
        return "1024x1024"

    def _safe_error_text(self, value: object, *, limit: int = 600) -> str:
        return _redact_secret_text(value, self._api_key)[:limit]

    def _api_error(
        self,
        response: requests.Response,
        document: _ApiResponseDocument,
    ) -> str:
        request_id = self._safe_error_text(
            response.headers.get("x-request-id") or "",
            limit=200,
        )
        message = ""
        payload = document.payload
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or "").strip()
            elif isinstance(error, str):
                message = error.strip()
            if not message:
                message = str(payload.get("message") or "").strip()
        if not message:
            message = document.text_preview
        detail = self._safe_error_text(message) or f"HTTP {response.status_code}"
        if request_id:
            detail += f"（请求 ID：{request_id}）"
        return f"GPT Image 请求失败：{detail}"
