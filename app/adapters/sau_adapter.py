from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from app.adapters.base_adapter import PlatformAdapter


COMMANDS = {"douyin": "douyin", "kuaishou": "kuaishou", "bilibili": "bilibili", "tencent": "tencent"}


class SauAdapter(PlatformAdapter):
    def __init__(self, platform: str, executable: str = "sau"):
        self.name = platform
        self.executable = executable

    def build_payload(self, media: dict[str, Any], metadata: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
        return {"media": media, "metadata": metadata, "settings": settings}

    def publish(self, payload: dict[str, Any]) -> tuple[bool, str]:
        media, metadata, settings = payload["media"], payload["metadata"], payload["settings"]
        video = media["video_path"]
        title = settings.get("title") or metadata.get("title") or Path(video).stem
        description = settings.get("description") or metadata.get("description", "")
        topics = settings.get("topics") or metadata.get("tags", [])
        args = [self.executable, COMMANDS[self.name], "upload-video", "--video", video, "--title", title]
        if description:
            args += ["--description", description]
        if topics:
            args += ["--tags", ",".join(topics) if isinstance(topics, list) else str(topics)]
        if media.get("thumbnail_path"):
            args += ["--cover", media["thumbnail_path"]]
        env = os.environ.copy()
        if settings.get("proxy"):
            env["HTTPS_PROXY"] = settings["proxy"]
            env["HTTP_PROXY"] = settings["proxy"]
        try:
            proc = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", env=env, timeout=3600)
            output = (proc.stdout or "") + (proc.stderr or "")
            return proc.returncode == 0, output[-8000:]
        except FileNotFoundError:
            return False, "未找到 sau 命令，请安装 social-auto-upload 并将 sau 加入 PATH"
        except Exception as exc:
            return False, str(exc)

