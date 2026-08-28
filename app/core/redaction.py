from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit


_URL_PATTERN = re.compile(r"(?i)\bhttps?://[^\s<>\"']+")
_SECRET_KEY_NAMES = (
    r"authorization|proxy-authorization|set-cookie|cookie|cookies|"
    r"cookiefile|cookie_file|access[_ -]?token|refresh[_ -]?token|"
    r"api[_ -]?key|token|password|passwd|credential|secret"
)
_JSON_SECRET_PATTERN = re.compile(
    rf"(?ix)([\"']?(?:{_SECRET_KEY_NAMES})[\"']?\s*:\s*)"
    r'''(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^,\s}\]]+)'''
)
_HEADER_SECRET_PATTERN = re.compile(
    r"(?im)(\b(?:authorization|proxy-authorization|set-cookie|cookie|cookies|"
    r"cookiefile|cookie_file)\b\s*[:=]\s*)[^\r\n]+"
)
_VALUE_SECRET_PATTERN = re.compile(
    r"(?i)(\b(?:access[_ -]?token|refresh[_ -]?token|api[_ -]?key|token|"
    r"password|passwd|secret)\b\s*[:=]\s*)[^\s,;]+"
)
_BEARER_PATTERN = re.compile(r"(?i)(\bbearer\s+)[^\s,;]+")
_OPENAI_KEY_PATTERN = re.compile(r"(?i)\bsk-[a-z0-9_-]{8,}\b")
_TRAILING_URL_PUNCTUATION = ".,;:!?)]}"
_CLOSING_URL_DELIMITERS = {")": "(", "]": "[", "}": "{"}


def redact_url(value: object, *, invalid: str = "<无效地址>") -> str:
    """Remove credentials, query parameters and fragments from an URL."""

    try:
        parts = urlsplit(str(value or ""))
        hostname = parts.hostname
        if not parts.scheme or not hostname:
            return invalid
        host = f"[{hostname}]" if ":" in hostname else hostname
        try:
            port = parts.port
        except ValueError:
            return invalid
        netloc = f"{host}:{port}" if port is not None else host
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except (TypeError, ValueError):
        return invalid


def _redact_embedded_url(match: re.Match[str]) -> str:
    raw = match.group(0)
    suffix = ""
    while raw and raw[-1] in _TRAILING_URL_PUNCTUATION:
        closing = raw[-1]
        opening = _CLOSING_URL_DELIMITERS.get(closing)
        if opening is not None and raw.count(opening) >= raw.count(closing):
            break
        suffix = raw[-1] + suffix
        raw = raw[:-1]
    return redact_url(raw) + suffix


def redact_secret_text(
    value: object,
    *,
    explicit_secrets: Iterable[str] = (),
    replacement: str = "***",
    redact_urls: bool = True,
    limit: int | None = None,
) -> str:
    """Redact common credentials and optional known secrets from free text."""

    text = str(value or "")
    secrets = sorted(
        {str(secret) for secret in explicit_secrets if str(secret)},
        key=len,
        reverse=True,
    )
    for secret in secrets:
        text = text.replace(secret, replacement)
    if redact_urls:
        text = _URL_PATTERN.sub(_redact_embedded_url, text)
    prefixed_replacement = lambda match: match.group(1) + replacement
    text = _JSON_SECRET_PATTERN.sub(prefixed_replacement, text)
    text = _HEADER_SECRET_PATTERN.sub(prefixed_replacement, text)
    text = _VALUE_SECRET_PATTERN.sub(prefixed_replacement, text)
    text = _BEARER_PATTERN.sub(prefixed_replacement, text)
    text = _OPENAI_KEY_PATTERN.sub(lambda _match: replacement, text)
    if limit is not None:
        text = text[:max(0, int(limit))]
    return text
