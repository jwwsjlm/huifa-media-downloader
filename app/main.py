from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path
from typing import Any

from PySide6 import __version__ as PYSIDE_VERSION
from PySide6.QtCore import QCoreApplication, QEvent, QTimer, qVersion
from PySide6.QtGui import QIcon
from PySide6.QtQuickControls2 import QQuickStyle
from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QMessageBox

# Allow `python D:\code\yt-release\app\main.py` to work even when the
# current PowerShell directory is not the project root.  Python otherwise
# places only `...\app` on sys.path, so the top-level `app` package cannot be
# imported from a command launched in C:\Windows\System32.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.download_service import yt_dlp as download_ytdlp
from app.core.app_settings import AppSettings
from app.core.atomic_json import write_json_atomic
from app.core.version import APP_PUBLISHER, APP_VERSION
from app.core.paths import (
    PortableDataDirectoryError,
    application_dir,
    data_dir,
    initialize_data_layout,
)
from app.core.single_instance import SingleInstance
from app.core.update_service import installed_component_details
from app.presentation.application_runtime import ApplicationRuntime
from app.presentation.qml_shell import QmlShell
from app.presentation.runtime import configure_font, configure_high_dpi, ui_text
from app.presentation.i18n import application_name_text, runtime_text


PACKAGED_SMOKE_OUTPUT_ENV = "HUIFA_PACKAGED_SMOKE_OUTPUT"
PACKAGED_UPDATE_MODES = frozenset({"velopack"})


def _build_packaged_smoke_report(app: QApplication, shell: QmlShell) -> dict[str, Any]:
    """Describe the exact runtime loaded by a freshly built executable.

    The build pipeline uses this report to catch packaging regressions that
    source-level tests cannot see, such as a missing yt-dlp hidden import or a
    packaged build accidentally falling back to the non-updatable source mode.
    """
    yt_version, yt_source, yt_path = installed_component_details("yt-dlp")
    runtime = shell.runtime
    ffmpeg_version, ffmpeg_source, ffmpeg_path = installed_component_details(
        "FFmpeg", runtime.app_settings.get("ffmpeg_path")
    )
    configured_ffprobe = runtime.app_settings.get("ffprobe_path")
    ffprobe_version, ffprobe_source, ffprobe_path = (
        installed_component_details(
            "FFprobe",
            runtime.app_settings.get("ffmpeg_path"),
            configured_ffprobe,
        )
        if configured_ffprobe.strip()
        else installed_component_details("FFprobe", runtime.app_settings.get("ffmpeg_path"))
    )
    executable = Path(sys.executable).resolve()
    frozen = bool(getattr(sys, "frozen", False))
    yt_dlp_core_ready = bool(
        download_ytdlp is not None and callable(getattr(download_ytdlp, "YoutubeDL", None))
    )
    update_mode = str(runtime.application_update_mode or "")
    secure_store_backend = str(getattr(runtime.secure_store, "backend_name", "") or "")
    application_version = app.applicationVersion()
    organization_name = app.organizationName()
    ok = bool(
        frozen
        and executable.name.casefold() == "huifavideodownloader.exe"
        and update_mode in PACKAGED_UPDATE_MODES
        and application_version == APP_VERSION
        and organization_name == APP_PUBLISHER
        and secure_store_backend == "keyring.backends.Windows.WinVaultKeyring"
        and yt_dlp_core_ready
        and shell.root is not None
        and yt_version not in {"", "未安装", "未检测"}
        and ffmpeg_version not in {"", "未安装", "未检测", "不可用"}
        and ffprobe_version not in {"", "未安装", "未检测", "不可用"}
    )
    return {
        "schema_version": 1,
        "ok": ok,
        "frozen": frozen,
        "executable": str(executable),
        "application_dir": str(application_dir().resolve()),
        "application_update_mode": update_mode,
        "application_version": application_version,
        "organization_name": organization_name,
        "pyside6_version": PYSIDE_VERSION,
        "qt_version": qVersion(),
        "qt_platform": app.platformName(),
        "qml_root_loaded": shell.root is not None,
        "secure_store_backend": secure_store_backend,
        "yt_dlp": {
            "version": yt_version,
            "source": yt_source,
            "runtime_path": yt_path,
            "core_ready": yt_dlp_core_ready,
        },
        "ffmpeg": {
            "version": ffmpeg_version,
            "source": ffmpeg_source,
            "runtime_path": ffmpeg_path,
        },
        "ffprobe": {
            "version": ffprobe_version,
            "source": ffprobe_source,
            "runtime_path": ffprobe_path,
        },
    }


