from __future__ import annotations

"""Encrypted cookie storage and format conversion for the embedded browser.

Cookie values must never enter settings.ini, SQLite, logs, or diagnostic
bundles.  The application keeps one Windows-DPAPI-encrypted master copy and
materialises short-lived compatibility files only when yt-dlp or SAU needs
one.
"""

import ctypes
import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from ctypes import wintypes
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from app.core.paths import data_dir


_VAULT_VERSION = 1
_DPAPI_ENTROPY = b"huifa-video-downloader/browser-cookies/v1"
_VAULT_LOCKS_GUARD = threading.Lock()
_VAULT_LOCKS: dict[str, threading.RLock] = {}
_TEMP_EXPORT_CLEANUP_GUARD = threading.Lock()
_TEMP_EXPORTS_CLEANED = False


class CookieVaultError(RuntimeError):
    """A persistent Cookie vault could not be read or updated safely."""


def _vault_lock(path: Path) -> threading.RLock:
    try:
        key = os.path.normcase(str(path.resolve(strict=False)))
    except OSError:
        key = os.path.normcase(str(path.absolute()))
    with _VAULT_LOCKS_GUARD:
        lock = _VAULT_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _VAULT_LOCKS[key] = lock
        return lock


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=path.parent,
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def cleanup_stale_cookie_exports(temp_root: str | Path | None = None) -> int:
    """Remove plaintext Cookie snapshots left by an earlier app process."""

    root = Path(temp_root) if temp_root is not None else data_dir() / "temp"
    removed = 0
    try:
        candidates = tuple(root.glob("huifa-cookie-*.txt"))
    except OSError:
        return 0
    for path in candidates:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue
        removed += 1
    return removed


def _cleanup_stale_cookie_exports_once(temp_root: Path) -> None:
    global _TEMP_EXPORTS_CLEANED
    with _TEMP_EXPORT_CLEANUP_GUARD:
        if _TEMP_EXPORTS_CLEANED:
            return
        cleanup_stale_cookie_exports(temp_root)
        _TEMP_EXPORTS_CLEANED = True


@dataclass(frozen=True, slots=True)
class BrowserCookie:
    name: str
    value: str
    domain: str
    path: str = "/"
    expires: int = 0
    http_only: bool = False
    secure: bool = False
    same_site: str = "Lax"
    host_only: bool = False

    def normalized(self, default_domain: str = "") -> "BrowserCookie | None":
        name = str(self.name or "").strip()
        domain = str(self.domain or default_domain or "").strip().lower()
        if not name or not domain:
            return None
        if "://" in domain:
            try:
                domain = (urlparse(domain).hostname or "").lower()
            except ValueError:
                return None
        if domain.casefold().startswith("#httponly_"):
            domain = domain[len("#HttpOnly_"):]
        if not domain:
            return None
        path = str(self.path or "/") or "/"
        value = str(self.value or "")
        if any(char in field for field in (name, value, domain, path) for char in ("\r", "\n", "\t")):
            return None
        same_site = str(self.same_site or "Lax").strip().capitalize()
        if same_site not in {"Strict", "Lax", "None"}:
            same_site = "Lax"
        try:
            expires = max(0, int(float(self.expires or 0)))
        except (TypeError, ValueError, OverflowError):
            expires = 0
        return BrowserCookie(
            name=name,
            value=value,
            domain=domain,
            path=path,
            expires=expires,
            http_only=bool(self.http_only),
            secure=bool(self.secure),
            same_site=same_site,
            host_only=bool(self.host_only),
        )

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.domain.casefold(), self.path, self.name)

    def to_playwright(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "domain": self.domain,
            "path": self.path,
            "expires": self.expires if self.expires > 0 else -1,
            "httpOnly": self.http_only,
            "secure": self.secure,
            "sameSite": self.same_site,
        }


def deduplicate_cookies(cookies: Iterable[BrowserCookie]) -> list[BrowserCookie]:
    values: dict[tuple[str, str, str], BrowserCookie] = {}
    for raw in cookies:
        cookie = raw.normalized()
        if cookie is not None:
            values[cookie.key] = cookie
    return sorted(values.values(), key=lambda item: item.key)


def cookies_from_playwright(value: str | bytes | dict[str, Any] | list[Any]) -> list[BrowserCookie]:
    if isinstance(value, (str, bytes)):
        value = json.loads(value)
    rows = value.get("cookies", []) if isinstance(value, dict) else value
    cookies: list[BrowserCookie] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        domain = str(row.get("domain") or "")
        cookie = BrowserCookie(
            name=str(row.get("name") or ""),
            value=str(row.get("value") or ""),
            domain=domain,
            path=str(row.get("path") or "/"),
            expires=row.get("expires") or 0,
            http_only=bool(row.get("httpOnly", row.get("http_only", False))),
            secure=bool(row.get("secure", False)),
            same_site=str(row.get("sameSite", row.get("same_site", "Lax")) or "Lax"),
            host_only=bool(row.get("hostOnly", row.get("host_only", not domain.startswith(".")))),
        ).normalized()
        if cookie is not None:
            cookies.append(cookie)
    return deduplicate_cookies(cookies)


