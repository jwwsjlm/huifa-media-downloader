from __future__ import annotations

from datetime import datetime

from app.presentation.i18n import runtime_text, text as ui_text


def format_file_size(value: int) -> str:
    size = max(0.0, float(value or 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "0 B"


PLATFORM_TEXT = {
    "douyin": "Douyin",
    "bilibili": "Bilibili",
    "tencent": "WeChat Channels",
    "kuaishou": "Kuaishou",
    "toutiao": "Toutiao",
    "xiaohongshu": "Xiaohongshu",
    "baijiahao": "Baijiahao",
    "alipay": "Alipay Life",
    "weibo": "Weibo",
    "hupu": "Hupu",
    "youtube": "YouTube",
    "tiktok": "TikTok",
}


def platform_label(platform: str) -> str:
    if platform in PLATFORM_TEXT:
        return ui_text(PLATFORM_TEXT[platform])
    return runtime_text(platform)


def publish_status_label(status: str) -> str:
    return {
        "pending": "等待发布",
        "uploading": "发布中",
        "success": "已发布",
        "failed": "发布失败",
    }.get(str(status or ""), str(status or ""))


def format_media_timestamp(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime(
            "%Y-%m-%d %H:%M"
        )
    except ValueError:
        return raw.replace("T", " ")[:16]


def error_category_text(category: str) -> str:
    return str(category or "未知")


def compact_path_display(value: str) -> str:
    """Keep absolute runtime paths out of normal presentation."""

    raw = str(value or "").strip()
    if not raw:
        return ""
    normalized = raw.replace("/", "\\")
    parts = [part for part in normalized.split("\\") if part]
    tools_index = next(
        (index for index, part in enumerate(parts) if part.casefold() == "tools"),
        -1,
    )
    if tools_index >= 0:
        return "\\".join(parts[tools_index:])
    if len(parts) >= 2:
        return f"…\\{parts[-2]}\\{parts[-1]}"
    return parts[-1] if parts else raw
