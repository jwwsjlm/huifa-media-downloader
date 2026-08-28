from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.core.redaction import redact_secret_text
from app.integrations.social_auto_upload.runtime import (
    account_check,
    account_login,
    core_status,
    publish_video,
)


@dataclass(frozen=True, slots=True)
class SauPlatformCapability:
    """UI-facing capabilities exposed by the vendored publishing core."""

    display_name: str
    supports_dual_thumbnail: bool = False
    supports_schedule: bool = False
    supports_collection: bool = False
    supports_playlist: bool = False
    supports_visibility: bool = False
    tid_required: bool = False
    interactive_login: bool = False


SAU_PLATFORM_CAPABILITIES: dict[str, SauPlatformCapability] = {
    "douyin": SauPlatformCapability(
        "抖音",
        supports_dual_thumbnail=True,
        supports_schedule=True,
        supports_collection=True,
    ),
    "kuaishou": SauPlatformCapability(
        "快手", supports_schedule=True, supports_collection=True
    ),
    "xiaohongshu": SauPlatformCapability("小红书", supports_schedule=True),
    "bilibili": SauPlatformCapability(
        "哔哩哔哩",
        supports_schedule=True,
        tid_required=True,
        interactive_login=True,
    ),
    "tencent": SauPlatformCapability(
        "视频号",
        supports_dual_thumbnail=True,
        supports_schedule=True,
        supports_collection=True,
    ),
    "baijiahao": SauPlatformCapability("百家号", supports_collection=True),
    "alipay": SauPlatformCapability("支付宝生活号", supports_collection=True),
    "weibo": SauPlatformCapability("微博", supports_collection=True),
    "hupu": SauPlatformCapability("虎扑"),
    "youtube": SauPlatformCapability(
        "YouTube", supports_playlist=True, supports_visibility=True
    ),
}

SAU_SUPPORTED_PLATFORMS = tuple(SAU_PLATFORM_CAPABILITIES)
SAU_PLATFORM_DISPLAY_NAMES = {
    name: capability.display_name
    for name, capability in SAU_PLATFORM_CAPABILITIES.items()
}
ACCOUNT_ACTIONS = frozenset({"login", "check"})
UPLOAD_VIDEO_ACTION = "upload-video"
YOUTUBE_VISIBILITY = {
    "": "public",
    "public": "public",
    "公开": "public",
    "unlisted": "unlisted",
    "不公开": "unlisted",
    "链接可见": "unlisted",
    "private": "private",
    "仅自己可见": "private",
    "私密/草稿": "private",
}


def get_sau_platform_capability(platform: str) -> SauPlatformCapability | None:
    return SAU_PLATFORM_CAPABILITIES.get(str(platform or "").strip().casefold())


def _redact_output(value: str) -> str:
    return redact_secret_text(
        value,
        replacement="<redacted>",
        redact_urls=True,
    )[-8000:]


@dataclass(frozen=True, slots=True)
class SauCoreCompatibility:
    compatible: bool
    platform: str = ""
    action: str = ""
    problems: tuple[str, ...] = ()

    def user_message(self) -> str:
        if self.compatible:
            return f"内置发布核心可用：{self.platform} / {self.action}"
        return "；".join(self.problems) or "内置发布核心不可用"


def probe_sau_compatibility(
    platform: str,
    action: str,
) -> SauCoreCompatibility:
    platform_key = str(platform or "").strip().casefold()
    action_key = str(action or "").strip().casefold()
    capability = get_sau_platform_capability(platform_key)
    problems: list[str] = []
    ok, detail = core_status()
    if not ok:
        problems.append(detail)
    if capability is None:
        problems.append(f"平台 {platform_key or '未知'} 尚未接入内置发布核心")
    elif action_key not in ACCOUNT_ACTIONS and action_key != UPLOAD_VIDEO_ACTION:
        problems.append(f"{capability.display_name} 不支持 {action_key or '该操作'}")
    return SauCoreCompatibility(
        not problems,
        platform_key,
        action_key,
        tuple(problems),
    )


