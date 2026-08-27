from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from conf import BASE_DIR


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


class UploadLogger:
    """Small compatibility wrapper for uploader logging calls."""

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def debug(self, message, *args, **kwargs) -> None:
        self._logger.debug(message, *args, **kwargs)

    def info(self, message, *args, **kwargs) -> None:
        self._logger.info(message, *args, **kwargs)

    def success(self, message, *args, **kwargs) -> None:
        self._logger.info(message, *args, **kwargs)

    def warning(self, message, *args, **kwargs) -> None:
        self._logger.warning(message, *args, **kwargs)

    def error(self, message, *args, **kwargs) -> None:
        self._logger.error(message, *args, **kwargs)


def create_logger(log_name: str, file_path: str) -> UploadLogger:
    logger = logging.getLogger(f"social_auto_upload.{log_name}")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if not logger.handlers:
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        console = logging.StreamHandler(sys.stdout)
        console.setLevel(logging.DEBUG)
        console.setFormatter(formatter)
        logger.addHandler(console)

        target = Path(BASE_DIR) / file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        rotating_file = RotatingFileHandler(
            target,
            maxBytes=10 * 1024 * 1024,
            backupCount=10,
            encoding="utf-8",
        )
        rotating_file.setLevel(logging.INFO)
        rotating_file.setFormatter(formatter)
        logger.addHandler(rotating_file)
    return UploadLogger(logger)


douyin_logger = create_logger("douyin", "logs/douyin.log")
tencent_logger = create_logger("tencent", "logs/tencent.log")
xhs_logger = create_logger("xhs", "logs/xhs.log")
tiktok_logger = create_logger("tiktok", "logs/tiktok.log")
bilibili_logger = create_logger("bilibili", "logs/bilibili.log")
kuaishou_logger = create_logger("kuaishou", "logs/kuaishou.log")
baijiahao_logger = create_logger("baijiahao", "logs/baijiahao.log")
xiaohongshu_logger = create_logger("xiaohongshu", "logs/xiaohongshu.log")
youtube_logger = create_logger("youtube", "logs/youtube.log")
alipay_logger = create_logger("alipay", "logs/alipay.log")
weibo_logger = create_logger("weibo", "logs/weibo.log")
hupu_logger = create_logger("hupu", "logs/hupu.log")
