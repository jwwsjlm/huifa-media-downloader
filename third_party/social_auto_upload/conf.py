"""Configuration bridge used by Huifa's vendored integration.

Upstream expects a top-level ``conf`` module. Huifa prepares these values
before importing the vendor tree so all writable state stays inside the
portable application data directory and every platform uses the same bundled
Chromium executable.
"""

from __future__ import annotations

import os
from pathlib import Path


SOURCE_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(
    os.environ.get("HUIFA_SAU_HOME")
    or os.environ.get("HUIFA_SAU_DATA_DIR")
    or SOURCE_DIR
).resolve()
BASE_DIR.mkdir(parents=True, exist_ok=True)

XHS_SERVER = os.environ.get("HUIFA_SAU_XHS_SERVER", "http://127.0.0.1:11901")
LOCAL_CHROME_PATH = os.environ.get("HUIFA_CHROMIUM_PATH", "").strip()
LOCAL_CHROME_HEADLESS = os.environ.get("HUIFA_SAU_HEADLESS", "1") != "0"
DEBUG_MODE = os.environ.get("HUIFA_SAU_DEBUG", "0") == "1"
YT_PROXY = os.environ.get("HUIFA_SAU_YT_PROXY", "").strip() or None
