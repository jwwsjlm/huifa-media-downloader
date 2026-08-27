from __future__ import annotations


YTDLP_EJS_SOURCES = frozenset({"auto", "npm", "github", "local"})


def normalize_ytdlp_ejs_source(value: object) -> str:
    """Normalize the configured yt-dlp-ejs provider policy."""

    source = str(value or "auto").strip().casefold()
    return source if source in YTDLP_EJS_SOURCES else "auto"
