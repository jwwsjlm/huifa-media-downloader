from __future__ import annotations

from functools import lru_cache
from importlib.metadata import PackageNotFoundError, requires

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name


YTDLP_EJS_SOURCES = frozenset({"auto", "npm", "github", "local"})


def normalize_ytdlp_ejs_source(value: object) -> str:
    """Normalize the configured yt-dlp-ejs provider policy."""

    source = str(value or "auto").strip().casefold()
    return source if source in YTDLP_EJS_SOURCES else "auto"


@lru_cache(maxsize=1)
def required_ytdlp_ejs_version() -> str:
    """Return the exact EJS version pinned by the bundled Python yt-dlp.

    yt-dlp intentionally pins this companion package and rejects unsupported
    versions. Both PyInstaller specs copy yt-dlp's distribution metadata so
    the same lookup works in source and packaged applications.
    """

    try:
        dependencies = requires("yt-dlp") or ()
    except (PackageNotFoundError, ValueError):
        return ""
    for dependency in dependencies:
        try:
            requirement = Requirement(dependency)
        except InvalidRequirement:
            continue
        if canonicalize_name(requirement.name) != "yt-dlp-ejs":
            continue
        for specifier in requirement.specifier:
            version = str(specifier.version or "").strip()
            if specifier.operator in {"==", "==="} and version and "*" not in version:
                return version
    return ""


def ytdlp_ejs_version_compatible(version: object) -> bool:
    """Return whether one EJS wheel matches the bundled yt-dlp pin."""

    candidate = str(version or "").strip()
    required = required_ytdlp_ejs_version()
    return bool(candidate) and (not required or candidate == required)
