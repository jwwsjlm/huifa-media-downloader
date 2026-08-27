from __future__ import annotations

import importlib
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

from app.core.paths import application_dir, tool_runtime_roots


@dataclass(frozen=True, slots=True)
class LocalPythonComponent:
    name: str
    version: str
    path: str
    source: str


def _version_key(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", str(value or ""))
    return tuple(int(number) for number in numbers[:8]) or (0,)


def wheel_distribution_version(path: str | Path, distribution: str) -> str:
    """Read a wheel version from trusted metadata without importing it."""
    wheel = Path(path)
    expected = distribution.replace("-", "_").casefold()
    try:
        with zipfile.ZipFile(wheel) as archive:
            metadata_names = [
                name
                for name in archive.namelist()
                if name.casefold().endswith(".dist-info/metadata")
                and Path(name).parts[0].casefold().startswith(expected + "-")
            ]
            if not metadata_names:
                return ""
            raw = archive.read(sorted(metadata_names)[0]).decode("utf-8", errors="replace")
    except (OSError, KeyError, zipfile.BadZipFile):
        return ""
    for line in raw.splitlines():
        if line.casefold().startswith("version:"):
            return line.partition(":")[2].strip()
    return ""


def local_ejs_wheels() -> list[Path]:
    """Return valid yt-dlp-ejs wheels from app-owned persistent tool roots."""
    app_root = application_dir()
    wheels: list[tuple[tuple[int, ...], Path]] = []
    seen: set[Path] = set()
    for root in tool_runtime_roots(app_root):
        folder = root / "tools" / "yt-dlp-ejs"
        if not folder.is_dir():
            continue
        for path in folder.glob("*.whl"):
            try:
                resolved = path.resolve()
            except OSError:
                resolved = path
            if resolved in seen:
                continue
            version = wheel_distribution_version(path, "yt-dlp-ejs")
            if not version:
                continue
            seen.add(resolved)
            wheels.append((_version_key(version), resolved))
    wheels.sort(key=lambda item: (item[0], str(item[1]).casefold()), reverse=True)
    return [path for _, path in wheels]


def local_ejs_component() -> LocalPythonComponent | None:
    wheels = local_ejs_wheels()
    if not wheels:
        return None
    path = wheels[0]
    version = wheel_distribution_version(path, "yt-dlp-ejs")
    return LocalPythonComponent(
        name="yt-dlp-ejs",
        version=version,
        path=str(path),
        source="软件本地核心目录",
    )


def activate_local_ejs() -> LocalPythonComponent | None:
    """Put the newest app-owned EJS wheel ahead of system Python packages."""
    component = local_ejs_component()
    if component is None:
        return None
    selected = str(Path(component.path))
    known = {str(path) for path in local_ejs_wheels()}
    sys.path[:] = [entry for entry in sys.path if entry not in known]
    sys.path.insert(0, selected)
    importlib.invalidate_caches()
    return component
