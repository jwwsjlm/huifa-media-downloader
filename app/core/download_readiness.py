from __future__ import annotations

from app.core.cookie_sources import (
    COOKIE_BROWSER_LABELS,
    COOKIE_SOURCE_BROWSER,
    COOKIE_SOURCE_EMBEDDED,
    COOKIE_SOURCE_FILE,
    EMBEDDED_DOWNLOAD_PROFILE,
    normalize_cookie_browser,
    normalize_cookie_source,
)
from app.core.paths import resolve_portable_path
from app.core.update_service import (
    runtime_component_presence,
    ytdlp_python_core_available,
)
from app.core.ytdlp_core_selection import normalize_ytdlp_core_mode
from app.core.ytdlp_ejs import normalize_ytdlp_ejs_source


ReadinessRow = dict[str, str]
_UNAVAILABLE_COMPONENT_STATES = {"未安装", "未检测", "不可用"}


def _component_available(state: str) -> bool:
    return state not in _UNAVAILABLE_COMPONENT_STATES


def _download_core_readiness(core_mode: str) -> tuple[bool, ReadinessRow]:
    version, source, runtime = runtime_component_presence("yt-dlp")
    detected = _component_available(version)
    external = detected and bool(runtime) and runtime != "内置 Python 模块"
    builtin = ytdlp_python_core_available()

    if core_mode == "external":
        available = external
        label = "外置核心"
        unavailable = "已选择外置核心，但没有检测到可运行的 yt-dlp.exe"
    elif core_mode == "builtin":
        available = builtin
        label = "内置核心"
        unavailable = "已选择内置核心，但当前主程序没有可用的内置 yt-dlp 模块"
    else:
        available = detected
        label = "自动选择"
        unavailable = source or "未检测到可用的外置 yt-dlp.exe 或内置模块"

    detail = (
        f"{label} · 版本 {version} · {source or '未检测到来源'}"
        if available
        else f"{label} · {unavailable}"
    )
    return available, {
        "name": "下载核心（yt-dlp）",
        "state": "可用" if available else "不可用",
        "detail": detail,
    }


def _media_tool_readiness(
    ffmpeg_path: str,
    ffprobe_path: str,
) -> tuple[bool, list[ReadinessRow]]:
    ffmpeg_state, ffmpeg_source, ffmpeg_runtime = runtime_component_presence(
        "FFmpeg",
        ffmpeg_path,
    )
    ffprobe_state, ffprobe_source, ffprobe_runtime = (
        runtime_component_presence("FFprobe", ffmpeg_path, ffprobe_path)
        if str(ffprobe_path or "").strip()
        else runtime_component_presence("FFprobe", ffmpeg_path)
    )
    ffmpeg_ok = _component_available(ffmpeg_state)
    ffprobe_ok = _component_available(ffprobe_state)
    return ffmpeg_ok and ffprobe_ok, [
        {
            "name": "合并工具（FFmpeg）",
            "state": "可用" if ffmpeg_ok else "不可用",
            "detail": (
                f"{ffmpeg_source or '已找到'} · {ffmpeg_runtime}"
                if ffmpeg_ok
                else f"缺失或损坏；{ffmpeg_source or ffmpeg_state}"
            ),
        },
        {
            "name": "媒体检查（FFprobe）",
            "state": "可用" if ffprobe_ok else "不可用",
            "detail": (
                f"{ffprobe_source or '已找到'} · {ffprobe_runtime}"
                if ffprobe_ok
                else f"缺失或损坏；{ffprobe_source or ffprobe_state}"
            ),
        },
    ]


