from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, QUrl
from PySide6.QtQml import QQmlApplicationEngine

from app.presentation.app_bridge import AppBridge


class QmlShell(QObject):
    """Load the QML surface over an already-initialized application runtime."""

    def __init__(self, runtime: Any, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._root: QObject | None = None
        self.engine = QQmlApplicationEngine(self)
        # This is the only Python context object exposed to QML. The engine
        # owns it so QML root objects are released while their Interface is
        # still alive during application teardown.
        self.bridge = AppBridge(runtime, self.engine)
        self.engine.rootContext().setContextProperty("appBridge", self.bridge)

    @staticmethod
    def main_qml_path() -> Path:
        source_path = Path(__file__).resolve().parents[1] / "qml" / "Main.qml"
        bundle_root = getattr(sys, "_MEIPASS", "")
        bundled_path = Path(bundle_root) / "app" / "qml" / "Main.qml" if bundle_root else source_path
        return bundled_path if bundled_path.is_file() else source_path

    @property
    def runtime(self) -> Any:
        return self._runtime

    @property
    def root(self) -> QObject | None:
        return self._root

    def load(self) -> QObject:
        path = self.main_qml_path()
        self.engine.load(QUrl.fromLocalFile(str(path)))
        roots = self.engine.rootObjects()
        if not roots:
            raise RuntimeError(f"无法加载 QML 主界面：{path}")
        self._root = roots[0]
        return self._root

    def restore_startup_state(self) -> None:
        QTimer.singleShot(0, self.bridge.restoreStartupState)
