from __future__ import annotations

from typing import Any

from app.adapters.base_adapter import PlatformAdapter


class ToutiaoAdapter(PlatformAdapter):
    name = "toutiao"

    def build_payload(self, media: dict[str, Any], metadata: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
        return {"media": media, "metadata": metadata, "settings": settings}

    def publish(self, payload: dict[str, Any]) -> tuple[bool, str]:
        return False, "今日头条适配器尚未连接稳定的官方/上游上传接口，请在浏览器页手动完成发布"

