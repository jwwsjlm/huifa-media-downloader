from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import os
import sys
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import Any, Awaitable

from app.core.browser_cookies import BrowserCookie, CookieVault, cookies_from_playwright
from app.core.paths import application_dir, data_dir, tool_runtime_roots


class SocialAutoUploadError(RuntimeError):
    """Raised when the embedded publishing core cannot complete an operation."""


_LOGIN_FUNCTIONS = {
    "douyin": "login_douyin_account",
    "kuaishou": "login_kuaishou_account",
    "xiaohongshu": "login_xiaohongshu_account",
    "bilibili": "login_bilibili_account",
    "tencent": "login_tencent_account",
    "baijiahao": "login_baijiahao_account",
    "alipay": "login_alipay_account",
    "weibo": "login_weibo_account",
    "hupu": "login_hupu_account",
    "youtube": "login_youtube_account",
}
_CHECK_FUNCTIONS = {
    platform: name.replace("login_", "check_")
    for platform, name in _LOGIN_FUNCTIONS.items()
}
_IMPORT_LOCK = threading.RLock()


@dataclass(slots=True)
class _DownloadCookieBrowserState:
    closed: asyncio.Event
    saved_count: int = 0
    saved_signature: tuple[tuple[object, ...], ...] | None = None
    current_url: str = "about:blank"


def vendor_root() -> Path:
    """Return the fixed vendored source tree in source or frozen layouts."""
    candidates: list[Path] = []
    for root in tool_runtime_roots(application_dir(), data_dir()):
        candidates.append(root / "third_party" / "social_auto_upload")
    candidates.append(Path(__file__).resolve().parents[3] / "third_party" / "social_auto_upload")
    for candidate in candidates:
        if (candidate / "sau_cli.py").is_file() and (candidate / "LICENSE").is_file():
            return candidate.resolve()
    raise SocialAutoUploadError("程序包缺少内置 social-auto-upload 源码，请重新下载完整程序")


def vendor_commit() -> str:
    try:
        return (vendor_root() / "UPSTREAM_COMMIT").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def runtime_home() -> Path:
    home = data_dir() / "browser" / "sau-cookies"
    home.mkdir(parents=True, exist_ok=True)
    (home / "cookies").mkdir(parents=True, exist_ok=True)
    (home / "db").mkdir(parents=True, exist_ok=True)
    return home.resolve()


def download_browser_profile_dir(profile_id: str = "download") -> Path:
    """Return an app-local Chromium profile directory for download cookies."""
    raw = str(profile_id or "download").strip() or "download"
    readable = "".join(char if char.isalnum() or char in "-_" else "_" for char in raw)[:48]
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    profile = data_dir() / "browser" / "profiles" / f"{readable or 'profile'}-{digest}"
    profile.mkdir(parents=True, exist_ok=True)
    return profile.resolve()


@lru_cache(maxsize=1)
def resolve_chromium_executable() -> Path | None:
    """Find the app-owned Playwright Chromium; never inspect system browsers."""
    roots: list[Path] = []
    for root in tool_runtime_roots(application_dir(), data_dir()):
        roots.append(root / "tools" / "chromium")
    explicit = os.environ.get("HUIFA_CHROMIUM_PATH", "").strip()
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return path.resolve()
    for root in roots:
        if not root.is_dir():
            continue
        direct = root / "chrome.exe"
        if direct.is_file():
            return direct.resolve()
        matches = list(root.rglob("chrome.exe"))
        if matches:
            def browser_revision(path: Path) -> int:
                for parent in path.parents:
                    name = parent.name.casefold()
                    if name.startswith("chromium-"):
                        try:
                            return int(name.rsplit("-", 1)[-1])
                        except ValueError:
                            break
                return 0

            return max(
                matches,
                key=lambda path: (browser_revision(path), -len(path.parts), str(path)),
            ).resolve()
    return None


def _prepare_environment() -> None:
    root = vendor_root()
    home = runtime_home()
    chromium = resolve_chromium_executable()
    if chromium is None:
        raise SocialAutoUploadError("程序包缺少内置 Chromium，请重新下载完整程序")
    os.environ["HUIFA_SAU_HOME"] = str(home)
    os.environ["HUIFA_SAU_DATA_DIR"] = str(home)
    os.environ["HUIFA_SAU_HEADLESS"] = "1"
    os.environ["PYTHONUTF8"] = "1"
    os.environ["HUIFA_CHROMIUM_PATH"] = str(chromium)
    browser_root = next(
        (parent for parent in chromium.parents if parent.name.casefold() == "chromium"),
        chromium.parent,
    )
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_root)
    root_text = str(root)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)


def _load_conf(root: Path) -> None:
    conf_path = root / "conf.py"
    current = sys.modules.get("conf")
    if current is not None and Path(str(getattr(current, "__file__", ""))).resolve() == conf_path.resolve():
        return
    spec = importlib.util.spec_from_file_location("conf", conf_path)
    if spec is None or spec.loader is None:
        raise SocialAutoUploadError("无法加载内置 social-auto-upload 配置桥接模块")
    module = importlib.util.module_from_spec(spec)
    sys.modules["conf"] = module
    spec.loader.exec_module(module)