def _run_packaged_smoke(
    app: QApplication,
    output_path: Path,
    runtime_data_dir: Path,
) -> int:
    """Load the real QML shell, record runtime health, then exit promptly."""
    shell: QmlShell | None = None
    root: Any | None = None
    try:
        runtime = ApplicationRuntime(runtime_data_dir, app)
        shell = QmlShell(runtime, app)
        root = shell.load()
        report = _build_packaged_smoke_report(app, shell)
        write_json_atomic(output_path, report)
    except Exception as exc:
        try:
            write_json_atomic(
                output_path,
                {
                    "schema_version": 1,
                    "ok": False,
                    "frozen": bool(getattr(sys, "frozen", False)),
                    "executable": str(Path(sys.executable).resolve()),
                    "error_type": type(exc).__name__,
                    "error": str(exc)[:1000],
                },
            )
        except Exception:
            traceback.print_exc()
        return 2

    result_code = 0 if report["ok"] else 2

    # The package probe must not race ordinary startup restoration with the
    # asynchronous user-facing shutdown path. Loading the real QML root above
    # is the coverage needed here; exit the isolated process directly.
    QTimer.singleShot(0, lambda: app.exit(result_code))
    timeout = QTimer(app)
    timeout.setSingleShot(True)
    timeout.timeout.connect(lambda: app.exit(3))
    timeout.start(20_000)
    event_result = app.exec()
    timeout.stop()
    return int(event_result)


def main() -> int:
    configure_high_dpi()
    # Keep every QML control on one cross-platform dark control style instead
    # of falling back to the Windows-native white widgets.
    QQuickStyle.setStyle("Material")
    app = QApplication(sys.argv)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(APP_PUBLISHER)
    try:
        runtime_data_dir = initialize_data_layout()
    except PortableDataDirectoryError as exc:
        QMessageBox.critical(
            None,
            ui_text('Application Folder Is Not Writable'),
            ui_text(
                'The application stores settings, cookies, the database, and updates in its local folder.\n\n',
            )
            + ui_text('Cannot write to: ')
            + f"{exc.path}"
            + ui_text(
                '\n\nExit, move the complete application folder to a writable location, and start it again.',
            )
        )
        return 1

    configure_font(app, AppSettings().get("ui_language"))
    app.setApplicationName(application_name_text())
    app.setApplicationDisplayName(application_name_text())
    runtime_root = Path(getattr(sys, "_MEIPASS", application_dir()))
    icon_candidates = (
        runtime_root / "assets" / "huifa.ico",
        application_dir() / "assets" / "huifa.ico",
        application_dir() / "_internal" / "assets" / "huifa.ico",
    )
    icon_path = next((path for path in icon_candidates if path.exists()), None)
    if icon_path is not None:
        app.setWindowIcon(QIcon(str(icon_path)))

    def handle_exception(exc_type, exc_value, exc_traceback):
        """Keep unexpected GUI exceptions visible instead of a silent exit."""
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return
        details = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
        try:
            # Keep crash diagnostics beside the portable runtime data in both
            # source and PyInstaller-frozen launches.  Using ``__file__``
            # here would point into ``_internal`` in an onedir build.
            log_path = data_dir() / "logs" / "app-crash.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(details + "\n")
        except Exception:
            pass
        try:
            QMessageBox.critical(
                None,
                ui_text('Application Error'),
                ui_text(
                    'The application encountered an unhandled error:\n',
                )
                + runtime_text(exc_value)
                + ui_text(
                    '\n\nDetails were written to: data/logs/app-crash.log',
                ),
            )
        finally:
            sys.__excepthook__(exc_type, exc_value, exc_traceback)

    sys.excepthook = handle_exception

    smoke_output = os.environ.get(PACKAGED_SMOKE_OUTPUT_ENV, "").strip()
    if smoke_output:
        # Build verification must not contend with or activate an already
        # running user instance. It runs from an isolated temporary directory
        # and exits immediately after the real QML shell has initialized.
        return _run_packaged_smoke(app, Path(smoke_output), runtime_data_dir)

    instance_guard = SingleInstance(parent=app)
    try:
        is_primary = instance_guard.acquire()
    except RuntimeError as exc:
        QMessageBox.critical(
            None,
            ui_text('Startup Failed'),
            ui_text('Unable to establish single-instance communication:\n')
            + runtime_text(exc),
        )
        return 1
    if not is_primary:
        # The primary process has already received an activation request.
        # Exit immediately so a second modal window/process is not left behind.
        return 0

    runtime = ApplicationRuntime(runtime_data_dir, app)
    shell = QmlShell(runtime, app)
    root = shell.load()
    shell.restore_startup_state()

    def activate_window() -> None:
        try:
            if root.isMinimized():
                root.showNormal()
            else:
                root.show()
            root.raise_()
            root.requestActivate()
        except RuntimeError:
            pass

    instance_guard.activation_requested.connect(activate_window)
    app.aboutToQuit.connect(instance_guard.close)
    exit_code = app.exec()
    if not shell.bridge.shutdownReady:
        # Some native close paths tear down the QML window before its closing
        # handler runs. Finish the same cooperative shutdown synchronously
        # instead of leaving Python alive without a GUI event loop.
        shell.bridge.finish_shutdown_blocking()
    instance_guard.close()
    shell.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    return int(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
