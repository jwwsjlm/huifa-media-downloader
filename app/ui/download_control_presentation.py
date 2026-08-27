from __future__ import annotations

from PySide6.QtWidgets import QComboBox

from app.core.transcode_service import normalize_transcode_encoder
from app.ui.i18n import text as ui_text


DOWNLOAD_QUALITY_VALUES = (
    "custom",
    "best",
    "8k",
    "4k",
    "2k",
    "1k",
    "1080p",
    "720p",
    "480p",
    "360p",
    "240p",
    "qcif",
)


def download_quality_text(value: str) -> str:
    normalized = str(value or "best").strip().casefold()
    labels = {
        "best": ui_text("Best quality"),
        "8k": "8K (4320p)",
        "4k": "4K (2160p)",
        "2k": "2K (1440p)",
        "1k": "1K (1080p)",
        "1080p": "1080p",
        "720p": "720p",
        "480p": "480p",
        "360p": "360p",
        "240p": "240p",
        "qcif": "QCIF (144p)",
        "custom": ui_text("Manual"),
    }
    return labels.get(normalized, normalized or ui_text("Best quality"))


def set_combo_current_data(
    combo: QComboBox,
    value: object,
    *,
    fallback: object,
) -> None:
    """Select item data predictably when persisted settings are invalid."""

    index = combo.findData(value)
    if index < 0:
        index = combo.findData(fallback)
    combo.setCurrentIndex(max(0, index))


SUBTITLE_LANGUAGE_LABELS = {
    "none": "Do not download subtitles",
    "all": "All available subtitles",
    "zh-Hans": "Simplified Chinese",
    "zh-Hant": "Traditional Chinese",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "ru": "Russian",
    "pt": "Portuguese",
    "ar": "Arabic",
    "hi": "Hindi",
    "id": "Indonesian",
    "vi": "Vietnamese",
    "th": "Thai",
    "tr": "Turkish",
}

AUDIO_TRACK_LABELS = {
    "default": "Default audio track",
    "original": "Original audio track",
    "all": "All audio tracks",
    **{
        language_code: translation_key
        for language_code, translation_key in SUBTITLE_LANGUAGE_LABELS.items()
        if language_code not in {"none", "all"}
    },
}

TRANSCODE_ENCODER_NATIVE_LABELS = {
    "libx264": "x264 (H.264 / AVC, CPU)",
    "libx265": "x265 (H.265 / HEVC, CPU)",
    "libsvtav1": "SVT-AV1 (AV1, CPU)",
    "libaom-av1": "AOM AV1 (AV1, CPU)",
    "librav1e": "rav1e (AV1, CPU)",
    "h264_nvenc": "NVIDIA NVENC H.264 (GPU)",
    "hevc_nvenc": "NVIDIA NVENC HEVC (GPU)",
    "av1_nvenc": "NVIDIA NVENC AV1 (GPU)",
    "h264_qsv": "Intel Quick Sync H.264 (GPU)",
    "hevc_qsv": "Intel Quick Sync HEVC (GPU)",
    "av1_qsv": "Intel Quick Sync AV1 (GPU)",
    "h264_amf": "AMD AMF H.264 (GPU)",
    "hevc_amf": "AMD AMF HEVC (GPU)",
    "av1_amf": "AMD AMF AV1 (GPU)",
}


def transcode_encoder_label(encoder: str) -> str:
    normalized = normalize_transcode_encoder(encoder)
    if normalized == "original":
        return ui_text("Keep original encoding (no conversion)")
    return TRANSCODE_ENCODER_NATIVE_LABELS.get(normalized, normalized)
