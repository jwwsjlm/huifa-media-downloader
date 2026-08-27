from __future__ import annotations

import base64
import json
import logging
import threading
import traceback
import unittest
from io import StringIO
from typing import Any, Callable
from unittest.mock import patch

import requests

from app.adapters.openai_cover_provider import (
    DEFAULT_OPENAI_IMAGE_ENDPOINT,
    OpenAICoverGenerationError,
    OpenAICoverGenerationProvider,
    normalize_openai_image_endpoint,
)
from app.core.cover_service import CoverGenerationRequest


SOURCE_PNG = b"\x89PNG\r\n\x1a\nsource-cover"
GENERATED_PNG = b"\x89PNG\r\n\x1a\ngenerated-cover"


class FakeResponse:
    def __init__(
        self,
        payload: Any,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
        text: str = "",
        json_error: Exception | None = None,
        close_error: Exception | None = None,
        body: bytes | None = None,
        on_chunk: Callable[[int], None] | None = None,
    ) -> None:
        self.payload = payload
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text
        self.json_error = json_error
        self.close_error = close_error
        self.body = (
            bytes(body)
            if body is not None
            else (
                b"{invalid-json"
                if json_error is not None
                else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            )
        )
        self.on_chunk = on_chunk
        self.close_count = 0

    def json(self) -> Any:
        if self.json_error is not None:
            raise self.json_error
        return self.payload

    def close(self) -> None:
        self.close_count += 1
        if self.close_error is not None:
            raise self.close_error

    def iter_content(
        self,
        chunk_size: int,
        decode_unicode: bool = False,
    ):
        del decode_unicode
        for offset in range(0, len(self.body), max(1, chunk_size)):
            if self.on_chunk is not None:
                self.on_chunk(offset)
            yield self.body[offset:offset + chunk_size]


