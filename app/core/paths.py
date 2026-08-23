from __future__ import annotations

import shutil
import sys
from pathlib import Path


def application_dir() -> Path:
    """Return the portable application directory for source and packaged runs."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    path = application_dir() / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def downloads_dir() -> Path:
    path = data_dir() / "downloads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def initialize_data_layout() -> Path:
    """Create portable storage and migrate the legacy database once."""
    target = data_dir()
    for name in ("browser", "downloads", "logs", "temp"):
        (target / name).mkdir(parents=True, exist_ok=True)

    legacy_db = Path.home() / ".youtube-release-studio" / "app.db"
    local_db = target / "app.db"
    if not local_db.exists() and legacy_db.exists():
        shutil.copy2(legacy_db, local_db)
    return target

