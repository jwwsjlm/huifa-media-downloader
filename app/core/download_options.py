from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
from typing import Any, Mapping

from app.core.download_progress import non_negative_float


AUDIO_FORMATS = frozenset({"best", "aac", "alac", "flac", "m4a", "mp3", "opus", "vorbis", "wav"})
AUDIO_TRACKS = (
    "default", "original", "all", "zh-Hans", "zh-Hant", "en", "ja", "ko",
    "es", "fr", "de", "ru", "pt", "ar", "hi", "id", "vi", "th", "tr",
)
_AUDIO_TRACK_LOOKUP = {item.casefold(): item for item in AUDIO_TRACKS}
CONTAINERS = frozenset({"auto", "mp4", "mkv"})
VIDEO_FPS_CHOICES = frozenset({"best", "240", "120", "60", "50", "30", "25", "24"})
SOURCE_VIDEO_CODECS = frozenset({"auto", "h264", "h265", "av1", "vp9"})
VR_MODES = frozenset({"any", "2d360", "3d180", "3d360", "none"})
COMPATIBILITY_TARGETS = frozenset({"auto", "windows", "macos", "linux", "ios", "android"})
QUALITY_HEIGHTS = {
    "8k": 4320,
    "4k": 2160,
    "2k": 1440,
    "1k": 1080,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
    "240p": 240,
    "qcif": 144,
}
COLLECTION_MODES = frozenset({"select", "single", "all"})
COLLECTION_ORDERS = frozenset({"original", "reverse", "random"})
LIVE_FILTERS = frozenset({"videos", "all", "live"})
SUBTITLE_FORMATS = frozenset({"best", "vtt", "srt", "ass", "lrc"})
SPONSORBLOCK_MODES = frozenset({"off", "mark", "remove"})
SPONSORBLOCK_CATEGORIES = frozenset({
    "sponsor", "intro", "outro", "selfpromo", "interaction", "preview",
    "music_offtopic", "filler",
})


def _choice(value: object, allowed: frozenset[str], default: str) -> str:
    if type(value) is str and value in allowed:
        return value
    normalized = str(value or "").strip().casefold()
    return normalized if normalized in allowed else default


def _audio_track(value: object) -> str:
    """Return a bounded yt-dlp audio-language preference."""

    return _AUDIO_TRACK_LOOKUP.get(str(value or "").strip().casefold(), "default")


def _integer(value: object, default: int = 0, minimum: int = 0, maximum: int = 2**31 - 1) -> int:
    try:
        number = int(value or default)
    except (TypeError, ValueError, OverflowError):
        number = default
    return max(minimum, min(number, maximum))


def _boolean(value: object, default: bool = False) -> bool:
    """Normalize persisted JSON/settings values without treating ``"false"`` as true."""

    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return default
        return value != 0
    normalized = str(value).strip().casefold()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    return default