def netscape_cookie_text(cookies: Iterable[BrowserCookie]) -> str:
    lines = ["# Netscape HTTP Cookie File", "# Generated temporarily by Huifa; do not share this file."]
    for cookie in deduplicate_cookies(cookies):
        domain = cookie.domain
        if cookie.http_only:
            domain = "#HttpOnly_" + domain
        include_subdomains = "FALSE" if cookie.host_only else "TRUE"
        lines.append("\t".join((
            domain,
            include_subdomains,
            cookie.path,
            "TRUE" if cookie.secure else "FALSE",
            str(cookie.expires if cookie.expires > 0 else 0),
            cookie.name,
            cookie.value,
        )))
    return "\n".join(lines) + "\n"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(value: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(value)
    return _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _dpapi(value: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise RuntimeError("内置浏览器 Cookie 加密存储仅支持 Windows DPAPI")
    source, source_buffer = _blob(value)
    entropy, entropy_buffer = _blob(_DPAPI_ENTROPY)
    destination = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if protect:
        ok = function(
            ctypes.byref(source), None, ctypes.byref(entropy), None, None,
            0x01, ctypes.byref(destination),
        )
    else:
        ok = function(
            ctypes.byref(source), None, ctypes.byref(entropy), None, None,
            0x01, ctypes.byref(destination),
        )
    # Keep the backing buffers alive until CryptProtectData returns.
    _ = source_buffer, entropy_buffer
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(destination.pbData, destination.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(destination.pbData)


def _profile_filename(profile_id: str) -> str:
    raw = str(profile_id or "default").strip() or "default"
    readable = "".join(char if char.isalnum() or char in "-_" else "_" for char in raw)[:48]
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{readable or 'profile'}-{digest}.bin"


class CookieVault:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root is not None else data_dir() / "browser" / "cookies"

    def path_for(self, profile_id: str) -> Path:
        return self.root / _profile_filename(profile_id)

    def _recovery_notice_path(self, profile_id: str) -> Path:
        return self.path_for(profile_id).with_suffix(".recovery.txt")

    def consume_recovery_notice(self, profile_id: str) -> str:
        """Return and clear a persisted notice left by an unreadable vault."""
        marker = self._recovery_notice_path(profile_id)
        with _vault_lock(self.path_for(profile_id)):
            try:
                message = marker.read_text(encoding="utf-8").strip()
            except OSError:
                return ""
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                pass
            return message

    def load(self, profile_id: str) -> list[BrowserCookie]:
        path = self.path_for(profile_id)
        with _vault_lock(path):
            if not path.is_file():
                return []
            try:
                encrypted = path.read_bytes()
            except FileNotFoundError:
                return []
            except OSError:
                raise CookieVaultError(
                    "无法读取已保存的 Cookie；原文件保持不变，请检查文件权限后重试。"
                ) from None
            try:
                return self._decode_vault(encrypted)
            except Exception:
                return self._quarantine_unreadable(profile_id, path)

    @staticmethod
    def _decode_vault(encrypted: bytes) -> list[BrowserCookie]:
        decoded = _dpapi(encrypted, protect=False)
        payload = json.loads(decoded.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("invalid cookie vault payload")
        if int(payload.get("version") or 0) != _VAULT_VERSION:
            raise ValueError("unsupported cookie vault version")
        return cookies_from_playwright(payload.get("cookies") or [])

    def _quarantine_unreadable(
        self,
        profile_id: str,
        path: Path,
    ) -> list[BrowserCookie]:
        backup = path.with_name(
            f"{path.name}.unreadable-{time.strftime('%Y%m%dT%H%M%S')}-"
            f"{uuid.uuid4().hex[:8]}.bak"
        )
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            os.replace(path, backup)
        except FileNotFoundError:
            return []
        except OSError:
            raise CookieVaultError(
                "已保存的 Cookie 无法解密，但无法安全备份原文件；"
                "为避免覆盖现有 Cookie，未启动空会话。"
            ) from None

        message = (
            "原有 Cookie 无法解密，可能已更换 Windows 用户或文件已损坏；"
            f"已启动空 Cookie 会话，原文件已备份为 {backup.name}，请重新登录。"
        )
        try:
            _atomic_write(
                self._recovery_notice_path(profile_id),
                message.encode("utf-8"),
            )
        except OSError:
            pass
        return []

    def save(self, profile_id: str, cookies: Iterable[BrowserCookie]) -> Path:
        values = deduplicate_cookies(cookies)
        payload = {
            "version": _VAULT_VERSION,
            "cookies": [asdict(cookie) for cookie in values],
        }
        encrypted = _dpapi(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            protect=True,
        )
        path = self.path_for(profile_id)
        with _vault_lock(path):
            _atomic_write(path, encrypted)
        return path

    def count(self, profile_id: str) -> int:
        return len(self.load(profile_id))

    def create_temporary_netscape_file(self, profile_id: str) -> Path:
        cookies = self.load(profile_id)
        if not cookies:
            raise RuntimeError("内置浏览器还没有保存 Cookie，请先打开登录页完成登录")
        temp_root = data_dir() / "temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        _cleanup_stale_cookie_exports_once(temp_root)
        handle, name = tempfile.mkstemp(prefix="huifa-cookie-", suffix=".txt", dir=temp_root, text=True)
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(netscape_cookie_text(cookies))
            return Path(name)
        except Exception:
            try:
                os.unlink(name)
            except OSError:
                pass
            raise
