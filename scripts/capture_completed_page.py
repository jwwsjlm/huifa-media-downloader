from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", os.environ.get("HUIFA_QT_PLATFORM", "windows"))

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtGui import QColor, QImage

from app.storage.models import MediaItem
from app.ui.completed_page import (
    FILTER_ALL,
    FILTER_COMPLETE,
    FILTER_NEEDS_DISTRIBUTION,
    FILTER_PUBLISHED,
    FILTER_QUEUED,
    FILTER_RETRY_NEEDED,
    CompletedPage,
)
from app.ui.i18n import apply_runtime_translation
from app.ui.runtime import create_application
from app.ui.theme import THEME_LIGHT, build_application_stylesheet


class FakeSettings:
    def get(self, key: str) -> str:
        return "douyin,bilibili,youtube" if key == "publish_target_platforms" else ""


class FakeDatabase:
    def __init__(self, cover_path: str) -> None:
        self.media = [
            MediaItem(
                id=index,
                title=(
                    "用于检查长标题省略、按钮对齐与完成列表卡片布局的示例视频 "
                    f"#{index}"
                ),
                uploader=f"示例作者 {index}",
                source_url=(
                    "https://www.youtube.com/watch?v=demo"
                    if index % 2
                    else "https://www.douyin.com/video/demo"
                ),
                thumbnail_path=cover_path,
                video_path=f"D:/视频归档/示例项目 {index}/这是一个较长的视频文件名 [{index}].mp4",
                downloaded_at="2026-08-26T20:00:00",
            )
            for index in range(1, 7)
        ]
        self.statuses = {
            2: {"douyin": "success"},
            3: {"youtube": "uploading"},
            4: {"douyin": "failed"},
            5: {"douyin": "success", "bilibili": "success", "youtube": "success"},
            6: {"douyin": "success", "bilibili": "pending"},
        }

    def count_media(self) -> int:
        return len(self.media)

    def list_media(self, limit=None, offset: int = 0):
        return list(self.media[offset:] if limit is None else self.media[offset:offset + limit])

    def publish_statuses_for_media_ids(self, media_ids):
        return {
            int(media_id): dict(self.statuses.get(int(media_id), {}))
            for media_id in media_ids
            if int(media_id) in self.statuses
        }

    def publish_statuses_for_media(self, media_id: int):
        return dict(self.statuses.get(int(media_id), {}))

    def publish_statuses_by_media(self):
        return {media_id: dict(states) for media_id, states in self.statuses.items()}

    def media_distribution_counts(self, _platforms):
        return {
            FILTER_ALL: 6,
            FILTER_NEEDS_DISTRIBUTION: 5,
            FILTER_PUBLISHED: 3,
            FILTER_QUEUED: 2,
            FILTER_RETRY_NEEDED: 1,
            FILTER_COMPLETE: 1,
        }

    def get_media(self, media_id: int):
        return next((media for media in self.media if int(media.id or 0) == int(media_id)), None)


def save_cover(path: Path) -> None:
    image = QImage(640, 360, QImage.Format_RGB32)
    image.fill(QColor("#2f7bdc"))
    image.save(str(path))


def capture(page: CompletedPage, app, path: Path, width: int, height: int) -> None:
    page.resize(width, height)
    page.show()
    page.refresh()
    while page._media_render_timer.isActive():
        app.processEvents()
    app.processEvents()
    page.grab().save(str(path))


def main() -> int:
    app, _font = create_application([])
    app.setStyleSheet(build_application_stylesheet(THEME_LIGHT))
    target_dir = ROOT / "data" / "temp" / "ui-review"
    target_dir.mkdir(parents=True, exist_ok=True)
    cover_path = target_dir / "completed-page-sample-cover.png"
    save_cover(cover_path)
    window = SimpleNamespace(
        app_settings=FakeSettings(),
        db=FakeDatabase(str(cover_path)),
    )
    page = CompletedPage(window)
    apply_runtime_translation(page)
    wide = target_dir / "completed-page-wide.png"
    narrow = target_dir / "completed-page-narrow.png"
    capture(page, app, wide, 1280, 760)
    capture(page, app, narrow, 900, 680)
    page.close()
    app.processEvents()
    print(wide)
    print(narrow)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
