from __future__ import annotations

import json
import re
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.core.paths import data_dir


class DownloadLogService:
    """Append-only per-task diagnostic logs for download troubleshooting."""

    _lock = threading.RLock()

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root else data_dir() / "logs" / "downloads"
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, task_id: str) -> Path:
        safe_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(task_id))[:80] or "task"
        return self.root / f"{safe_id}.jsonl"

    def clear(self, task_id: str) -> None:
        try:
            self.path_for(task_id).unlink(missing_ok=True)
        except OSError:
            pass

    def write(self, task_id: str, level: str, category: str, message: str, **details: Any) -> None:
        event = {
            "time": datetime.now().isoformat(timespec="seconds"),
            "level": level,
            "category": category,
            "message": message,
        }
        if details:
            event["details"] = self._sanitize(details)
        line = json.dumps(event, ensure_ascii=False)
        with self._lock:
            try:
                with self.path_for(task_id).open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                # Logging must never break a download.
                return

    def read(self, task_id: str) -> list[dict[str, Any]]:
        path = self.path_for(task_id)
        if not path.exists():
            return []
        events: list[dict[str, Any]] = []
        with self._lock:
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        value = {"time": "", "level": "warning", "category": "日志", "message": line}
                    if isinstance(value, dict):
                        events.append(value)
            except OSError:
                return []
        return events

    def render(self, task_id: str) -> str:
        events = self.read(task_id)
        if not events:
            return "暂无日志。"
        lines: list[str] = []
        for event in events:
            details = event.get("details") or {}
            suffix = "  " + json.dumps(details, ensure_ascii=False) if details else ""
            lines.append(f"[{event.get('time', '')}] [{event.get('level', '')}] [{event.get('category', '')}] {event.get('message', '')}{suffix}")
        return "\n".join(lines)

    def export_bundle(self, destination: str | Path | None = None, summary: dict[str, Any] | None = None) -> Path:
        """Export logs and a redacted environment summary for support tickets."""
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = Path(destination) if destination else self.root.parent / f"diagnostics-{timestamp}.zip"
        target.parent.mkdir(parents=True, exist_ok=True)
        manifest = self._sanitize(summary or {})
        manifest["exported_at"] = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                for path in sorted(self.root.glob("*.jsonl")):
                    archive.write(path, arcname=f"downloads/{path.name}")
        return target

    @staticmethod
    def redact_url(url: str) -> str:
        try:
            parts = urlsplit(url)
            return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        except Exception:
            return "<无效地址>"

    @staticmethod
    def classify_error(error: str) -> str:
        text = (error or "").lower()
        risk_words = ("captcha", "challenge", "bot", "robot", "sign in", "login", "age-restricted", "verification", "429", "rate limit", "too many requests")
        network_words = ("timeout", "timed out", "connection", "dns", "proxy", "network", "http error 5", "temporary failure", "name or service not known", "connection reset")
        format_words = ("requested format", "format is not available", "ffmpeg", "merge")
        if any(word in text for word in risk_words):
            return "风控/登录"
        if any(word in text for word in network_words):
            return "网络/代理"
        if any(word in text for word in format_words):
            return "格式/工具"
        if "cancel" in text or "取消" in text:
            return "用户操作"
        return "未知"

    @staticmethod
    def _sanitize(value: Any) -> Any:
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, item in value.items():
                key_text = str(key).lower()
                if key_text in {"cookie", "cookiefile", "password", "token", "authorization"}:
                    continue
                if key_text in {"url", "source_url", "webpage_url"} and isinstance(item, str):
                    result[str(key)] = DownloadLogService.redact_url(item)
                else:
                    result[str(key)] = DownloadLogService._sanitize(item)
            return result
        if isinstance(value, (list, tuple)):
            return [DownloadLogService._sanitize(item) for item in value]
        if isinstance(value, str) and len(value) > 2000:
            return value[:2000] + "…"
        return value
