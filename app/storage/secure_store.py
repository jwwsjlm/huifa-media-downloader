from __future__ import annotations

import sys

try:
    from keyring import errors as keyring_errors
    if sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring
    else:  # pragma: no cover - production releases target Windows
        WinVaultKeyring = None
except ImportError:  # pragma: no cover
    keyring_errors = None
    WinVaultKeyring = None

_PASSWORD_DELETE_ERRORS = (
    (keyring_errors.PasswordDeleteError,)
    if keyring_errors is not None
    else ()
)


class SecureStore:
    service = "huifa-video-downloader"

    def __init__(self, backend=None):
        self._backend = backend
        if self._backend is None and WinVaultKeyring is not None:
            try:
                # Use keyring's maintained Windows Credential Manager backend
                # directly. Generic plugin discovery also collects Linux,
                # macOS and DBus backends that a Windows build can never use.
                WinVaultKeyring.priority
                self._backend = WinVaultKeyring()
            except Exception:
                self._backend = None

    @property
    def backend_name(self) -> str:
        backend = self._backend
        if backend is None:
            return ""
        return f"{type(backend).__module__}.{type(backend).__name__}"

    def set(self, key: str, value: str) -> None:
        if self._backend is None:
            raise RuntimeError("系统凭据库组件不可用")
        try:
            self._backend.set_password(self.service, key, value)
        except Exception as exc:
            raise RuntimeError(f"无法写入系统凭据库：{exc}") from exc

    def get(self, key: str) -> str | None:
        if self._backend is None:
            return None
        try:
            return self._backend.get_password(self.service, key)
        except Exception:
            # A locked/misconfigured credential backend must not prevent the
            # main window from starting. Saving later will surface a detailed
            # actionable error to the user.
            return None

    def delete(self, key: str) -> None:
        if self._backend is None:
            raise RuntimeError("系统凭据库组件不可用")
        try:
            self._backend.delete_password(self.service, key)
        except _PASSWORD_DELETE_ERRORS:
            pass
        except Exception as exc:
            raise RuntimeError(f"无法访问系统凭据库：{exc}") from exc
