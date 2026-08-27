from __future__ import annotations

from urllib.parse import urlparse


_PLATFORM_DOMAINS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("youtube", ("youtube.com", "youtube-nocookie.com", "youtu.be")),
    ("douyin", ("douyin.com",)),
    ("bilibili", ("bilibili.com", "b23.tv")),
    (
        "tencent",
        (
            "v.qq.com",
            "channels.weixin.qq.com",
            "finder.video.qq.com",
        ),
    ),
    ("kuaishou", ("kuaishou.com",)),
    ("toutiao", ("toutiao.com", "ixigua.com")),
    ("xiaohongshu", ("xiaohongshu.com", "xhslink.com")),
    (
        "baijiahao",
        (
            "baijiahao.baidu.com",
            "mbd.baidu.com",
            "haokan.baidu.com",
        ),
    ),
    ("alipay", ("alipay.com",)),
    ("weibo", ("weibo.com", "weibo.cn")),
    ("hupu", ("hupu.com",)),
    ("tiktok", ("tiktok.com",)),
)


def detect_platform(url: str) -> str:
    """Map a media URL to a compact platform key without broad host guesses."""

    try:
        host = (urlparse(str(url or "")).hostname or "").casefold().rstrip(".")
    except ValueError:
        return "generic"
    if not host:
        return "generic"
    for platform, domains in _PLATFORM_DOMAINS:
        if any(host == domain or host.endswith("." + domain) for domain in domains):
            return platform
    return "generic"
