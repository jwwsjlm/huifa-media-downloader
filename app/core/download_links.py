from __future__ import annotations

import re
from urllib.parse import urlparse


_DOWNLOAD_URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_TRAILING_URL_PUNCTUATION = ".,;:!?，。；：！？、"


def normalize_download_link(value: str) -> str:
    """Return one usable HTTP(S) link or an empty string.

    Clipboard text frequently wraps links in Markdown, angle brackets, or
    sentence punctuation.  Remove only those wrappers and preserve the URL's
    path, query and balanced parentheses for yt-dlp.
    """

    candidate = str(value or "").strip()
    if not candidate:
        return ""

    # Sentence punctuation is outside Markdown/angle-bracket wrappers. Strip
    # it before wrapper detection so values such as ``<https://example>。``
    # and ``[title](https://example)。`` are handled like browser clipboard
    # links instead of being rejected as malformed URLs.
    candidate = candidate.rstrip(_TRAILING_URL_PUNCTUATION)

    markdown = re.fullmatch(
        r"\[[^\]]*\]\((https?://[^\s]+)\)",
        candidate,
        re.IGNORECASE,
    )
    if markdown:
        candidate = markdown.group(1).strip()
    elif candidate.startswith("<") and candidate.endswith(">"):
        candidate = candidate[1:-1].strip()

    candidate = candidate.rstrip(_TRAILING_URL_PUNCTUATION)
    for opener, closer in (("(", ")"), ("[", "]"), ("{", "}")):
        while candidate.endswith(closer) and candidate.count(closer) > candidate.count(opener):
            candidate = candidate[:-1]
    if not candidate:
        return ""

    try:
        parsed = urlparse(candidate)
        hostname = parsed.hostname
    except ValueError:
        return ""
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        return ""
    return candidate


def extract_download_links(text: str) -> list[str]:
    """Extract ordered, de-duplicated HTTP(S) links from pasted text."""

    links: list[str] = []
    seen: set[str] = set()
    for match in _DOWNLOAD_URL_PATTERN.finditer(str(text or "")):
        link = normalize_download_link(match.group(0))
        if not link or link in seen:
            continue
        seen.add(link)
        links.append(link)
    if not links:
        single = normalize_download_link(text)
        if single:
            links.append(single)
    return links
