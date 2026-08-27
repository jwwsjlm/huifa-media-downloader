from __future__ import annotations


SUBTITLE_DISABLED = "none"
DEFAULT_SUBTITLE_FORMAT = "srt/vtt/best"

# yt-dlp uses extractor-provided language identifiers. Keep the common exact
# identifiers here so one requested language produces at most one subtitle
# file, while still allowing advanced users to type another identifier.
def normalize_subtitle_language(value: object) -> str:
    language = str(value or "").strip()
    if not language or language.casefold() in {"none", "off", "false", "disabled"}:
        return SUBTITLE_DISABLED
    # yt-dlp accepts extractor language identifiers and regular expressions.
    # Bound the persisted value to prevent accidental multi-line CLI values.
    return language.replace("\r", "").replace("\n", "")[:80] or SUBTITLE_DISABLED


def subtitle_ytdlp_options(language: object) -> dict[str, object]:
    normalized = normalize_subtitle_language(language)
    if normalized == SUBTITLE_DISABLED:
        return {
            "writesubtitles": False,
            "writeautomaticsub": False,
        }
    return {
        # YoutubeDL.process_subtitles gives normal/uploader subtitles priority
        # and fills the same language from automatic captions only when needed.
        "writesubtitles": True,
        "writeautomaticsub": True,
        "subtitleslangs": [normalized],
        "subtitlesformat": DEFAULT_SUBTITLE_FORMAT,
    }
