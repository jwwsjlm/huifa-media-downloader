from __future__ import annotations

"""Cookie source helpers shared by Settings and the yt-dlp worker."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any


COOKIE_SOURCE_NONE = "none"
COOKIE_SOURCE_FILE = "file"
COOKIE_SOURCE_BROWSER = "browser"
COOKIE_SOURCE_EMBEDDED = "embedded"
EMBEDDED_DOWNLOAD_PROFILE = "download"

SUPPORTED_COOKIE_BROWSERS = ("chrome", "edge", "firefox", "brave")
COOKIE_BROWSER_LABELS = {
    "chrome": "Chrome",
    "edge": "Edge",
    "firefox": "Firefox",
    "brave": "Brave",
}


def normalize_cookie_source(value: str | None) -> str:
    value = str(value or "").strip().casefold()
    return value if value in {
        COOKIE_SOURCE_NONE,
        COOKIE_SOURCE_FILE,
        COOKIE_SOURCE_BROWSER,
        COOKIE_SOURCE_EMBEDDED,
    } else COOKIE_SOURCE_NONE


def normalize_cookie_browser(value: str | None) -> str:
    value = str(value or "").strip().casefold()
    return value if value in SUPPORTED_COOKIE_BROWSERS else "chrome"


def browser_cookie_spec(
    browser: str | None,
    profile: str | None = "",
    keyring: str | None = "",
    container: str | None = "",
) -> tuple[str, str | None, str | None, str | None]:
    """Return yt-dlp's cookiesfrombrowser tuple."""
    return (
        normalize_cookie_browser(browser),
        str(profile or "").strip() or None,
        str(keyring or "").strip().upper() or None,
        str(container or "").strip() or None,
    )


@dataclass(frozen=True)
class CookieSource:
    source: str = COOKIE_SOURCE_NONE
    file: str = ""
    browser: str = "chrome"
    profile: str = ""
    keyring: str = ""
    container: str = ""

    def normalized(self) -> "CookieSource":
        return CookieSource(
            source=normalize_cookie_source(self.source),
            file=str(self.file or "").strip(),
            browser=normalize_cookie_browser(self.browser),
            profile=str(self.profile or "").strip(),
            keyring=str(self.keyring or "").strip(),
            container=str(self.container or "").strip(),
        )

    def ytdlp_options(self) -> dict[str, Any]:
        value = self.normalized()
        if value.source == COOKIE_SOURCE_FILE and value.file:
            return {"cookiefile": value.file}
        if value.source == COOKIE_SOURCE_BROWSER:
            return {"cookiesfrombrowser": browser_cookie_spec(
                value.browser, value.profile, value.keyring, value.container
            )}
        return {}


@dataclass(slots=True)
class MaterializedCookieSource:
    """A yt-dlp Cookie snapshot and any temporary file owned by it."""

    options: dict[str, Any]
    normalized_source: str
    temporary_file: Path | None = None

    def cleanup(self) -> bool:
        path = self.temporary_file
        if path is None:
            return True
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return False
        self.temporary_file = None
        return True


def materialize_cookie_source(
    source: CookieSource,
    *,
    embedded_profile: str = EMBEDDED_DOWNLOAD_PROFILE,
) -> MaterializedCookieSource:
    """Resolve one Cookie source while retaining ownership of temp exports."""

    normalized = source.normalized()
    temporary_file: Path | None = None
    try:
        if normalized.source != COOKIE_SOURCE_EMBEDDED:
            return MaterializedCookieSource(
                options=normalized.ytdlp_options(),
                normalized_source=normalized.source,
            )
        from app.core.browser_cookies import CookieVault

        temporary_file = Path(
            CookieVault().create_temporary_netscape_file(embedded_profile)
        )
        if not temporary_file.is_file():
            raise FileNotFoundError(f"临时 Cookie 文件不存在：{temporary_file}")
        return MaterializedCookieSource(
            options={"cookiefile": str(temporary_file)},
            normalized_source=normalized.source,
            temporary_file=temporary_file,
        )
    except BaseException:
        if temporary_file is not None:
            try:
                temporary_file.unlink(missing_ok=True)
            except OSError:
                pass
        raise


def browser_cookie_count(browser: str, profile: str = "", keyring: str = "", container: str = "") -> int:
    """Read browser cookies through yt-dlp and return only a count."""
    try:
        from yt_dlp.cookies import extract_cookies_from_browser
    except Exception as exc:  # pragma: no cover - environment dependent
        raise RuntimeError("yt-dlp Cookie 组件不可用") from exc

    class _Logger:
        def debug(self, *_args, **_kwargs):
            return None

        info = debug
        warning = debug
        error = debug

    spec = browser_cookie_spec(browser, profile, keyring, container)
    jar = extract_cookies_from_browser(
        spec[0], spec[1], _Logger(), keyring=spec[2], container=spec[3]
    )
    return sum(1 for _ in jar)
