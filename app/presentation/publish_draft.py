from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import Property, QObject, Signal, Slot

from app.adapters.sau_adapter import SAU_SUPPORTED_PLATFORMS, get_sau_platform_capability
from app.storage.models import MediaItem
from app.presentation.distribution_plan import (
    distribution_preselected_platforms,
    distribution_target_platforms,
)
from app.presentation.media_text import platform_label


class PublishDraft(QObject):
    """One editable completed-media publish request for the QML dialog."""

    changed = Signal()
    platformsChanged = Signal()
    stateChanged = Signal()

    def __init__(self, database: Any, settings: Any, publish_service: Any, parent=None) -> None:
        super().__init__(parent)
        self._database = database
        self._settings = settings
        self._publish_service = publish_service
        self._media: MediaItem | None = None
        self._platforms: list[dict[str, Any]] = []
        self._title = ""
        self._description = ""
        self._topics = ""
        self._source_type = "original"
        self._visibility = "public"
        self._collection = ""
        self._playlist = ""
        self._partition = ""
        self._schedule = ""
        self._thumbnail = ""
        self._thumbnail_landscape = ""
        self._thumbnail_portrait = ""
        self._last_message = ""

    @Property(bool, notify=stateChanged)
    def active(self) -> bool:
        return self._media is not None

    @Property(int, notify=stateChanged)
    def mediaId(self) -> int:
        return int(self._media.id or 0) if self._media is not None else 0

    @Property(str, notify=stateChanged)
    def mediaTitle(self) -> str:
        return self._media.title if self._media is not None else ""

    @Property(str, notify=stateChanged)
    def sourceUrl(self) -> str:
        return self._media.source_url if self._media is not None else ""

    @Property(str, notify=stateChanged)
    def sourceIp(self) -> str:
        return self._media.source_ip if self._media is not None else ""

    @Property(str, notify=changed)
    def title(self) -> str:
        return self._title

    @title.setter
    def title(self, value: str) -> None:
        self._set_field("_title", value)

    @Property(str, notify=changed)
    def description(self) -> str:
        return self._description

    @description.setter
    def description(self, value: str) -> None:
        self._set_field("_description", value)

    @Property(str, notify=changed)
    def topics(self) -> str:
        return self._topics

    @topics.setter
    def topics(self, value: str) -> None:
        self._set_field("_topics", value)

    @Property(str, notify=changed)
    def sourceType(self) -> str:
        return self._source_type

    @sourceType.setter
    def sourceType(self, value: str) -> None:
        self._set_field("_source_type", value if value in {"original", "repost"} else "original")

    @Property(str, notify=changed)
    def visibility(self) -> str:
        return self._visibility

    @visibility.setter
    def visibility(self, value: str) -> None:
        self._set_field("_visibility", value if value in {"public", "unlisted", "private"} else "public")

    @Property(str, notify=changed)
    def collection(self) -> str:
        return self._collection

    @collection.setter
    def collection(self, value: str) -> None:
        self._set_field("_collection", value)

    @Property(str, notify=changed)
    def playlist(self) -> str:
        return self._playlist

    @playlist.setter
    def playlist(self, value: str) -> None:
        self._set_field("_playlist", value)

    @Property(str, notify=changed)
    def partition(self) -> str:
        return self._partition

    @partition.setter
    def partition(self, value: str) -> None:
        self._set_field("_partition", value)

    @Property(str, notify=changed)
    def schedule(self) -> str:
        return self._schedule

    @schedule.setter
    def schedule(self, value: str) -> None:
        self._set_field("_schedule", value)

    @Property(str, notify=changed)
    def thumbnail(self) -> str:
        return self._thumbnail

    @thumbnail.setter
    def thumbnail(self, value: str) -> None:
        self._set_field("_thumbnail", value)

    @Property(str, notify=changed)
    def thumbnailLandscape(self) -> str:
        return self._thumbnail_landscape

    @thumbnailLandscape.setter
    def thumbnailLandscape(self, value: str) -> None:
        self._set_field("_thumbnail_landscape", value)

    @Property(str, notify=changed)
    def thumbnailPortrait(self) -> str:
        return self._thumbnail_portrait

    @thumbnailPortrait.setter
    def thumbnailPortrait(self, value: str) -> None:
        self._set_field("_thumbnail_portrait", value)

    @Property("QVariantList", notify=platformsChanged)
    def platforms(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._platforms]

    @Property(str, notify=stateChanged)
    def lastMessage(self) -> str:
        return self._last_message

    @Property(bool, notify=platformsChanged)
    def supportsVisibility(self) -> bool:
        return self._any_selected_capability("supportsVisibility")

    @Property(bool, notify=platformsChanged)
    def supportsCollection(self) -> bool:
        return self._any_selected_capability("supportsCollection")

    @Property(bool, notify=platformsChanged)
    def supportsPlaylist(self) -> bool:
        return self._any_selected_capability("supportsPlaylist")

    @Property(bool, notify=platformsChanged)
    def requiresPartition(self) -> bool:
        return self._any_selected_capability("requiresPartition")

    @Property(bool, notify=platformsChanged)
    def supportsSchedule(self) -> bool:
        return self._any_selected_capability("supportsSchedule")

    @Property(bool, notify=platformsChanged)
    def supportsDualThumbnail(self) -> bool:
        return self._any_selected_capability("supportsDualThumbnail")

    @Slot(int, result=bool)
    def openMedia(self, media_id: int) -> bool:
        media = self._database.get_media(int(media_id or 0))
        if media is None:
            self._set_message("找不到已完成媒体")
            return False
        target_platforms = distribution_target_platforms(
            self._settings.get("publish_target_platforms")
        )
        existing = self._database.publish_statuses_for_media(int(media.id or 0))
        selected = set(distribution_preselected_platforms(existing, target_platforms))
        self._media = media
        self._title = str(media.title or "")
        self._description = str(media.description or "")
        self._topics = " ".join(f"#{tag}" for tag in media.tags if str(tag).strip())
        self._source_type = "original"
        self._visibility = "public"
        self._collection = ""
        self._playlist = ""
        self._partition = ""
        self._schedule = ""
        self._thumbnail = str(media.thumbnail_path or "")
        self._thumbnail_landscape = ""
        self._thumbnail_portrait = ""
        self._platforms = [
            self._platform_row(platform, platform in selected)
            for platform in SAU_SUPPORTED_PLATFORMS
        ]
        self._set_message("")
        self.stateChanged.emit()
        self.changed.emit()
        self.platformsChanged.emit()
        return True

    @Slot()
    def close(self) -> None:
        if self._media is None:
            return
        self._media = None
        self._platforms = []
        self._set_message("")
        self.stateChanged.emit()
        self.platformsChanged.emit()

    @Slot(str, bool)
    def setPlatformSelected(self, platform: str, selected: bool) -> None:
        row = self._platform(platform)
        if row is None or bool(row["selected"]) == bool(selected):
            return
        row["selected"] = bool(selected)
        self.platformsChanged.emit()

    @Slot(str, str)
    def setPlatformAccount(self, platform: str, account: str) -> None:
        row = self._platform(platform)
        if row is None:
            return
        value = str(account or "").strip() or "default"
        if value == row["account"]:
            return
        row["account"] = value
        self.platformsChanged.emit()

    @Slot(result=bool)
    def submit(self) -> bool:
        media = self._media
        if media is None:
            self._set_message("请先选择一个已完成媒体")
            return False
        selected = [row for row in self._platforms if bool(row["selected"])]
        if not selected:
            self._set_message("请至少选择一个发布平台")
            return False
        if any(not str(row["account"] or "").strip() for row in selected):
            self._set_message("请填写所选平台的账号名称")
            return False
        if self.requiresPartition and (not self._partition.isdigit() or int(self._partition) <= 0):
            self._set_message("Bilibili 发布需要填写有效的数字分区 ID（tid）")
            return False
        schedule = self._schedule.strip()
        if schedule and self.supportsSchedule:
            try:
                datetime.strptime(schedule, "%Y-%m-%d %H:%M")
            except ValueError:
                self._set_message("定时发布时间格式必须为 YYYY-MM-DD HH:MM")
                return False
        if schedule and not self.supportsSchedule:
            schedule = ""
        platform_settings = {
            str(row["platform"]): self._platform_settings(row, schedule)
            for row in selected
        }
        metadata = {
            "title": self._title.strip() or media.title,
            "description": self._description,
            "tags": [item.lstrip("#") for item in self._topics.split() if item.strip()],
        }
        try:
            self._publish_service.create_tasks(
                media,
                list(platform_settings),
                metadata,
                platform_settings,
            )
        except Exception as exc:
            self._set_message(f"无法创建发布任务：{exc}")
            return False
        created = int(getattr(self._publish_service, "last_created_count", 0) or 0)
        existing = int(getattr(self._publish_service, "last_existing_count", 0) or 0)
        message = f"已创建 {created} 个发布任务"
        if existing:
            message += f"，保留 {existing} 个已有任务"
        try:
            self._settings.set_many({
                f"publish_account/{row['platform']}": str(row["account"])
                for row in selected
            })
        except Exception as exc:
            message += f"；账号偏好未保存：{exc}"
        self._set_message(message)
        return True

    def _platform_row(self, platform: str, selected: bool) -> dict[str, Any]:
        capability = get_sau_platform_capability(platform)
        return {
            "platform": platform,
            "label": platform_label(platform),
            "account": str(self._settings.get(f"publish_account/{platform}") or "default").strip() or "default",
            "selected": bool(selected),
            "supportsVisibility": bool(capability and capability.supports_visibility),
            "supportsCollection": bool(capability and capability.supports_collection),
            "supportsPlaylist": bool(capability and capability.supports_playlist),
            "requiresPartition": bool(capability and capability.tid_required),
            "supportsSchedule": bool(capability and capability.supports_schedule),
            "supportsDualThumbnail": bool(capability and capability.supports_dual_thumbnail),
        }

    def _platform(self, platform: str) -> dict[str, Any] | None:
        target = str(platform or "").strip().casefold()
        return next((row for row in self._platforms if row["platform"] == target), None)

    def _platform_settings(self, row: dict[str, Any], schedule: str) -> dict[str, object]:
        settings: dict[str, object] = {
            "source_type": self._source_type,
            "source_url": self.sourceUrl,
            "source_ip": self.sourceIp,
            "account": str(row["account"]),
            "thumbnail": self._thumbnail.strip(),
        }
        if row["supportsVisibility"]:
            settings["visibility"] = self._visibility
        if row["supportsCollection"]:
            settings["collection"] = self._collection.strip()
        if row["supportsPlaylist"]:
            settings["playlist"] = self._playlist.strip()
        if row["requiresPartition"]:
            settings["partition"] = self._partition.strip()
        if row["supportsSchedule"]:
            settings["schedule"] = schedule
        if row["supportsDualThumbnail"]:
            settings["thumbnail_landscape"] = self._thumbnail_landscape.strip()
            settings["thumbnail_portrait"] = self._thumbnail_portrait.strip()
        return settings

    def _any_selected_capability(self, name: str) -> bool:
        return any(bool(row["selected"]) and bool(row.get(name)) for row in self._platforms)

    def _set_field(self, name: str, value: str) -> None:
        value = str(value or "")
        if value == getattr(self, name):
            return
        setattr(self, name, value)
        self.changed.emit()

    def setDatabase(self, database: Any) -> None:
        if database is self._database:
            return
        self.close()
        self._database = database

    def _set_message(self, value: str) -> None:
        value = str(value or "")
        if value == self._last_message:
            return
        self._last_message = value
        self.stateChanged.emit()
