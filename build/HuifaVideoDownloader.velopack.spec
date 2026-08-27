# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir build consumed by Velopack.

Velopack updates the packaged application directory produced by this spec.
"""

from pathlib import Path
import sys

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules, copy_metadata


PROJECT_ROOT = Path(SPECPATH).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from app.core.version import (
    APP_COPYRIGHT,
    APP_DESCRIPTION,
    APP_NAME,
    APP_PUBLISHER,
    APP_VERSION,
)
from qt_bundle_filter import (
    EXCLUDED_PYSIDE_MODULES,
    EXCLUDED_WINDOWS_RUNTIME_MODULES,
    filter_qt_bundle_entries,
    validate_required_qt_artifacts,
)
from windows_version_info import build_windows_version_info

windows_version_info = build_windows_version_info(
    APP_VERSION,
    APP_NAME,
    APP_PUBLISHER,
    APP_DESCRIPTION,
    APP_COPYRIGHT,
)


datas = []
datas.append((str(PROJECT_ROOT / "assets" / "huifa.ico"), "assets"))
datas.append((str(PROJECT_ROOT / "languages"), "languages"))
vendor_sau = PROJECT_ROOT / "third_party" / "social_auto_upload"
if not (vendor_sau / "sau_cli.py").is_file():
    raise FileNotFoundError("Missing vendored social-auto-upload source")
for vendor_name, vendor_destination in (
    ("__init__.py", "third_party/social_auto_upload"),
    ("conf.py", "third_party/social_auto_upload"),
    ("sau_cli.py", "third_party/social_auto_upload"),
    ("LICENSE", "third_party/social_auto_upload"),
    ("UPSTREAM_COMMIT", "third_party/social_auto_upload"),
    ("uploader", "third_party/social_auto_upload/uploader"),
    ("utils", "third_party/social_auto_upload/utils"),
):
    datas.append((str(vendor_sau / vendor_name), vendor_destination))
datas += collect_data_files("yt_dlp")
datas += copy_metadata("yt-dlp")
playwright_datas, playwright_binaries, playwright_hidden = collect_all("playwright")
datas += playwright_datas

hiddenimports = [
    "app.core.application_updater",
    "app.core.tool_installer",
    "app.core.update_service",
    "app.core.version",
    "yt_dlp",
    "yt_dlp.version",
    "velopack",
    *playwright_hidden,
]

a = Analysis(
    [str(PROJECT_ROOT / "app" / "main.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=playwright_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[str(PROJECT_ROOT / "scripts" / "pyinstaller_hooks")],
    hooksconfig={},
    runtime_hooks=[
        str(PROJECT_ROOT / "build" / "01_velopack_hook.py"),
    ],
    excludes=[*EXCLUDED_PYSIDE_MODULES, *EXCLUDED_WINDOWS_RUNTIME_MODULES],
    noarchive=False,
    optimize=0,
)

unfiltered_qt_entries = len(a.binaries) + len(a.datas)
a.binaries = filter_qt_bundle_entries(a.binaries)
a.datas = filter_qt_bundle_entries(a.datas)
validate_required_qt_artifacts(a.binaries, a.datas)
print(
    "Qt bundle filter removed "
    f"{unfiltered_qt_entries - len(a.binaries) - len(a.datas)} unused entries."
)

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="HuifaVideoDownloader",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    version=windows_version_info,
    icon=str(PROJECT_ROOT / "assets" / "huifa.ico"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="HuifaVideoDownloader",
)