def _javascript_readiness(
    deno_path: str,
    ejs_source: str,
) -> list[ReadinessRow]:
    deno_state, deno_source, deno_runtime = runtime_component_presence(
        "Deno",
        deno_path,
    )
    deno_ok = _component_available(deno_state)
    local_state, _local_source, local_runtime = runtime_component_presence(
        "yt-dlp-ejs"
    )
    local_ok = _component_available(local_state)
    source = normalize_ytdlp_ejs_source(ejs_source)
    ejs_ok = deno_ok and (local_ok if source == "local" else True)
    details = {
        "auto": (
            f"软件本地版本 {local_state} · {local_runtime}"
            if local_ok
            else "软件本地核心未安装；当前会临时使用 Deno/npm，建议在运行组件中安装本地版本"
        ),
        "npm": "从 npm 按需获取；不依赖 GitHub，要求 Deno 可用",
        "github": "从 GitHub 按需获取；网络需能访问 GitHub",
        "local": (
            f"仅使用软件本地版本 {local_state} · {local_runtime}"
            if local_ok
            else "仅本地模式，但软件本地 yt-dlp-ejs 尚未安装"
        ),
    }
    return [
        {
            "name": "JavaScript 运行时（Deno）",
            "state": "可用" if deno_ok else "建议安装",
            "detail": (
                f"已找到 · {deno_source or '未检测到来源'} · {deno_runtime}"
                if deno_ok
                else "未安装；yt-dlp 推荐 Deno 处理 YouTube JavaScript challenge"
            ),
        },
        {
            "name": "YouTube JS 支持（yt-dlp-ejs）",
            "state": "可用" if ejs_ok else "建议安装",
            "detail": details[source],
        },
    ]


def _download_directory_readiness(download_dir: str) -> tuple[bool, ReadinessRow]:
    raw_dir = str(download_dir or "").strip()
    try:
        if not raw_dir:
            raise OSError("未设置下载保存目录")
        path = resolve_portable_path(raw_dir)
        if path.exists() and not path.is_dir():
            raise OSError("该路径不是目录")
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return False, {
            "name": "下载保存目录",
            "state": "不可用",
            "detail": str(exc),
        }
    return True, {
        "name": "下载保存目录",
        "state": "可用",
        "detail": str(path),
    }


def _cookie_readiness(
    cookie_file: str,
    cookie_source: str,
    cookie_browser: str,
) -> tuple[bool, ReadinessRow]:
    source = normalize_cookie_source(cookie_source)
    raw_cookie = str(cookie_file or "").strip()
    if source == COOKIE_SOURCE_EMBEDDED:
        try:
            from app.core.browser_cookies import CookieVault

            count = CookieVault().count(EMBEDDED_DOWNLOAD_PROFILE)
        except Exception:
            count = 0
        # Cookies are optional for public content. An empty embedded profile is
        # actionable information, not a reason to mark the whole downloader
        # unusable for every site.
        return True, {
            "name": "下载 Cookie",
            "state": "可用" if count else "未获取",
            "detail": (
                f"内置浏览器已加密保存 {count} 条 Cookie"
                if count
                else "内置浏览器尚未保存 Cookie；公开内容仍可下载，需要登录时再打开对应站点"
            ),
        }
    if source == COOKIE_SOURCE_BROWSER:
        browser = normalize_cookie_browser(cookie_browser)
        return True, {
            "name": "下载 Cookie",
            "state": "已配置",
            "detail": (
                f"将按需读取 {COOKIE_BROWSER_LABELS.get(browser, browser)} Cookie；"
                "不会复制到应用数据库"
            ),
        }
    if not raw_cookie:
        return True, {
            "name": "下载 Cookie",
            "state": "未配置",
            "detail": "公开内容通常不需要；私密、限龄或需登录内容请在设置中选择浏览器 Cookie 或 Netscape 文件",
        }

    path = resolve_portable_path(raw_cookie)
    available = path.is_file()
    return available, {
        "name": "下载 Cookie",
        "state": "可用" if available else "不可用",
        "detail": (
            str(path)
            if available
            else "已配置的 Cookie 文件不存在或不是普通文件"
        ),
    }


def download_readiness_report(
    download_dir: str,
    cookie_file: str = "",
    ffmpeg_path: str = "",
    cookie_source: str = "none",
    cookie_browser: str = "chrome",
    ytdlp_core_mode: str = "auto",
    ffprobe_path: str = "",
    deno_path: str = "",
    ytdlp_ejs_source: str = "auto",
) -> tuple[bool, list[ReadinessRow]]:
    """Inspect the local download environment without contacting any site."""

    ready, core_row = _download_core_readiness(
        normalize_ytdlp_core_mode(ytdlp_core_mode)
    )
    media_ready, media_rows = _media_tool_readiness(ffmpeg_path, ffprobe_path)
    directory_ready, directory_row = _download_directory_readiness(download_dir)
    cookie_ready, cookie_row = _cookie_readiness(
        cookie_file,
        cookie_source,
        cookie_browser,
    )
    rows = [
        core_row,
        *media_rows,
        *_javascript_readiness(deno_path, ytdlp_ejs_source),
        directory_row,
        cookie_row,
    ]
    return ready and media_ready and directory_ready and cookie_ready, rows