def _text(settings: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = settings.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _tags(value: Any) -> list[str]:
    values = (
        value if isinstance(value, (list, tuple, set)) else str(value or "").split(",")
    )
    return [item for item in (str(raw).strip().lstrip("#") for raw in values) if item]


def build_upload_request(platform: str, payload: dict[str, Any]) -> dict[str, Any]:
    platform_key = str(platform or "").strip().casefold()
    capability = get_sau_platform_capability(platform_key)
    if capability is None:
        raise ValueError(f"平台 {platform_key or '未知'} 尚未接入视频发布")
    media = payload.get("media") or {}
    metadata = payload.get("metadata") or {}
    settings = payload.get("settings") or {}
    video = str(media.get("video_path") or "").strip()
    title = str(
        settings.get("title")
        or metadata.get("title")
        or (Path(video).stem if video else "")
    ).strip()
    description = str(settings.get("description") or metadata.get("description") or "")
    tags = _tags(settings.get("topics") or metadata.get("tags"))
    account = str(settings.get("account") or "default").strip() or "default"
    portrait = _text(settings, "thumbnail_portrait", "thumbnail_portrait_path")
    landscape = _text(settings, "thumbnail_landscape", "thumbnail_landscape_path")
    explicit = _text(settings, "thumbnail", "thumbnail_path")
    media_thumbnail = str(media.get("thumbnail_path") or "").strip()
    if capability.supports_dual_thumbnail:
        thumbnail = explicit or (media_thumbnail if not portrait else "")
    else:
        thumbnail = explicit or portrait or landscape or media_thumbnail

    collection = _text(settings, "collection", "collection_name")
    schedule = _text(settings, "schedule", "publish_at", "scheduled_at")
    if schedule:
        if not capability.supports_schedule:
            raise ValueError(
                f"{capability.display_name} 不支持定时发布，请清空发布时间后重试"
            )
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", schedule):
            raise ValueError("定时发布时间格式必须为 YYYY-MM-DD HH:MM")
        try:
            datetime.strptime(schedule, "%Y-%m-%d %H:%M")
        except ValueError as exc:
            raise ValueError(
                "定时发布时间不是有效日期，请使用 YYYY-MM-DD HH:MM"
            ) from exc

    tid = _text(settings, "tid", "partition")
    if capability.tid_required and not tid:
        raise ValueError("Bilibili 发布必须填写数字分区 ID（tid）")
    normalized_tid: int | None = None
    if tid:
        if not tid.isdigit() or int(tid) <= 0:
            raise ValueError("Bilibili 发布必须填写有效的数字分区 ID（tid）")
        normalized_tid = int(tid)
    playlist = ""
    if capability.supports_playlist:
        playlist = _text(settings, "playlist", "collection", "collection_name")
    visibility = "public"
    if capability.supports_visibility:
        raw_visibility = str(settings.get("visibility") or "").strip()
        visibility = YOUTUBE_VISIBILITY.get(raw_visibility.casefold())
        if visibility is None:
            raise ValueError("YouTube 可见范围仅支持 public、unlisted 或 private")
    return {
        "account_name": account,
        "video_file": video,
        "title": title,
        "description": description,
        "tags": tags,
        "thumbnail_file": thumbnail,
        "thumbnail_landscape_file": (
            landscape if capability.supports_dual_thumbnail else ""
        ),
        "thumbnail_portrait_file": (
            portrait if capability.supports_dual_thumbnail else ""
        ),
        "schedule": schedule,
        "collection_name": collection if capability.supports_collection else "",
        "tid": normalized_tid,
        "playlist": playlist,
        "visibility": visibility,
        "debug": False,
        "headless": True,
    }


def account_action(
    platform: str,
    action: str,
    account: str,
    cancel_event: threading.Event | None = None,
) -> tuple[bool, str]:
    platform_key = str(platform or "").strip().casefold()
    action_key = str(action or "").strip().casefold()
    account_name = str(account or "").strip()
    if action_key not in ACCOUNT_ACTIONS:
        return False, "不支持的账号操作"
    if not account_name:
        return False, "请先填写发布账号名"
    compatibility = probe_sau_compatibility(platform_key, action_key)
    if not compatibility.compatible:
        return False, compatibility.user_message()
    try:
        if action_key == "check":
            ok = account_check(platform_key, account_name, cancel_event=cancel_event)
            return ok, "Cookie 有效" if ok else "Cookie 无效、缺失或已过期"
        result = account_login(
            platform_key, account_name, headed=True, cancel_event=cancel_event
        )
        ok = bool(result.get("success"))
        message = str(result.get("message") or ("登录完成" if ok else "登录失败"))
        return ok, _redact_output(message)
    except InterruptedError:
        return False, "登录任务已取消"
    except Exception as exc:
        return False, _redact_output(str(exc))


def publish(
    platform: str,
    payload: dict[str, Any],
    cancel_event: threading.Event | None = None,
) -> tuple[bool, str]:
    platform_key = str(platform or "").strip().casefold()
    media = payload.get("media") or {}
    video = str(media.get("video_path") or "").strip()
    if not video or not Path(video).is_file():
        return False, f"视频文件不存在：{video or '未设置'}"
    compatibility = probe_sau_compatibility(platform_key, UPLOAD_VIDEO_ACTION)
    if not compatibility.compatible:
        return False, compatibility.user_message()
    try:
        result = publish_video(
            platform_key,
            build_upload_request(platform_key, payload),
            cancel_event=cancel_event,
        )
        return True, result
    except InterruptedError:
        return False, "发布任务已取消"
    except Exception as exc:
        return False, _redact_output(str(exc))
