from __future__ import annotations

from app.adapters.sau_adapter import SAU_SUPPORTED_PLATFORMS
from app.ui.i18n import text as ui_text


def distribution_target_platforms(
    configured: str,
    supported: tuple[str, ...] = SAU_SUPPORTED_PLATFORMS,
) -> tuple[str, ...]:
    """Return the ordered supported targets for media distribution."""

    supported_order = tuple(
        dict.fromkeys(
            str(name).strip()
            for name in supported
            if str(name).strip()
        )
    )
    raw = str(configured or "").strip()
    if not raw:
        return supported_order
    requested = {name.strip() for name in raw.split(",") if name.strip()}
    selected = tuple(name for name in supported_order if name in requested)
    return selected or supported_order


def serialize_distribution_target_platforms(
    selected: tuple[str, ...] | list[str] | set[str],
    supported: tuple[str, ...] = SAU_SUPPORTED_PLATFORMS,
) -> str:
    """Persist an ordered subset; the full set keeps the compact empty value."""

    supported_order = tuple(
        dict.fromkeys(
            str(name).strip()
            for name in supported
            if str(name).strip()
        )
    )
    selected_names = {
        str(name).strip()
        for name in selected
        if str(name).strip()
    }
    ordered = tuple(name for name in supported_order if name in selected_names)
    if not ordered:
        raise ValueError(
            ui_text("Select at least one default publishing platform")
        )
    return "" if ordered == supported_order else ",".join(ordered)


def distribution_platform_states(
    platform_states: dict[str, str],
    target_platforms: tuple[str, ...],
) -> dict[str, str]:
    """Filter persisted task states to the active distribution plan."""

    targets = set(target_platforms)
    return {
        str(name): str(state)
        for name, state in (platform_states or {}).items()
        if str(name) in targets
    }


def distribution_preselected_platforms(
    platform_states: dict[str, str],
    target_platforms: tuple[str, ...],
) -> tuple[str, ...]:
    """Choose targets that do not already have a durable publishing task."""

    states = distribution_platform_states(platform_states, target_platforms)
    return tuple(
        platform
        for platform in target_platforms
        if platform not in states
    )
