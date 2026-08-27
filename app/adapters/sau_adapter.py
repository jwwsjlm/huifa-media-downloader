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
    vendor_root,
)


@dataclass(frozen=True, slots=True)
class SauPlatformCapability:
    """UI-facing capabilities exposed by the vendored publishing core."""

    command: str
    display_name: str
    supports_login: bool = True
    supports_check: bool = True
    supports_upload_video: bool = True
    thumbnail_flag: str = "--thumbnail"
    thumbnail_landscape_flag: str = ""
    thumbnail_portrait_flag: str = ""
    schedule_flag: str = ""
    collection_flag: str = ""
    playlist_flag: str = ""
    visibility_flag: str = ""
    tid_flag: str = ""
    tid_required: bool = False
    interactive_login: bool = False

    @property
    def actions(self) -> tuple[str, ...]:
        actions: list[str] = []
        if self.supports_login:
            actions.append("login")
        if self.supports_check:
            actions.append("check")
        if self.supports_upload_video:
            actions.append("upload-video")
        return tuple(actions)

    @property
    def supports_dual_thumbnail(self) -> bool:
        return bool(self.thumbnail_landscape_flag and self.thumbnail_portrait_flag)

    @property
    def supports_schedule(self) -> bool:
        return bool(self.schedule_flag)

    @property
    def supports_collection(self) -> bool:
        return bool(self.collection_flag)

    @property
    def supports_playlist(self) -> bool:
        return bool(self.playlist_flag)

    @property
    def supports_visibility(self) -> bool:
        return bool(self.visibility_flag)

    @property
    def requires_tid(self) -> bool:
        return bool(self.tid_flag and self.tid_required)


SAU_PLATFORM_CAPABILITIES: dict[str, SauPlatformCapability] = {
    "douyin": SauPlatformCapability(
        "douyin", "抖音",
        thumbnail_landscape_flag="--thumbnail-landscape",
        thumbnail_portrait_flag="--thumbnail-portrait",
        schedule_flag="--schedule",
        collection_flag="--collection",
    ),
    "kuaishou": SauPlatformCapability(
        "kuaishou", "快手", schedule_flag="--schedule", collection_flag="--collection"
    ),
    "xiaohongshu": SauPlatformCapability("xiaohongshu", "小红书", schedule_flag="--schedule"),
    "bilibili": SauPlatformCapability(
        "bilibili", "哔哩哔哩", schedule_flag="--schedule",
        tid_flag="--tid", tid_required=True, interactive_login=True,
    ),
    "tencent": SauPlatformCapability(
        "tencent", "视频号",
        thumbnail_landscape_flag="--thumbnail-landscape",
        thumbnail_portrait_flag="--thumbnail-portrait",
        schedule_flag="--schedule",
        collection_flag="--collection",
    ),
    "baijiahao": SauPlatformCapability("baijiahao", "百家号", collection_flag="--collection"),
    "alipay": SauPlatformCapability("alipay", "支付宝生活号", collection_flag="--collection"),
    "weibo": SauPlatformCapability("weibo", "微博", collection_flag="--collection"),
    "hupu": SauPlatformCapability("hupu", "虎扑"),
    "youtube": SauPlatformCapability(
        "youtube", "YouTube", playlist_flag="--playlist", visibility_flag="--visibility"
    ),
}

SAU_SUPPORTED_PLATFORMS = tuple(SAU_PLATFORM_CAPABILITIES)
SAU_PLATFORM_DISPLAY_NAMES = {
    name: capability.display_name for name, capability in SAU_PLATFORM_CAPABILITIES.items()
}
COMMANDS = {name: capability.command for name, capability in SAU_PLATFORM_CAPABILITIES.items()}
ACCOUNT_ACTIONS = frozenset({"login", "check"})
UPLOAD_VIDEO_ACTION = "upload-video"
YOUTUBE_VISIBILITY = {
    "": "public", "public": "public", "公开": "public",
    "unlisted": "unlisted", "不公开": "unlisted", "链接可见": "unlisted",
    "private": "private", "仅自己可见": "private", "私密/草稿": "private",
}


def get_sau_platform_capability(platform: str) -> SauPlatformCapability | None:
    return SAU_PLATFORM_CAPABILITIES.get(str(platform or "").strip().casefold())


def resolve_sau_executable() -> str:
    try:
        return str(vendor_root() / "sau_cli.py")
    except Exception:
        return ""


def _redact_output(value: str) -> str:
    return redact_secret_text(
        value,
        replacement="<redacted>",
        redact_urls=True,
    )[-8000:]


@dataclass(frozen=True, slots=True)
class SauCoreCompatibility:
    compatible: bool
    executable: str
    platform: str = ""
    action: str = ""
    problems: tuple[str, ...] = ()
    checked_platforms: tuple[str, ...] = ()

    def user_message(self) -> str:
        if self.compatible:
            if self.platform and self.action:
                return f"内置发布核心可用：{self.platform} / {self.action}"
            return f"内置发布核心可用：{len(self.checked_platforms)} 个平台"
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
    elif action_key not in capability.actions:
        problems.append(f"{capability.display_name} 不支持 {action_key or '该操作'}")
    return SauCoreCompatibility(
        not problems,
        resolve_sau_executable(),
        platform_key,
        action_key,
        tuple(problems),
    )


