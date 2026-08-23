from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class PlatformAdapter(ABC):
    name = "unknown"

    def check_account(self, account: str) -> tuple[bool, str]:
        return True, account

    def validate_metadata(self, metadata: dict[str, Any]) -> list[str]:
        return []

    @abstractmethod
    def build_payload(self, media: dict[str, Any], metadata: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def publish(self, payload: dict[str, Any]) -> tuple[bool, str]: ...

    def parse_result(self, result: str) -> dict[str, Any]:
        return {"raw": result}

