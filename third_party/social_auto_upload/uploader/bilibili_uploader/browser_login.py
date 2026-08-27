from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

import requests
from playwright.async_api import async_playwright

from conf import BASE_DIR, LOCAL_CHROME_PATH
from utils.base_social_media import set_init_script


_BILITV_APP_KEY = "4409e2ce8ffd12b8"
_BILITV_APP_SECRET = "59b43e04ad6965f34319062b478f83dd"
_AUTH_CODE_URL = "https://passport.bilibili.com/x/passport-tv-login/qrcode/auth_code"
_CONFIRM_URL = "https://passport.bilibili.com/x/passport-tv-login/h5/qrcode/confirm"
_POLL_URL = "https://passport.bilibili.com/x/passport-tv-login/qrcode/poll"
_LOGIN_URL = "https://passport.bilibili.com/login"
_BILIAPP_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64; rv:38.0) "
    "Gecko/20100101 Firefox/38.0 Iceweasel/38.2.1 BiliApp"
)


class BilibiliBrowserLoginError(RuntimeError):
    pass


def _profile_dir(account_name: str) -> Path:
    raw = str(account_name or "default").strip() or "default"
    readable = "".join(char if char.isalnum() or char in "-_" else "_" for char in raw)[:48]
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    path = Path(BASE_DIR) / "browser_profiles" / "bilibili" / f"{readable or 'account'}-{digest}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _signed_form(values: dict[str, Any]) -> dict[str, Any]:
    normalized = sorted((str(key), str(value)) for key, value in values.items())
    encoded = urlencode(normalized)
    sign = hashlib.md5(f"{encoded}{_BILITV_APP_SECRET}".encode("utf-8")).hexdigest()
    return {**values, "sign": sign}


def _post_json(
    session: requests.Session,
    url: str,
    form: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    response = session.post(url, data=form, headers=headers, timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise BilibiliBrowserLoginError("Bilibili 登录接口返回了无效数据")
    return payload


def exchange_web_cookies_for_login_info(
    sess_data: str,
    bili_jct: str,
    *,
    post_json: Callable[[requests.Session, str, dict[str, Any], dict[str, str] | None], dict[str, Any]] = _post_json,
) -> dict[str, Any]:
    """Mirror biliup's Web Cookie -> BiliTV OAuth credential flow."""
    if not sess_data or not bili_jct:
        raise BilibiliBrowserLoginError("Bilibili 登录 Cookie 不完整")

    session = requests.Session()
    session.headers.update({"User-Agent": _BILIAPP_USER_AGENT})
    timestamp = int(time.time())
    auth_form = _signed_form({
        "appkey": _BILITV_APP_KEY,
        "local_id": "0",
        "ts": timestamp,
    })
    auth_payload = post_json(session, _AUTH_CODE_URL, auth_form, None)
    if int(auth_payload.get("code", -1)) != 0:
        raise BilibiliBrowserLoginError(
            f"Bilibili 授权初始化失败（代码 {auth_payload.get('code', 'unknown')}）"
        )
    auth_code = str((auth_payload.get("data") or {}).get("auth_code") or "")
    if not auth_code:
        raise BilibiliBrowserLoginError("Bilibili 授权初始化未返回 auth_code")

    confirm_payload = post_json(
        session,
        _CONFIRM_URL,
        {"auth_code": auth_code, "csrf": bili_jct, "scanning_type": 3},
        {
            "Cookie": f"SESSDATA={sess_data}; bili_jct={bili_jct}",
            "User-Agent": _BILIAPP_USER_AGENT,
        },
    )
    if int(confirm_payload.get("code", -1)) != 0:
        raise BilibiliBrowserLoginError(
            f"Bilibili 网页登录授权确认失败（代码 {confirm_payload.get('code', 'unknown')}）"
        )

    poll_form = _signed_form({
        "appkey": _BILITV_APP_KEY,
        "auth_code": auth_code,
        "local_id": "0",
        "ts": timestamp,
    })
    for _attempt in range(30):
        poll_payload = post_json(session, _POLL_URL, poll_form, None)
        code = int(poll_payload.get("code", -1))
        if code == 86039:
            time.sleep(1)
            continue
        if code != 0:
            raise BilibiliBrowserLoginError(f"Bilibili 授权失败（代码 {code}）")
        login_info = poll_payload.get("data")
        if not isinstance(login_info, dict):
            raise BilibiliBrowserLoginError("Bilibili 授权结果缺少登录信息")
        token_info = login_info.get("token_info")
        cookie_info = login_info.get("cookie_info")
        if not isinstance(token_info, dict) or not isinstance(cookie_info, dict):
            raise BilibiliBrowserLoginError("Bilibili 授权结果缺少 Cookie 或令牌")
        if not token_info.get("access_token") or not token_info.get("refresh_token"):
            raise BilibiliBrowserLoginError("Bilibili 授权结果中的令牌不完整")
        result = dict(login_info)
        result["platform"] = "BiliTV"
        result.setdefault("sso", [])
        return result
    raise BilibiliBrowserLoginError("等待 Bilibili 授权结果超时")


def _atomic_write_login_info(destination: Path, login_info: dict[str, Any]) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.biliup-write-{os.getpid()}-{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as file_obj:
            json.dump(login_info, file_obj, ensure_ascii=False, indent=2)
            file_obj.write("\n")
            file_obj.flush()
            os.fsync(file_obj.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


async def login_bilibili_with_playwright(
    account_name: str,
    account_file: Path,
    *,
    headless: bool = False,
) -> dict[str, Any]:
    chromium_path = Path(LOCAL_CHROME_PATH) if LOCAL_CHROME_PATH else None
    if chromium_path is None or not chromium_path.is_file():
        raise BilibiliBrowserLoginError("程序包缺少内置 Chromium")

    closed = asyncio.Event()
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            user_data_dir=str(_profile_dir(account_name)),
            executable_path=str(chromium_path),
            headless=bool(headless),
            args=["--hide-crash-restore-bubble"],
        )
        context.on("close", lambda *_args: closed.set())
        try:
            await set_init_script(context)
            pages = [page for page in context.pages if not page.is_closed()]
            page = pages[-1] if pages else await context.new_page()
            await page.goto(_LOGIN_URL, wait_until="domcontentloaded", timeout=45_000)

            while not closed.is_set():
                cookies = await context.cookies(
                    ["https://www.bilibili.com", "https://passport.bilibili.com"]
                )
                by_name = {str(cookie.get("name")): str(cookie.get("value") or "") for cookie in cookies}
                if by_name.get("SESSDATA") and by_name.get("bili_jct"):
                    login_info = await asyncio.to_thread(
                        exchange_web_cookies_for_login_info,
                        by_name["SESSDATA"],
                        by_name["bili_jct"],
                    )
                    _atomic_write_login_info(account_file, login_info)
                    return {
                        "success": True,
                        "message": "Bilibili 登录信息已保存",
                        "account_file": str(account_file),
                    }
                try:
                    await asyncio.wait_for(closed.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    pass
        finally:
            if not closed.is_set():
                try:
                    await context.close()
                except Exception:
                    pass

    return {
        "success": False,
        "message": "浏览器已关闭，但尚未取得 Bilibili 登录信息",
        "account_file": str(account_file),
    }
