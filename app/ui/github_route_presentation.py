from __future__ import annotations

from app.core.github_mirrors import ROUTE_DIRECT
from app.ui.i18n import format_text as ui_format
from app.ui.i18n import text as ui_text


def github_route_display_name(route_id: str, fallback: str = "") -> str:
    """Return one stable user-facing name for a GitHub download route."""

    route_id = str(route_id or "")
    if route_id == ROUTE_DIRECT:
        return ui_text("GitHub Direct")
    if route_id == "mirror:gh-proxy":
        return "GitHub Proxy"
    if route_id == "mirror:ghfast":
        return "GHFast"
    if route_id.startswith("custom:"):
        host = str(fallback or "").removeprefix("自定义 · ")
        return ui_format("Custom · {host}", host=host)
    return fallback or route_id
