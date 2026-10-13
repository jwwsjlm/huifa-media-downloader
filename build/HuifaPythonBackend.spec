# -*- mode: python ; coding: utf-8 -*-
"""Qt event-loop Python backend staged beside the Electron main executable."""
from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_all, collect_data_files, copy_metadata

PROJECT_ROOT = Path(SPECPATH).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from qt_bundle_filter import EXCLUDED_PYSIDE_MODULES, EXCLUDED_WINDOWS_RUNTIME_MODULES, filter_qt_bundle_entries
from app.core.version import APP_VERSION, APP_NAME, APP_PUBLISHER, APP_DESCRIPTION, APP_COPYRIGHT
from windows_version_info import build_windows_version_info

datas = [(str(PROJECT_ROOT / "assets/huifa.ico"), "assets"), (str(PROJECT_ROOT / "languages"), "languages")]
vendor = PROJECT_ROOT / "third_party/social_auto_upload"
for name in ("__init__.py", "conf.py", "sau_cli.py", "LICENSE", "UPSTREAM_COMMIT", "uploader", "utils"):
    datas.append((str(vendor / name), "third_party/social_auto_upload" + ("/" + name if (vendor / name).is_dir() else "")))
datas += collect_data_files("yt_dlp") + copy_metadata("yt-dlp")
playwright_datas, playwright_binaries, playwright_hidden = collect_all("playwright")
datas += playwright_datas
a = Analysis([str(PROJECT_ROOT / "app/electron_backend.py")], pathex=[str(PROJECT_ROOT)], binaries=playwright_binaries, datas=datas,
    hiddenimports=["app.core.application_updater", "app.core.tool_installer", "app.core.update_service", "app.core.version", "yt_dlp", "yt_dlp.version", "velopack", *playwright_hidden],
    hookspath=[str(PROJECT_ROOT / "scripts/pyinstaller_hooks")], runtime_hooks=[str(PROJECT_ROOT / "build/01_velopack_hook.py")],
    excludes=[*EXCLUDED_PYSIDE_MODULES, *EXCLUDED_WINDOWS_RUNTIME_MODULES, "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickControls2"], noarchive=False, optimize=0)
a.binaries = filter_qt_bundle_entries(a.binaries)
a.datas = filter_qt_bundle_entries(a.datas)
destinations = {str(entry[0]).replace("\\", "/").casefold() for entry in [*a.binaries, *a.datas]}
required = {"pyside6/qt6core.dll", "pyside6/qt6gui.dll", "pyside6/plugins/imageformats/qjpeg.dll", "pyside6/plugins/platforms/qwindows.dll"}
if required - destinations:
    raise RuntimeError("Missing backend Qt artifacts: " + ", ".join(sorted(required - destinations)))
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="HuifaPythonBackend", debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=True, version=build_windows_version_info(APP_VERSION, APP_NAME, APP_PUBLISHER, APP_DESCRIPTION, APP_COPYRIGHT), icon=str(PROJECT_ROOT / "assets/huifa.ico"))
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="HuifaPythonBackend")