@lru_cache(maxsize=1)
def _load_upstream() -> ModuleType:
    with _IMPORT_LOCK:
        _prepare_environment()
        root = vendor_root()
        _load_conf(root)
        try:
            module = importlib.import_module("sau_cli")
        except ModuleNotFoundError as exc:
            missing = str(getattr(exc, "name", "") or exc)
            raise SocialAutoUploadError(
                f"程序缺少内置发布核心依赖 {missing}，请重新下载完整程序后重试"
            ) from exc
        except Exception as exc:
            raise SocialAutoUploadError(f"内置 social-auto-upload 初始化失败：{exc}") from exc
        origin = Path(str(getattr(module, "__file__", ""))).resolve()
        try:
            origin.relative_to(root)
        except ValueError as exc:
            raise SocialAutoUploadError(f"加载到了非内置 social-auto-upload 源码：{origin}") from exc
        return module


def core_status() -> tuple[bool, str]:
    try:
        root = vendor_root()
        commit = vendor_commit()
        for dependency in ("playwright", "requests"):
            if importlib.util.find_spec(dependency) is None:
                return False, f"内置源码存在，但缺少 Python 依赖：{dependency}"
        if resolve_chromium_executable() is None:
            return False, "内置源码存在，但程序本地 Chromium 尚未安装"
        return True, f"内置源码 {commit[:12]}（{root.name}）"
    except Exception as exc:
        return False, str(exc)


async def _cancellable(coroutine: Awaitable[Any], cancel_event: threading.Event | None) -> Any:
    task = asyncio.create_task(coroutine)
    try:
        while not task.done():
            if cancel_event is not None and cancel_event.is_set():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                raise InterruptedError("任务已取消")
            await asyncio.sleep(0.2)
        return await task
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def _close_unstarted_awaitable(awaitable: Awaitable[Any]) -> None:
    close = getattr(awaitable, "close", None)
    if callable(close):
        close()


def _run(coroutine: Awaitable[Any], cancel_event: threading.Event | None = None) -> Any:
    if cancel_event is not None and cancel_event.is_set():
        _close_unstarted_awaitable(coroutine)
        raise InterruptedError("任务已取消")
    try:
        return asyncio.run(_cancellable(coroutine, cancel_event))
    except KeyboardInterrupt as exc:
        raise InterruptedError("任务已取消") from exc


