from __future__ import annotations

import getpass
import hashlib
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket


def instance_server_name() -> str:
    """Return a stable per-user name for the local activation channel."""
    identity = f"{getpass.getuser()}|{Path.home()}".casefold()
    digest = hashlib.sha256(identity.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"huifa-video-downloader-{digest}"


class SingleInstance(QObject):
    """Prevent concurrent app instances and ask the primary window to activate."""

    activation_requested = Signal()

    def __init__(self, name: str | None = None, parent: QObject | None = None):
        super().__init__(parent)
        self.name = name or instance_server_name()
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._accept_connections)
        self._primary = False

    @property
    def is_primary(self) -> bool:
        return self._primary

    def acquire(self) -> bool:
        """Listen as primary or notify the already-running primary instance."""
        if self._primary:
            return True
        if self._notify_existing():
            return False

        # A crashed process can leave a stale local-server endpoint.  Remove it
        # only after a real connection attempt has failed.
        QLocalServer.removeServer(self.name)
        if self.server.listen(self.name):
            self._primary = True
            return True

        # Two processes can race between the initial probe and listen().  Give
        # the winner one final chance to receive this process' activation ping.
        if self._notify_existing(timeout_ms=600):
            return False
        raise RuntimeError(self.server.errorString() or "无法创建单实例通信通道")

    def _notify_existing(self, timeout_ms: int = 350) -> bool:
        socket = QLocalSocket()
        socket.connectToServer(self.name)
        if not socket.waitForConnected(timeout_ms):
            socket.abort()
            return False
        socket.write(b"activate\n")
        socket.flush()
        socket.waitForBytesWritten(timeout_ms)
        socket.disconnectFromServer()
        return True

    def _accept_connections(self) -> None:
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            if socket is None:
                continue
            # The private server name is only used for activation.  Treat the
            # accepted connection itself as the request so a short-lived
            # secondary process cannot disconnect before readyRead is handled.
            self.activation_requested.emit()
            socket.readAll()
            socket.disconnected.connect(socket.deleteLater)

    def close(self) -> None:
        if not self._primary:
            return
        self.server.close()
        QLocalServer.removeServer(self.name)
        self._primary = False
