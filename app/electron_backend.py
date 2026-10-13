from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def main():
    # QtGui supplies cover rendering; no QML engine or QWidget is created.
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QGuiApplication
    from app.core.paths import initialize_data_layout
    from app.core.version import APP_PUBLISHER, APP_VERSION
    from app.presentation.application_runtime import ApplicationRuntime
    from app.presentation.electron_host import ElectronHost

    protocol = sys.stdout
    # PyInstaller's frozen interpreter can ignore PYTHONIOENCODING on Windows.
    # Keep the wire format UTF-8 even when the host's console locale is GBK.
    if hasattr(protocol, "reconfigure"):
        protocol.reconfigure(encoding="utf-8", errors="strict", newline="\n")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout = sys.stderr  # libraries can never contaminate the JSON channel
    lock = threading.Lock()

    def output(payload):
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        with lock:
            protocol.write(encoded + "\n")
            protocol.flush()

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QGuiApplication(sys.argv)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(APP_PUBLISHER)
    def safe_exception(exc_type, exc_value, exc_traceback):
        from app.core.paths import data_dir
        from app.core.redaction import redact_secret_text
        try:
            log_path = data_dir() / "logs" / "app-crash.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            details = redact_secret_text("".join(traceback.format_exception(exc_type, exc_value, exc_traceback)), limit=16000)
            with log_path.open("a", encoding="utf-8") as stream:
                stream.write(details + "\n")
            output({"type": "event", "event": "backendError", "data": {"message": redact_secret_text(str(exc_value))[:2000]}})
        except Exception:
            pass

    sys.excepthook = safe_exception
    try:
        runtime = ApplicationRuntime(initialize_data_layout(), app)
        from app.presentation.runtime import configure_font
        configure_font(app, runtime.app_settings.get("ui_language"))
        host = ElectronHost(runtime, output, app)
        host.bridge.shutdownCompleted.connect(app.quit)
        host.start_reader()
        QTimer.singleShot(0, host.start)
        result = app.exec()
        host.bridge.finish_shutdown_blocking()
        return int(result)
    except Exception as exc:
        from app.core.redaction import redact_secret_text
        output({"type": "fatal", "error": redact_secret_text(str(exc))[:2000]})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
