from __future__ import annotations

try:
    import keyring
except ImportError:  # pragma: no cover
    keyring = None


class SecureStore:
    service = "youtube-release-studio"

    def set(self, key: str, value: str) -> None:
        if keyring:
            keyring.set_password(self.service, key, value)

    def get(self, key: str) -> str | None:
        return keyring.get_password(self.service, key) if keyring else None

