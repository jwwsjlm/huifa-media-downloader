from __future__ import annotations

import html
import re
from dataclasses import dataclass
from collections.abc import Mapping
from typing import Any

from app.core.update_service import select_release_asset
from app.ui.i18n import format_text as ui_format
from app.ui.i18n import runtime_text, text as ui_text
from app.ui.media_presentation import compact_path_display


@dataclass(frozen=True, slots=True)
class RuntimeComponentPresentation:
    label_text: str
    label_clickable: bool
    label_tooltip: str
    button_text: str
    button_enabled: bool
    button_tooltip: str = ""


def runtime_result_component(component: str) -> str:
    return "FFmpeg" if component == "FFprobe" else component


def runtime_component_install_needed(result: Mapping[str, Any]) -> bool:
    return bool(result.get("install_available") or result.get("has_update"))


def compact_runtime_version(component: str, version: str) -> str:
    value = str(version or "").strip()
    if not value:
        return "—"
    if value in {"未安装", "未检测", "检测失败", "已找到（版本读取失败）"}:
        return value
    if component in {"Deno", "FFmpeg", "FFprobe"}:
        value = re.sub(
            rf"^{re.escape(component)}\s+",
            "",
            value,
            flags=re.IGNORECASE,
        )
    if component in {"FFmpeg", "FFprobe"}:
        release = re.match(r"([^\s-]+(?:-[0-9]+)?).*", value)
        build_date = re.findall(r"(?<!\d)(20\d{6})(?!\d)", value)
        if release and build_date:
            return f"{release.group(1)} · {build_date[-1]}"
    return value if len(value) <= 22 else value[:19] + "…"


def build_runtime_component_presentation(
    component: str,
    local_detail: tuple[str, str, str],
    result: dict[str, Any],
    *,
    remote_checking: bool,
    remote_error: str,
    installing_component: str,
) -> RuntimeComponentPresentation:
    raw_version, source, runtime_path = local_detail
    translated_version = str(raw_version or "")
    if translated_version == "未安装":
        translated_version = ui_text('Not installed')
    elif translated_version in {"检测失败", "未检测"}:
        translated_version = ui_text('Detection failed')
    compact_version = compact_runtime_version(component, translated_version)
    actual_component = runtime_result_component(component)
    asset = (
        select_release_asset(actual_component, result.get("assets") or [])
        if result
        else None
    )
    local_missing = str(raw_version or "") in {
        "", "未安装", "未检测", "检测失败",
    }
    channel_switch_required = bool(result.get("channel_switch_required"))
    downloadable = bool(result.get("auto_install_supported") and asset is not None)
    actionable = downloadable and runtime_component_install_needed(result)

    tooltip_parts = [ui_format(
        'Current local version: {version}',
        version=translated_version or ui_text('Not detected'),
    )]
    if source:
        tooltip_parts.append(str(source))
    if runtime_path:
        tooltip_parts.append(ui_format(
            'Location: {path}',
            path=compact_path_display(runtime_path),
        ))
    if result:
        latest = str(result.get("latest") or ui_text('Unknown'))
        tooltip_parts.append(ui_format(
            'Remote version: {version}',
            version=latest,
        ))
        warning = str(result.get("metadata_warning") or "")
        if warning:
            tooltip_parts.append(warning)
    if actionable:
        tooltip_parts.append(
            ui_text(
                'The current FFmpeg does not match the selected build. Click the version or button to switch it in the app-local tools folder.',
            )
            if channel_switch_required
            else ui_text(
                'A downloadable update is available. Click the version or the button to install it in the app-local tools folder.',
            )
            if not local_missing
            else ui_text(
                'Not installed. Click the version or the button to install it in the app-local tools folder.',
            )
        )
    escaped_version = html.escape(compact_version)
    label_text = (
        f'<a href="update" style="color:#c2413a; text-decoration:underline;">{escaped_version}</a>'
        if actionable
        else escaped_version
    )

    installing = installing_component == actual_component
    if installing:
        button_text = ui_text('Updating…')
        button_enabled = False
        button_tooltip = ""
    elif remote_checking and not result:
        button_text = ui_text('Checking…')
        button_enabled = False
        button_tooltip = ""
    elif result.get("error") or (remote_error and not result):
        button_text = ui_text('Retry')
        button_enabled = True
        button_tooltip = runtime_text(result.get("error") or remote_error)
    elif not result:
        button_text = ui_text('Check Update')
        button_enabled = True
        button_tooltip = ""
    elif actionable:
        button_text = (
            ui_text('Switch')
            if channel_switch_required
            else ui_text('Download')
            if local_missing
            else ui_text('Update')
        )
        button_enabled = True
        button_tooltip = tooltip_parts[-1]
    elif not downloadable:
        button_text = ui_text(
            'Unavailable',
            context="runtime_component.action",
        )
        button_enabled = False
        button_tooltip = ""
    else:
        button_text = ui_text('Up to Date')
        button_enabled = False
        button_tooltip = ui_text('The local version is up to date.')

    return RuntimeComponentPresentation(
        label_text=label_text,
        label_clickable=actionable,
        label_tooltip="\n".join(tooltip_parts),
        button_text=button_text,
        button_enabled=button_enabled,
        button_tooltip=button_tooltip,
    )


__all__ = [
    "RuntimeComponentPresentation",
    "build_runtime_component_presentation",
    "compact_runtime_version",
    "runtime_component_install_needed",
    "runtime_result_component",
]
