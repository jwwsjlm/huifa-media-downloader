from __future__ import annotations

import requests


def detect_public_ip(proxy: str = "") -> str:
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        response = requests.get("https://api.ipify.org", params={"format": "text"}, proxies=proxies, timeout=8)
        response.raise_for_status()
        return response.text.strip()
    except Exception:
        return ""

