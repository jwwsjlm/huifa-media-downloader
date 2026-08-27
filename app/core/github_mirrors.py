from __future__ import annotations

import ipaddress
import hashlib
import re
from dataclasses import dataclass
from urllib.parse import urlsplit


ROUTE_AUTO = "auto"
ROUTE_DIRECT = "direct"
BUILTIN_GITHUB_MIRRORS: tuple[tuple[str, str, str, str], ...] = (
    ("gh-proxy", "GitHub Proxy", "https://gh-proxy.com/", "prefix"),
    ("ghfast", "GHFast", "https://ghfast.top/", "auto"),
    ("idayer", "IDayer", "https://gh.idayer.com/", "auto"),
    ("monlor", "Monlor", "https://gh.monlor.com/", "auto"),
    ("078465", "078465", "https://ghm.078465.xyz/", "auto"),
    ("tbap", "TBAP", "https://github.tbap.top/", "auto"),
    ("mxw", "MXW", "https://down.mxw.xx.kg/", "auto"),
    ("monkeyray", "MonkeyRay", "https://ghproxy.monkeyray.net/", "auto"),
    ("jasonzeng", "JasonZeng", "https://gh.jasonzeng.dev/", "auto"),
    ("akaere", "Akaere", "https://cdn.akaere.online/", "auto"),
    ("yylx", "YYLX", "https://git.yylx.win/", "auto"),
)

BUILTIN_JSDELIVR_CDNS: tuple[tuple[str, str, str], ...] = (
    ("jsdelivr", "jsDelivr", "https://cdn.jsdelivr.net/"),
    ("jsdelivr-fastly", "jsDelivr Fastly", "https://fastly.jsdelivr.net/"),
    ("jsdelivr-testingcf", "jsDelivr TestingCF", "https://testingcf.jsdelivr.net/"),
)


@dataclass(frozen=True)
class GithubDownloadRoute:
    id: str
    name: str
    base_url: str = ""
    third_party: bool = False
    kind: str = "prefix"
    metadata_supported: bool = True
    release_page_supported: bool = True
    asset_supported: bool = True


def normalize_github_route(value: str | None) -> str:
    route = str(value or "").strip().casefold()
    if route in {ROUTE_AUTO, ROUTE_DIRECT}:
        return route
    if route.startswith("mirror:") or route.startswith("custom:"):
        return route
    return ROUTE_AUTO


def _is_forbidden_host(hostname: str) -> bool:
    host = str(hostname or "").strip().rstrip(".").casefold()
    if not host or host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def is_public_http_url(value: str) -> bool:
    """Return whether a URL is HTTP(S) and not an obvious local/private target."""
    try:
        parsed = urlsplit(str(value or ""))
    except ValueError:
        return False
    return bool(
        parsed.scheme.casefold() in {"http", "https"}
        and parsed.hostname
        and not parsed.username
        and not parsed.password
        and not _is_forbidden_host(parsed.hostname)
    )


def normalize_mirror_base_url(value: str) -> str:
    raw = str(value or "").strip()
    if len(raw) > 500:
        raise ValueError("GitHub 代理地址过长")
    parsed = urlsplit(raw)
    scheme = parsed.scheme.casefold()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("GitHub 代理必须是完整的 HTTP 或 HTTPS 地址")
    if parsed.username or parsed.password:
        raise ValueError("GitHub 代理地址不能包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise ValueError("GitHub 代理地址不能包含查询参数或片段")
    if _is_forbidden_host(parsed.hostname):
        raise ValueError("GitHub 代理不能指向本机、私网或保留地址")
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if not path.endswith("/"):
        path += "/"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{scheme}://{parsed.hostname}{port}{path}"


def parse_custom_mirror_urls(value: str | None) -> tuple[str, ...]:
    parts = re.split(r"[\r\n,;|]+", str(value or ""))
    normalized: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if not part.strip():
            continue
        url = normalize_mirror_base_url(part)
        if url in seen:
            continue
        seen.add(url)
        normalized.append(url)
        if len(normalized) >= 12:
            break
    return tuple(normalized)


def github_download_routes(custom_urls: str | None = None) -> tuple[GithubDownloadRoute, ...]:
    routes = [GithubDownloadRoute(ROUTE_DIRECT, "GitHub 直连", kind="direct")]
    for route_id, name, base_url, kind in BUILTIN_GITHUB_MIRRORS:
        routes.append(GithubDownloadRoute(
            f"mirror:{route_id}", name, base_url, True, kind=kind
        ))
    for route_id, name, base_url in BUILTIN_JSDELIVR_CDNS:
        routes.append(GithubDownloadRoute(
            f"mirror:{route_id}",
            name,
            base_url,
            True,
            kind="jsdelivr",
            metadata_supported=True,
            release_page_supported=False,
            asset_supported=False,
        ))
    for base_url in parse_custom_mirror_urls(custom_urls):
        host = urlsplit(base_url).hostname or base_url
        stable_id = hashlib.sha256(base_url.encode("utf-8")).hexdigest()[:12]
        routes.append(GithubDownloadRoute(
            f"custom:{stable_id}", f"自定义 · {host}", base_url, True, kind="auto"
        ))
    return tuple(routes)


def route_download_url(
    route: GithubDownloadRoute,
    official_url: str,
    kind_override: str = "",
) -> str:
    kind = str(kind_override or route.kind or "prefix").strip().casefold()
    if not route.third_party:
        return official_url
    if kind == "jsdelivr":
        raise ValueError("jsDelivr 不支持通用 GitHub URL 前缀代理")
    if kind == "host":
        parsed = urlsplit(official_url)
        suffix = parsed.path.lstrip("/")
        if parsed.query:
            suffix += "?" + parsed.query
        return route.base_url + suffix
    return route.base_url + official_url


def route_metadata_probe_url(route: GithubDownloadRoute, repo: str = "yt-dlp/yt-dlp") -> str:
    if route.kind == "jsdelivr":
        return f"https://data.jsdelivr.com/v1/package/gh/{repo}"
    return route_download_url(
        route,
        f"https://api.github.com/repos/{repo}/releases/latest",
    )


def selected_download_routes(route_mode: str, custom_urls: str | None = None) -> tuple[GithubDownloadRoute, ...]:
    routes = github_download_routes(custom_urls)
    normalized = normalize_github_route(route_mode)
    if normalized == ROUTE_AUTO:
        return routes
    for route in routes:
        if route.id == normalized:
            return (route,)
    return (routes[0],)