def _rate_limit(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text[:32]


def _rate_limit_bytes(value: str) -> int | None:
    match = re.fullmatch(r"(?i)\s*(\d+(?:\.\d+)?)\s*([kmgtpe]?)(?:i?b)?\s*", value)
    if not match:
        return None
    power = "kmgtpe".find(match.group(2).casefold()) + 1 if match.group(2) else 0
    return int(float(match.group(1)) * (1024 ** power))


@dataclass(slots=True)
class DownloadOptions:
    """Bounded, serializable yt-dlp feature set used by both core modes."""

    content_mode: str = "video"
    audio_format: str = "best"
    audio_track: str = "default"
    container: str = "auto"
    video_fps: str = "best"
    source_video_codec: str = "auto"
    vr_mode: str = "any"
    compatibility_target: str = "auto"
    collection_mode: str = "select"
    collection_order: str = "original"
    first_n: int = 0
    playlist_items: str = ""
    date_after: str = ""
    date_before: str = ""
    duration_min: int = 0
    duration_max: int = 0
    live_filter: str = "videos"
    live_from_start: bool = False
    wait_for_live: bool = False
    wait_min: int = 60
    wait_max: int = 300
    section_start: str = ""
    section_end: str = ""
    split_chapters: bool = False
    subtitle_format: str = "best"
    embed_subtitles: bool = False
    write_thumbnail: bool = True
    write_description: bool = False
    write_comments: bool = False
    write_info_json: bool = True
    embed_metadata: bool = False
    embed_chapters: bool = False
    embed_thumbnail: bool = False
    sponsorblock_mode: str = "off"
    sponsorblock_categories: list[str] = field(default_factory=list)
    rate_limit: str = ""
    organize_task_folder: bool = False
    processing_temp_dir: str = ""
    prepend_cover_enabled: bool = False
    prepend_cover_frames: int = 3

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "DownloadOptions":
        raw = dict(value or {})
        categories = [
            str(item).strip().casefold()
            for item in (raw.get("sponsorblock_categories") or [])
            if str(item).strip().casefold() in SPONSORBLOCK_CATEGORIES
        ]
        wait_min = _integer(raw.get("wait_min"), 60, 1, 86400)
        wait_max = _integer(raw.get("wait_max"), 300, wait_min, 86400)
        return cls(
            content_mode=_choice(raw.get("content_mode"), frozenset({"manual", "video", "audio"}), "video"),
            audio_format=_choice(raw.get("audio_format"), AUDIO_FORMATS, "best"),
            audio_track=_audio_track(raw.get("audio_track")),
            container=_choice(raw.get("container"), CONTAINERS, "auto"),
            video_fps=_choice(raw.get("video_fps"), VIDEO_FPS_CHOICES, "best"),
            source_video_codec=_choice(
                raw.get("source_video_codec"), SOURCE_VIDEO_CODECS, "auto"
            ),
            vr_mode=_choice(raw.get("vr_mode"), VR_MODES, "any"),
            compatibility_target=_choice(
                raw.get("compatibility_target"), COMPATIBILITY_TARGETS, "auto"
            ),
            collection_mode=_choice(raw.get("collection_mode"), COLLECTION_MODES, "select"),
            collection_order=_choice(raw.get("collection_order"), COLLECTION_ORDERS, "original"),
            first_n=_integer(raw.get("first_n"), 0, 0, 1_000_000),
            playlist_items=str(raw.get("playlist_items") or "").strip()[:200],
            date_after=str(raw.get("date_after") or "").strip()[:10],
            date_before=str(raw.get("date_before") or "").strip()[:10],
            duration_min=_integer(raw.get("duration_min"), 0, 0, 31_536_000),
            duration_max=_integer(raw.get("duration_max"), 0, 0, 31_536_000),
            live_filter=_choice(raw.get("live_filter"), LIVE_FILTERS, "videos"),
            live_from_start=_boolean(raw.get("live_from_start")),
            wait_for_live=_boolean(raw.get("wait_for_live")),
            wait_min=wait_min,
            wait_max=wait_max,
            section_start=str(raw.get("section_start") or "").strip()[:24],
            section_end=str(raw.get("section_end") or "").strip()[:24],
            split_chapters=_boolean(raw.get("split_chapters")),
            subtitle_format=_choice(raw.get("subtitle_format"), SUBTITLE_FORMATS, "best"),
            embed_subtitles=_boolean(raw.get("embed_subtitles")),
            write_thumbnail=_boolean(raw.get("write_thumbnail"), True),
            write_description=_boolean(raw.get("write_description")),
            write_comments=_boolean(raw.get("write_comments")),
            write_info_json=_boolean(raw.get("write_info_json"), True),
            embed_metadata=_boolean(raw.get("embed_metadata")),
            embed_chapters=_boolean(raw.get("embed_chapters")),
            embed_thumbnail=_boolean(raw.get("embed_thumbnail")),
            sponsorblock_mode=_choice(raw.get("sponsorblock_mode"), SPONSORBLOCK_MODES, "off"),
            sponsorblock_categories=list(dict.fromkeys(categories)),
            rate_limit=_rate_limit(raw.get("rate_limit")),
            organize_task_folder=_boolean(raw.get("organize_task_folder")),
            processing_temp_dir=str(raw.get("processing_temp_dir") or "").strip(),
            prepend_cover_enabled=_boolean(raw.get("prepend_cover_enabled")),
            prepend_cover_frames=_integer(raw.get("prepend_cover_frames"), 3, 1, 300),
        )

    def to_dict(self) -> dict[str, Any]:
        values = {name: getattr(self, name) for name in self.__slots__}
        values["sponsorblock_categories"] = list(self.sponsorblock_categories)
        return values

    def effective_container(self) -> str:
        """Return the explicit container or the selected compatibility preset."""

        if self.container != "auto":
            return self.container
        if self.compatibility_target == "linux":
            return "mkv"
        if self.compatibility_target in {"windows", "macos", "ios", "android"}:
            return "mp4"
        return "auto"

    def effective_video_codec(self) -> str:
        """Return the preferred source codec after applying a device preset."""

        if self.source_video_codec != "auto":
            return self.source_video_codec
        if self.compatibility_target in {"windows", "macos", "ios", "android"}:
            return "h264"
        return "auto"

    def format_sort(self, quality: str = "best") -> list[str]:
        """Keep resolution primary while applying optional FPS/codec preferences."""

        normalized_quality = str(quality or "best").strip().casefold()
        height = QUALITY_HEIGHTS.get(normalized_quality)
        sort = [f"res:{height}" if height else "res"]
        if self.video_fps != "best":
            sort.append(f"fps:{self.video_fps}")
        codec = self.effective_video_codec()
        if codec != "auto":
            sort.append(f"vcodec:{codec}")
        if self.compatibility_target in {"windows", "macos", "ios", "android"}:
            sort.append("acodec:m4a")
        return sort

    def video_format_selector(self, quality: str = "best") -> str:
        """Build a site-neutral video selector with a bounded resolution cap."""

        height = QUALITY_HEIGHTS.get(str(quality or "best").strip().casefold())
        video = f"bv*[height<={height}]" if height else "bv*"
        combined = f"b[height<={height}]" if height else "b"
        preferred_video = f"{video}{self._vr_format_filter()}"

        def with_audio(video_selector: str) -> str:
            if self.audio_track == "all":
                return f"{video_selector}+mergeall[vcodec=none]"
            audio = self._preferred_audio_selector()
            return (
                f"{video_selector}+({audio})"
                if "/" in audio
                else f"{video_selector}+{audio}"
            )

        selectors = [with_audio(preferred_video)]
        if preferred_video != video:
            # VR classification is not standardized across extractors. Treat
            # the chosen type as a preference and retain the normal selector
            # as a site-neutral fallback instead of failing the task.
            selectors.append(with_audio(video))
        selectors.append(combined)
        return "/".join(selectors)

    def _vr_format_filter(self) -> str:
        filters = {
            "2d360": "[format_note~='(?i)360'][format_note!~='(?i)(?:3d|vr)']",
            "3d180": "[format_note~='(?i)(?=.*(?:3d|vr))(?=.*180)']",
            "3d360": "[format_note~='(?i)(?=.*(?:3d|vr))(?=.*360)']",
            "none": "[format_note!~='(?i)(?:3d|vr|180|360)']",
        }
        return filters.get(self.vr_mode, "")

    def _preferred_audio_selector(self) -> str:
        """Select an audio stream by preference and retain a safe fallback."""

        if self.audio_track == "original":
            return "ba[format_note*=original]/ba"
        if self.audio_track not in {"default", "all"}:
            return f"ba[language^={self.audio_track}]/ba"
        return "ba"

    def audio_format_selector(self) -> str:
        """Build the audio-only selector using the same language preference."""

        if self.audio_track == "default":
            return "bestaudio/best"
        if self.audio_track == "all":
            return "mergeall[vcodec=none]/ba/b"
        preferred = self._preferred_audio_selector()
        return f"{preferred}/b"

    def _base_ytdlp_options(self) -> dict[str, Any]:
        return {
            "writethumbnail": self.write_thumbnail,
            "writedescription": self.write_description,
            "getcomments": self.write_comments,
            "writeinfojson": self.write_info_json,
            "subtitlesformat": self.subtitle_format,
        }

    def _media_ytdlp_options(
        self,
        *,
        video_mode: bool,
        effective_container: str,
    ) -> dict[str, Any]:
        options: dict[str, Any] = {}
        if video_mode:
            options["format_sort"] = self.format_sort()
            if effective_container != "auto":
                options["merge_output_format"] = effective_container
        else:
            options["format"] = self.audio_format_selector()
        if self.audio_track == "all":
            options["allow_multiple_audio_streams"] = True
        return options

    def _collection_ytdlp_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {}
        if self.collection_order == "reverse":
            options["playlistreverse"] = True
        elif self.collection_order == "random":
            options["playlistrandom"] = True
        if self.first_n:
            options["playlistend"] = self.first_n
        if self.playlist_items:
            options["playlist_items"] = self.playlist_items
        if self.date_after:
            options["dateafter"] = self.date_after.replace("-", "")
        if self.date_before:
            options["datebefore"] = self.date_before.replace("-", "")
        return options

    def _live_and_section_ytdlp_options(self) -> dict[str, Any]:
        options: dict[str, Any] = {}
        if self.live_from_start:
            options["live_from_start"] = True
        if self.wait_for_live:
            options["wait_for_video"] = (self.wait_min, self.wait_max)
        if self.section_start or self.section_end:
            start = self.section_start or "0"
            end = self.section_end or "inf"
            options["download_sections"] = [f"*{start}-{end}"]
        return options

    def _rate_limit_ytdlp_options(self) -> dict[str, Any]:
        if not self.rate_limit:
            return {}
        options: dict[str, Any] = {"ratelimit_text": self.rate_limit}
        parsed_rate = _rate_limit_bytes(self.rate_limit)
        if parsed_rate:
            options["ratelimit"] = parsed_rate
        return options

    def _container_postprocessors(
        self,
        *,
        video_mode: bool,
        effective_container: str,
    ) -> list[dict[str, Any]]:
        if not video_mode or effective_container == "auto":
            return []
        # ``merge_output_format`` only affects separate-stream merges. A
        # progressive source may already use another container, so remux it
        # explicitly without re-encoding the audio or video streams.
        return [{
            "key": "FFmpegVideoRemuxer",
            "preferedformat": effective_container,
        }]

    def _audio_postprocessors(self) -> list[dict[str, Any]]:
        if (
            self.content_mode != "audio"
            or self.audio_format == "best"
            or self.audio_track == "all"
        ):
            return []
        return [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": self.audio_format,
            "preferredquality": "0",
        }]

    def _metadata_postprocessors(self) -> list[dict[str, Any]]:
        processors: list[dict[str, Any]] = []
        if self.split_chapters:
            processors.append({"key": "FFmpegSplitChapters", "force_keyframes": False})
        if self.embed_metadata or self.embed_chapters:
            processors.append({
                "key": "FFmpegMetadata",
                "add_metadata": self.embed_metadata,
                "add_chapters": self.embed_chapters,
            })
        if self.embed_subtitles:
            processors.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False})
        if self.embed_thumbnail:
            processors.append({"key": "EmbedThumbnail", "already_have_thumbnail": False})
        return processors

    def _sponsorblock_postprocessors(self) -> list[dict[str, Any]]:
        if self.sponsorblock_mode == "off" or not self.sponsorblock_categories:
            return []
        categories = set(self.sponsorblock_categories)
        processors: list[dict[str, Any]] = [{
            "key": "SponsorBlock",
            "categories": categories,
            "api": "https://sponsor.ajay.app",
        }]
        if self.sponsorblock_mode == "remove":
            processors.append({
                "key": "ModifyChapters",
                "remove_sponsor_segments": categories,
                "force_keyframes": False,
            })
        return processors

    def _ytdlp_postprocessors(
        self,
        *,
        video_mode: bool,
        effective_container: str,
    ) -> list[dict[str, Any]]:
        return [
            *self._container_postprocessors(
                video_mode=video_mode,
                effective_container=effective_container,
            ),
            *self._audio_postprocessors(),
            *self._metadata_postprocessors(),
            *self._sponsorblock_postprocessors(),
        ]

    def ytdlp_options(self) -> dict[str, Any]:
        """Return options understood by YoutubeDL's Python API.

        CLI-only spelling is handled by ``external_ytdlp``. Keeping the same
        normalized keys here lets equivalence tests compare one snapshot.
        """

        effective_container = self.effective_container()
        video_mode = self.content_mode != "audio"
        options = self._base_ytdlp_options()
        options.update(self._media_ytdlp_options(
            video_mode=video_mode,
            effective_container=effective_container,
        ))
        options.update(self._collection_ytdlp_options())
        options.update(self._live_and_section_ytdlp_options())
        options.update(self._rate_limit_ytdlp_options())

        postprocessors = self._ytdlp_postprocessors(
            video_mode=video_mode,
            effective_container=effective_container,
        )
        if postprocessors:
            options["postprocessors"] = postprocessors
        return options

    def collection_match_filter(self, entry: Mapping[str, Any]) -> tuple[bool, str]:
        """Apply UI-side filters while flat playlist entries are streamed."""

        live_status = str(entry.get("live_status") or "").casefold()
        is_live = bool(entry.get("is_live")) or live_status in {
            "is_live", "is_upcoming", "post_live", "was_live",
        }
        if self.live_filter == "videos" and is_live:
            return False, "live_filtered"
        if self.live_filter == "live" and not is_live:
            return False, "video_filtered"
        duration_value = non_negative_float(entry.get("duration"))
        if self.duration_min and duration_value and duration_value < self.duration_min:
            return False, "duration_short"
        if self.duration_max and duration_value and duration_value > self.duration_max:
            return False, "duration_long"
        upload_date = str(entry.get("upload_date") or entry.get("release_date") or "")
        normalized_date = upload_date.replace("-", "")[:8]
        if self.date_after and normalized_date and normalized_date < self.date_after.replace("-", ""):
            return False, "date_old"
        if self.date_before and normalized_date and normalized_date > self.date_before.replace("-", ""):
            return False, "date_new"
        return True, ""
