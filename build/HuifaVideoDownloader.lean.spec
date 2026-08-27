# -*- mode: python ; coding: utf-8 -*-
"""Primary Windows release: one self-extracting executable.

PySide6 is a runtime dependency of the GUI and is intentionally embedded in
the executable. End users do not install it separately. This build is not a
Velopack-managed release; Velopack requires the separate onedir spec.
"""

from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

PROJECT_ROOT = Path(SPECPATH).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / 'scripts'))
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

# The release target is Windows x64.  Packaging the legacy root FFmpeg build
# and the x86 build added roughly 275 MiB to the one-file executable and made
# every launch extract files that can never be used.  Ship only the x64
# runtime; ffplay is not used by the downloader.
ffmpeg_dir = PROJECT_ROOT / 'tools' / 'ffmpeg' / 'x64'
required_ffmpeg_files = {'ffmpeg.exe', 'ffprobe.exe'}
missing_ffmpeg_files = sorted(
    name for name in required_ffmpeg_files if not (ffmpeg_dir / name).is_file()
)
if missing_ffmpeg_files:
    raise FileNotFoundError(
        'Missing x64 FFmpeg release files: ' + ', '.join(missing_ffmpeg_files)
    )
datas = [
    (str(path), 'tools/ffmpeg/x64')
    for path in ffmpeg_dir.iterdir()
    if path.is_file() and path.name.lower() not in {'ffplay.exe', 'readme.md'}
]
datas.append((str(PROJECT_ROOT / 'assets' / 'huifa.ico'), 'assets'))
datas.append((str(PROJECT_ROOT / 'languages'), 'languages'))
vendor_sau = PROJECT_ROOT / 'third_party' / 'social_auto_upload'
chromium_dir = PROJECT_ROOT / 'tools' / 'chromium'
if not (vendor_sau / 'sau_cli.py').is_file():
    raise FileNotFoundError('Missing vendored social-auto-upload source')
chromium_executables = sorted(chromium_dir.glob('chromium-*/chrome-win64/chrome.exe'))
if not chromium_executables:
    raise FileNotFoundError('Missing app-local Playwright Chromium')
chromium_executable = max(
    chromium_executables,
    key=lambda path: int(path.parents[1].name.rsplit('-', 1)[-1]),
)
chromium_runtime_dir = chromium_executable.parent
for vendor_name, vendor_destination in (
    ('__init__.py', 'third_party/social_auto_upload'),
    ('conf.py', 'third_party/social_auto_upload'),
    ('sau_cli.py', 'third_party/social_auto_upload'),
    ('LICENSE', 'third_party/social_auto_upload'),
    ('UPSTREAM_COMMIT', 'third_party/social_auto_upload'),
    ('myUtils', 'third_party/social_auto_upload/myUtils'),
    ('uploader', 'third_party/social_auto_upload/uploader'),
    ('utils', 'third_party/social_auto_upload/utils'),
):
    datas.append((str(vendor_sau / vendor_name), vendor_destination))
# The full Chromium executable supports both headed login and headless upload.
# Do not ship Playwright's duplicate headless shell, recording-only FFmpeg, or
# WinLDD installer helper alongside it.
datas.append((str(chromium_runtime_dir), 'tools/chromium/chrome-win64'))
datas += collect_data_files('yt_dlp')
playwright_datas, playwright_binaries, playwright_hidden = collect_all('playwright')
datas += playwright_datas
hiddenimports = [
    'app.core.application_update_service',
    'app.core.application_updater',
    'app.core.cover_service',
    'app.core.tool_installer',
    'app.core.update_service',
    'app.core.version',
    'app.adapters.openai_cover_provider',
    'yt_dlp', 'yt_dlp.version',
    'cv2', 'loguru', 'numpy', 'qrcode', 'segno',
    *playwright_hidden,
    *collect_submodules('cv2'),
]

a = Analysis(
    [str(PROJECT_ROOT / 'app' / 'main.py')],
    pathex=[str(PROJECT_ROOT)],
    binaries=playwright_binaries, datas=datas, hiddenimports=hiddenimports,
    hookspath=[str(PROJECT_ROOT / 'scripts' / 'pyinstaller_hooks')], hooksconfig={}, runtime_hooks=[],
    # The primary single-file build uses its own GitHub asset updater. Do not
    # accidentally pull the optional Velopack SDK into it from a developer
    # environment; Velopack remains exclusive to installer/onedir builds.
    excludes=[*EXCLUDED_PYSIDE_MODULES, *EXCLUDED_WINDOWS_RUNTIME_MODULES, 'velopack'],
    noarchive=False, optimize=0,
)

# PyInstaller's QtQml hook intentionally collects every QML module in the
# PySide6 wheel. Remove only the module families explicitly excluded above;
# keep the QtWidgets, image codec and platform-plugin runtime used by the GUI.
unfiltered_qt_entries = len(a.binaries) + len(a.datas)
a.binaries = filter_qt_bundle_entries(a.binaries)
a.datas = filter_qt_bundle_entries(a.datas)
validate_required_qt_artifacts(a.binaries, a.datas)
print(
    'Qt bundle filter removed '
    f'{unfiltered_qt_entries - len(a.binaries) - len(a.datas)} unused entries.'
)

pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name='HuifaVideoDownloader',
         debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
         console=False, disable_windowed_traceback=False,
         version=windows_version_info,
         icon=str(PROJECT_ROOT / 'assets' / 'huifa.ico'))