def account_login(
    platform: str,
    account: str,
    *,
    headed: bool = True,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    module = _load_upstream()
    key = str(platform or "").strip().casefold()
    function_name = _LOGIN_FUNCTIONS.get(key)
    if not function_name:
        raise SocialAutoUploadError(f"平台 {key or '未知'} 尚未接入内置发布核心")
    function = getattr(module, function_name)
    coroutine = function(account, headless=not headed)
    result = _run(coroutine, cancel_event)
    if isinstance(result, dict):
        return result
    return {"success": bool(result), "message": "登录完成" if result else "登录失败"}


def account_check(
    platform: str,
    account: str,
    *,
    cancel_event: threading.Event | None = None,
) -> bool:
    module = _load_upstream()
    key = str(platform or "").strip().casefold()
    function_name = _CHECK_FUNCTIONS.get(key)
    if not function_name:
        raise SocialAutoUploadError(f"平台 {key or '未知'} 尚未接入内置发布核心")
    return bool(_run(getattr(module, function_name)(account), cancel_event))


def _browser_cookie_signature(
    cookies: list[BrowserCookie],
) -> tuple[tuple[object, ...], ...]:
    """Return an order-independent snapshot including security attributes."""

    return tuple(sorted(
        (
            cookie.domain,
            cookie.path,
            cookie.name,
            cookie.value,
            cookie.expires,
            cookie.http_only,
            cookie.secure,
            cookie.same_site,
            cookie.host_only,
        )
        for cookie in cookies
    ))


async def _restore_download_browser_cookies(
    context: Any,
    vault_cookies: list[BrowserCookie],
) -> None:
    """Restore only live vault cookies absent from the Chromium profile."""

    profile_cookies = cookies_from_playwright(await context.cookies())
    profile_keys = {cookie.key for cookie in profile_cookies}
    now = int(time.time())
    restorable = [
        cookie
        for cookie in vault_cookies
        if cookie.key not in profile_keys
        and (cookie.expires <= 0 or cookie.expires > now)
    ]
    if restorable:
        await context.add_cookies([cookie.to_playwright() for cookie in restorable])


async def _save_download_browser_cookies(
    context: Any,
    vault: CookieVault,
    profile_id: str,
    state: _DownloadCookieBrowserState,
) -> None:
    cookies = cookies_from_playwright(await context.cookies())
    signature = _browser_cookie_signature(cookies)
    if signature != state.saved_signature:
        # The persistent Chromium profile is the complete browser session.
        # The encrypted vault is the yt-dlp-facing mirror.
        vault.save(profile_id, cookies)
        state.saved_signature = signature
    state.saved_count = len(cookies)


def _update_download_browser_url(context: Any, state: _DownloadCookieBrowserState) -> list[Any]:
    pages = [page for page in context.pages if not page.is_closed()]
    if pages:
        state.current_url = str(pages[-1].url or state.current_url)
    return pages


async def _run_download_browser_loop(
    context: Any,
    vault: CookieVault,
    profile_id: str,
    state: _DownloadCookieBrowserState,
    cancel_event: threading.Event | None,
) -> None:
    while not state.closed.is_set():
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("任务已取消")
        _update_download_browser_url(context, state)
        try:
            await _save_download_browser_cookies(context, vault, profile_id, state)
        except Exception:
            if state.closed.is_set():
                break
            raise
        try:
            await asyncio.wait_for(state.closed.wait(), timeout=0.2)
        except asyncio.TimeoutError:
            pass


async def _close_download_browser_context(
    context: Any,
    vault: CookieVault,
    profile_id: str,
    state: _DownloadCookieBrowserState,
) -> None:
    if state.closed.is_set():
        return
    # A cancellation can arrive between two polling ticks. Capture once more
    # before closing so a just-completed login is immediately usable by yt-dlp.
    try:
        await _save_download_browser_cookies(context, vault, profile_id, state)
    except Exception:
        pass
    try:
        await context.close()
    except Exception:
        pass


async def _download_cookie_browser_session(
    profile_id: str,
    cancel_event: threading.Event | None = None,
    *,
    headless: bool = False,
) -> dict[str, Any]:
    """Open the app-owned persistent browser and mirror all site cookies to the vault."""
    _prepare_environment()
    chromium = resolve_chromium_executable()
    if chromium is None:
        raise SocialAutoUploadError("程序包缺少内置 Chromium，请重新下载完整程序")

    # Loading the vendor module prepares the bundled stealth script without
    # handing control to any platform-specific login implementation.
    _load_upstream()
    set_init_script = importlib.import_module("utils.base_social_media").set_init_script
    from playwright.async_api import async_playwright

    profile_dir = download_browser_profile_dir(profile_id)
    vault = CookieVault()
    vault_cookies = vault.load(profile_id)
    state = _DownloadCookieBrowserState(asyncio.Event())

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            executable_path=str(chromium),
            headless=bool(headless),
            args=["--hide-crash-restore-bubble"],
        )
        context.on("close", lambda *_args: state.closed.set())
        try:
            await set_init_script(context)
            # Chromium may intentionally drop session-only cookies on a clean
            # exit. Rehydrate only cookies missing from the on-disk Profile;
            # Profile values always win, and expired vault entries stay gone.
            await _restore_download_browser_cookies(context, vault_cookies)
            pages = _update_download_browser_url(context, state)
            if not pages:
                await context.new_page()
            await _run_download_browser_loop(
                context,
                vault,
                profile_id,
                state,
                cancel_event,
            )
        finally:
            await _close_download_browser_context(context, vault, profile_id, state)

    if state.saved_count:
        return {
            "success": True,
            "status": "cookies_saved",
            "message": f"已持久化保存 {state.saved_count} 条 Cookie",
            "profile_dir": str(profile_dir),
            "current_url": state.current_url,
        }
    return {
        "success": False,
        "status": "closed_empty",
        "message": "浏览器已关闭，但当前 Profile 中没有 Cookie",
        "profile_dir": str(profile_dir),
        "current_url": state.current_url,
    }


def open_download_cookie_browser(
    profile_id: str = "download",
    *,
    cancel_event: threading.Event | None = None,
    headless: bool = False,
) -> dict[str, Any]:
    """Run the generic persistent sign-in browser outside the UI thread."""
    return _run(
        _download_cookie_browser_session(
            profile_id,
            cancel_event,
            headless=headless,
        ),
        cancel_event,
    )


def publish_video(
    platform: str,
    payload: dict[str, Any],
    *,
    cancel_event: threading.Event | None = None,
) -> str:
    """Dispatch one structured upload in-process without CLI argument parsing."""
    module = _load_upstream()
    platform_key = str(platform or "").strip().casefold()
    if platform_key not in _LOGIN_FUNCTIONS:
        raise SocialAutoUploadError(
            f"平台 {platform_key or '未知'} 尚未接入内置发布核心"
        )
    publish = getattr(module, "publish_video_payload", None)
    if not callable(publish):
        raise SocialAutoUploadError("内置发布核心缺少结构化视频发布接口")
    try:
        result = _run(publish(platform_key, dict(payload)), cancel_event)
    except InterruptedError:
        raise
    except Exception as exc:
        raise SocialAutoUploadError(f"内置发布核心执行失败：{exc}") from exc
    return str(result or f"{platform_key} 发布流程已完成")
