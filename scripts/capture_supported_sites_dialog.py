from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault(
    "QT_QPA_PLATFORM",
    os.environ.get("HUIFA_QT_PLATFORM", "windows"),
)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ui.runtime import create_application
from app.ui.supported_sites_dialog import SupportedSitesDialog
from app.ui.theme import THEME_LIGHT, build_application_stylesheet


def visible_rows(dialog: SupportedSitesDialog) -> int:
    return sum(
        not dialog.list.item(index).isHidden()
        for index in range(dialog.list.count())
    )


def main() -> int:
    app, font = create_application([], requested_locale="zh-CN")
    app.setStyleSheet(build_application_stylesheet(THEME_LIGHT))
    started_at = time.perf_counter()
    dialog = SupportedSitesDialog()
    load_elapsed_ms = round((time.perf_counter() - started_at) * 1000, 1)
    dialog.show()
    app.processEvents()

    target_dir = ROOT / "data" / "temp" / "ui-review"
    target_dir.mkdir(parents=True, exist_ok=True)
    default_target = target_dir / "supported-sites-default.png"
    default_saved = dialog.grab().save(str(default_target))

    dialog.search.setText("youtube")
    dialog.apply_filter()
    app.processEvents()
    filtered_target = target_dir / "supported-sites-filtered.png"
    filtered_saved = dialog.grab().save(str(filtered_target))
    filtered_count = visible_rows(dialog)

    dialog.search.clear()
    dialog.apply_filter()
    app.processEvents()
    restored_target = target_dir / "supported-sites-restored.png"
    restored_saved = dialog.grab().save(str(restored_target))
    restored_count = visible_rows(dialog)

    report = {
        "qt_platform": app.platformName(),
        "font_locale": font.locale,
        "extractor_count": dialog.list.count(),
        "load_elapsed_ms": load_elapsed_ms,
        "filtered_count": filtered_count,
        "restored_count": restored_count,
        "restored_label": dialog.count.text(),
        "screenshots": [
            str(default_target),
            str(filtered_target),
            str(restored_target),
        ],
        "ok": (
            default_saved
            and filtered_saved
            and restored_saved
            and dialog.list.count() > 0
            and 0 < filtered_count < dialog.list.count()
            and restored_count == dialog.list.count()
        ),
    }
    report_path = target_dir / "supported-sites-report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    dialog.close()
    app.processEvents()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(report_path)
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