class SauAdapter:
    def __init__(self, platform: str):
        self.name = str(platform or "").strip().casefold()
        self.executable = resolve_sau_executable()

    def build_payload(
        self, media: dict[str, Any], metadata: dict[str, Any], settings: dict[str, Any]
    ) -> dict[str, Any]:
        return {"media": media, "metadata": metadata, "settings": settings}

    def core_compatibility(self, action: str) -> SauCoreCompatibility:
        return probe_sau_compatibility(self.name, action)

    @staticmethod
    def _text(settings: dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = settings.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    @staticmethod
    def _tags(value: Any) -> str:
        if isinstance(value, (list, tuple, set)):
            return ",".join(
                item for item in (str(raw).strip().lstrip("#") for raw in value) if item
            )
        return str(value or "").strip()

    def build_upload_command(self, payload: dict[str, Any]) -> list[str]:
        capability = get_sau_platform_capability(self.name)
        if capability is None or not capability.supports_upload_video:
            raise ValueError(f"平台 {self.name or '未知'} 尚未接入视频发布")
        media = payload.get("media") or {}
        metadata = payload.get("metadata") or {}
        settings = payload.get("settings") or {}
        video = str(media.get("video_path") or "").strip()
        title = str(
            settings.get("title") or metadata.get("title") or (Path(video).stem if video else "")
        ).strip()
        description = str(settings.get("description") or metadata.get("description") or "")
        tags = self._tags(settings.get("topics") or metadata.get("tags"))
        account = str(settings.get("account") or "default").strip() or "default"
        args = [
            self.executable or "embedded-social-auto-upload",
            capability.command, UPLOAD_VIDEO_ACTION,
            "--account", account, "--file", video, "--title", title, "--desc", description,
        ]
        if tags:
            args += ["--tags", tags]

        portrait = self._text(settings, "thumbnail_portrait", "thumbnail_portrait_path")
        landscape = self._text(settings, "thumbnail_landscape", "thumbnail_landscape_path")
        explicit = self._text(settings, "thumbnail", "thumbnail_path")
        media_thumbnail = str(media.get("thumbnail_path") or "").strip()
        if capability.supports_dual_thumbnail:
            thumbnail = explicit or (media_thumbnail if not portrait else "")
        else:
            thumbnail = explicit or portrait or landscape or media_thumbnail
        if thumbnail and capability.thumbnail_flag:
            args += [capability.thumbnail_flag, thumbnail]
        if landscape and capability.thumbnail_landscape_flag:
            args += [capability.thumbnail_landscape_flag, landscape]
        if portrait and capability.thumbnail_portrait_flag:
            args += [capability.thumbnail_portrait_flag, portrait]

        collection = self._text(settings, "collection", "collection_name")
        if collection and capability.collection_flag:
            args += [capability.collection_flag, collection]
        schedule = self._text(settings, "schedule", "publish_at", "scheduled_at")
        if schedule:
            if not capability.schedule_flag:
                raise ValueError(f"{capability.display_name} 不支持定时发布，请清空发布时间后重试")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", schedule):
                raise ValueError("定时发布时间格式必须为 YYYY-MM-DD HH:MM")
            try:
                datetime.strptime(schedule, "%Y-%m-%d %H:%M")
            except ValueError as exc:
                raise ValueError("定时发布时间不是有效日期，请使用 YYYY-MM-DD HH:MM") from exc
            args += [capability.schedule_flag, schedule]

        tid = self._text(settings, "tid", "partition")
        if capability.requires_tid and not tid:
            raise ValueError("Bilibili 发布必须填写数字分区 ID（tid）")
        if tid and capability.tid_flag:
            if not tid.isdigit() or int(tid) <= 0:
                raise ValueError("Bilibili 发布必须填写有效的数字分区 ID（tid）")
            args += [capability.tid_flag, str(int(tid))]
        if capability.playlist_flag:
            playlist = self._text(settings, "playlist", "collection", "collection_name")
            if playlist:
                args += [capability.playlist_flag, playlist]
        if capability.visibility_flag:
            raw_visibility = str(settings.get("visibility") or "").strip()
            visibility = YOUTUBE_VISIBILITY.get(raw_visibility.casefold())
            if visibility is None:
                raise ValueError("YouTube 可见范围仅支持 public、unlisted 或 private")
            args += [capability.visibility_flag, visibility]
        return args

    def account_action(
        self,
        action: str,
        account: str,
        cancel_event: threading.Event | None = None,
    ) -> tuple[bool, str]:
        action_key = str(action or "").strip().casefold()
        account_name = str(account or "").strip()
        if action_key not in ACCOUNT_ACTIONS:
            return False, "不支持的账号操作"
        if not account_name:
            return False, "请先填写发布账号名"
        compatibility = self.core_compatibility(action_key)
        if not compatibility.compatible:
            return False, compatibility.user_message()
        try:
            if action_key == "check":
                ok = account_check(self.name, account_name, cancel_event=cancel_event)
                return ok, "Cookie 有效" if ok else "Cookie 无效、缺失或已过期"
            result = account_login(
                self.name, account_name, headed=True, cancel_event=cancel_event
            )
            ok = bool(result.get("success"))
            message = str(result.get("message") or ("登录完成" if ok else "登录失败"))
            return ok, _redact_output(message)
        except InterruptedError:
            return False, "登录任务已取消"
        except Exception as exc:
            return False, _redact_output(str(exc))

    def publish(
        self,
        payload: dict[str, Any],
        cancel_event: threading.Event | None = None,
    ) -> tuple[bool, str]:
        media = payload.get("media") or {}
        video = str(media.get("video_path") or "").strip()
        if not video or not Path(video).is_file():
            return False, f"视频文件不存在：{video or '未设置'}"
        compatibility = self.core_compatibility(UPLOAD_VIDEO_ACTION)
        if not compatibility.compatible:
            return False, compatibility.user_message()
        try:
            result = publish_video(self.build_upload_command(payload), cancel_event=cancel_event)
            return True, result
        except InterruptedError:
            return False, "发布任务已取消"
        except Exception as exc:
            return False, _redact_output(str(exc))