class FakeSession:
    def __init__(
        self,
        response: FakeResponse | None = None,
        *,
        error: requests.RequestException | None = None,
        on_post: Callable[[], None] | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.on_post = on_post
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.close_count = 0

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((url, kwargs))
        if self.on_post is not None:
            self.on_post()
        if self.error is not None:
            raise self.error
        if self.response is None:
            raise AssertionError("FakeSession response was not configured")
        return self.response

    def close(self) -> None:
        self.close_count += 1


def generation_request(
    width: int = 1280,
    height: int = 720,
    *,
    count: int = 1,
) -> CoverGenerationRequest:
    return CoverGenerationRequest(
        source_png=SOURCE_PNG,
        prompt="保留人物主体并提升标题可读性",
        width=width,
        height=height,
        count=count,
        provider_options={"quality": "high", "input_fidelity": "high"},
    )


def success_response(*, revised_prompt: str = "增强对比度") -> FakeResponse:
    return FakeResponse(
        {
            "data": [
                {
                    "b64_json": base64.b64encode(GENERATED_PNG).decode("ascii"),
                    "revised_prompt": revised_prompt,
                }
            ]
        }
    )


def formatted_exception(error: BaseException) -> str:
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s\n%(exc_text)s"))
    logger = logging.getLogger(f"openai-cover-provider-test-{id(error)}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.ERROR)
    record = logger.makeRecord(
        logger.name,
        logging.ERROR,
        __file__,
        1,
        "AI cover failed",
        (),
        (type(error), error, error.__traceback__),
    )
    handler.emit(record)
    handler.flush()
    return stream.getvalue() + "".join(traceback.format_exception(error))


class OpenAICoverGenerationProviderTests(unittest.TestCase):
    API_KEY = "sk-proj-unit-test-secret-1234567890"

    def create_provider(self, session: FakeSession) -> OpenAICoverGenerationProvider:
        return OpenAICoverGenerationProvider(
            self.API_KEY,
            model="gpt-image-2",
            session=session,  # type: ignore[arg-type]
        )

    def test_authorization_is_header_only_and_b64_result_is_decoded(self) -> None:
        response = success_response()
        session = FakeSession(response)
        provider = self.create_provider(session)
        request = generation_request(count=1)
        progress: list[tuple[int, str]] = []

        results = provider.generate(request, progress=lambda *args: progress.append(args))

        self.assertEqual(len(session.calls), 1)
        url, kwargs = session.calls[0]
        self.assertEqual(url, "https://api.openai.com/v1/images/edits")
        self.assertEqual(kwargs["headers"], {"Authorization": f"Bearer {self.API_KEY}"})
        self.assertEqual(kwargs["timeout"], (10, 300))
        self.assertTrue(kwargs["stream"])
        self.assertEqual(kwargs["files"]["image"], ("source-cover.png", SOURCE_PNG, "image/png"))
        non_header_request = (url, kwargs["data"], kwargs["files"], kwargs["timeout"])
        self.assertNotIn(self.API_KEY, repr(non_header_request))
        self.assertNotIn(self.API_KEY, repr(request))
        self.assertNotIn(self.API_KEY, repr(provider))
        self.assertNotIn(self.API_KEY, repr(results))
        self.assertEqual(results[0].image_bytes, GENERATED_PNG)
        self.assertEqual(results[0].mime_type, "image/png")
        self.assertEqual(results[0].revised_prompt, "增强对比度")
        self.assertEqual(results[0].metadata["size"], "1536x1024")
        self.assertEqual(progress, [(5, "正在准备原始封面"), (15, "正在调用 GPT Image 创作封面"), (100, "AI 封面创作完成")])
        self.assertEqual(response.close_count, 1)

    def test_custom_api_base_url_is_normalized_to_images_edits_endpoint(self) -> None:
        self.assertEqual(normalize_openai_image_endpoint(""), DEFAULT_OPENAI_IMAGE_ENDPOINT)
        self.assertEqual(
            normalize_openai_image_endpoint("https://api.example.com/v1/"),
            "https://api.example.com/v1/images/edits",
        )
        self.assertEqual(
            normalize_openai_image_endpoint("https://api.example.com/openai/v1/images/edits"),
            "https://api.example.com/openai/v1/images/edits",
        )

        response = success_response()
        session = FakeSession(response)
        provider = OpenAICoverGenerationProvider(
            self.API_KEY,
            model="custom-image-model",
            endpoint="https://gateway.example.com/openai/v1",
            session=session,  # type: ignore[arg-type]
        )
        provider.generate(generation_request())
        self.assertEqual(
            session.calls[0][0],
            "https://gateway.example.com/openai/v1/images/edits",
        )

    def test_custom_api_url_rejects_credentials_and_accepts_user_supplied_http(self) -> None:
        with self.assertRaisesRegex(OpenAICoverGenerationError, "用户名或密码"):
            normalize_openai_image_endpoint("https://user:secret@example.com/v1")
        self.assertEqual(
            normalize_openai_image_endpoint("http://api.example.com/v1"),
            "http://api.example.com/v1/images/edits",
        )
        self.assertEqual(
            normalize_openai_image_endpoint("http://127.0.0.1:8080/v1"),
            "http://127.0.0.1:8080/v1/images/edits",
        )

    def test_landscape_portrait_and_square_map_to_supported_api_sizes(self) -> None:
        cases = (
            ((1280, 720), "1536x1024"),
            ((1080, 1920), "1024x1536"),
            ((1080, 1080), "1024x1024"),
        )
        for dimensions, expected in cases:
            with self.subTest(dimensions=dimensions):
                response = success_response()
                session = FakeSession(response)
                provider = self.create_provider(session)
                result = provider.generate(generation_request(*dimensions))
                self.assertEqual(session.calls[0][1]["data"]["size"], expected)
                self.assertEqual(result[0].metadata["size"], expected)
                self.assertEqual(response.close_count, 1)

    def test_cancel_before_request_does_not_open_network_connection(self) -> None:
        session = FakeSession(success_response())
        provider = self.create_provider(session)
        cancelled = threading.Event()
        cancelled.set()
        progress: list[tuple[int, str]] = []

        result = provider.generate(
            generation_request(),
            cancel_event=cancelled,
            progress=lambda *args: progress.append(args),
        )

        self.assertEqual(result, ())
        self.assertEqual(session.calls, [])
        self.assertEqual(progress, [])

    def test_cancel_after_response_still_closes_response(self) -> None:
        cancelled = threading.Event()
        response = success_response()
        session = FakeSession(response, on_post=cancelled.set)
        provider = self.create_provider(session)

        result = provider.generate(generation_request(), cancel_event=cancelled)

        self.assertEqual(result, ())
        self.assertEqual(response.close_count, 1)

    def test_cancel_while_streaming_response_stops_before_full_body_is_read(self) -> None:
        cancelled = threading.Event()
        payload = {
            "data": [
                {"b64_json": base64.b64encode(GENERATED_PNG).decode("ascii")}
            ]
        }
        body = json.dumps(payload).encode("utf-8") + (b" " * 70_000)
        seen_offsets: list[int] = []

        def cancel_on_second_chunk(offset: int) -> None:
            seen_offsets.append(offset)
            if offset > 0:
                cancelled.set()

        response = FakeResponse(payload, body=body, on_chunk=cancel_on_second_chunk)
        provider = self.create_provider(FakeSession(response))

        result = provider.generate(generation_request(), cancel_event=cancelled)

        self.assertEqual(result, ())
        self.assertEqual(seen_offsets, [0, 64 * 1024])
        self.assertEqual(response.close_count, 1)

    def test_announced_oversized_response_is_rejected_without_reading_body(self) -> None:
        response = success_response()
        provider = self.create_provider(FakeSession(response))
        response.headers["content-length"] = str(
            provider._response_size_limit(1) + 1
        )
        response.on_chunk = lambda _offset: self.fail(
            "oversized response body should not be read"
        )

        with self.assertRaisesRegex(OpenAICoverGenerationError, "响应过大"):
            provider.generate(generation_request())

        self.assertEqual(response.close_count, 1)

    def test_streamed_response_cannot_exceed_runtime_size_limit(self) -> None:
        response = FakeResponse(None, body=b"123456789")
        provider = self.create_provider(FakeSession(response))

        with (
            patch(
                "app.adapters.openai_cover_provider._MAX_ENCODED_IMAGE_BYTES",
                8,
            ),
            patch(
                "app.adapters.openai_cover_provider."
                "_RESPONSE_METADATA_ALLOWANCE_BYTES",
                0,
            ),
            self.assertRaisesRegex(OpenAICoverGenerationError, "响应过大"),
        ):
            provider.generate(generation_request())

        self.assertEqual(response.close_count, 1)

    def test_http_error_extracts_message_request_id_and_redacts_secrets(self) -> None:
        response = FakeResponse(
            {
                "error": {
                    "message": (
                        "quota exhausted; Authorization: Bearer "
                        f"{self.API_KEY}; token=secondary-secret"
                    )
                }
            },
            status_code=429,
            headers={"x-request-id": f"req-{self.API_KEY}"},
        )
        provider = self.create_provider(FakeSession(response))

        with self.assertRaises(OpenAICoverGenerationError) as context:
            provider.generate(generation_request())

        message = str(context.exception)
        self.assertIn("quota exhausted", message)
        self.assertIn("请求 ID", message)
        self.assertIn("***", message)
        self.assertNotIn(self.API_KEY, message)
        self.assertNotIn("secondary-secret", message)
        self.assertNotIn(self.API_KEY, repr(context.exception))
        self.assertNotIn(self.API_KEY, formatted_exception(context.exception))
        self.assertIsNone(context.exception.__cause__)
        self.assertEqual(response.close_count, 1)

    def test_http_error_accepts_string_error_shape(self) -> None:
        response = FakeResponse({"error": "account is not permitted"}, status_code=403)
        provider = self.create_provider(FakeSession(response))

        with self.assertRaisesRegex(OpenAICoverGenerationError, "account is not permitted"):
            provider.generate(generation_request())

        self.assertEqual(response.close_count, 1)

    def test_non_json_http_error_uses_bounded_redacted_text_preview(self) -> None:
        response = FakeResponse(
            None,
            status_code=502,
            body=(
                f"gateway failed; token={self.API_KEY}".encode("utf-8")
                + (b"x" * 20_000)
            ),
        )
        provider = self.create_provider(FakeSession(response))

        with self.assertRaises(OpenAICoverGenerationError) as context:
            provider.generate(generation_request())

        message = str(context.exception)
        self.assertIn("gateway failed", message)
        self.assertIn("***", message)
        self.assertNotIn(self.API_KEY, message)
        self.assertLess(len(message), 9_000)
        self.assertEqual(response.close_count, 1)

    def test_invalid_b64_is_actionable_and_response_is_closed(self) -> None:
        response = FakeResponse({"data": [{"b64_json": "%%%not-base64%%%"}]})
        provider = self.create_provider(FakeSession(response))

        with self.assertRaisesRegex(OpenAICoverGenerationError, "无效的图像数据"):
            provider.generate(generation_request())

        self.assertEqual(response.close_count, 1)

    def test_valid_base64_that_is_not_png_is_rejected(self) -> None:
        response = FakeResponse(
            {
                "data": [
                    {
                        "b64_json": base64.b64encode(
                            b"not-a-png-image"
                        ).decode("ascii")
                    }
                ]
            }
        )
        provider = self.create_provider(FakeSession(response))

        with self.assertRaisesRegex(OpenAICoverGenerationError, "无效的图像数据"):
            provider.generate(generation_request())

        self.assertEqual(response.close_count, 1)

    def test_provider_rejects_more_images_than_requested(self) -> None:
        encoded = base64.b64encode(GENERATED_PNG).decode("ascii")
        response = FakeResponse(
            {"data": [{"b64_json": encoded}, {"b64_json": encoded}]}
        )
        provider = self.create_provider(FakeSession(response))

        with self.assertRaisesRegex(OpenAICoverGenerationError, "超过请求数量"):
            provider.generate(generation_request(count=1))

        self.assertEqual(response.close_count, 1)

    def test_json_decode_failure_is_generic_closes_response_and_does_not_leak_key(self) -> None:
        response = FakeResponse(
            None,
            json_error=ValueError(f"decoder saw {self.API_KEY}"),
        )
        provider = self.create_provider(FakeSession(response))

        with self.assertRaises(OpenAICoverGenerationError) as context:
            provider.generate(generation_request())

        self.assertEqual(str(context.exception), "无法解析 GPT Image 响应")
        self.assertNotIn(self.API_KEY, formatted_exception(context.exception))
        self.assertIsNone(context.exception.__cause__)
        self.assertEqual(response.close_count, 1)

    def test_network_exception_is_redacted_and_does_not_keep_raw_cause_in_logs(self) -> None:
        network_error = requests.ConnectionError(
            f"socket failed with Authorization: Bearer {self.API_KEY}"
        )
        provider = self.create_provider(FakeSession(error=network_error))

        with self.assertRaises(OpenAICoverGenerationError) as context:
            provider.generate(generation_request())

        message = str(context.exception)
        self.assertIn("连接 OpenAI 图像服务失败", message)
        self.assertIn("***", message)
        self.assertNotIn(self.API_KEY, message)
        self.assertNotIn(self.API_KEY, formatted_exception(context.exception))
        self.assertIsNone(context.exception.__cause__)

    def test_response_close_failure_does_not_override_successful_generation(self) -> None:
        response = success_response()
        response.close_error = requests.ConnectionError(f"close failed with {self.API_KEY}")
        provider = self.create_provider(FakeSession(response))

        results = provider.generate(generation_request())

        self.assertEqual(results[0].image_bytes, GENERATED_PNG)
        self.assertEqual(response.close_count, 1)

    def test_owned_session_is_closed_at_most_once(self) -> None:
        session = FakeSession(success_response())
        with patch(
            "app.adapters.openai_cover_provider.requests.Session",
            return_value=session,
        ):
            provider = OpenAICoverGenerationProvider(self.API_KEY)

        provider.close()
        provider.close()

        self.assertEqual(session.close_count, 1)


if __name__ == "__main__":
    unittest.main()
