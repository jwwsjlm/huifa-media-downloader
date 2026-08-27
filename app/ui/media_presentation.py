from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPointF, QSize, Qt
from PySide6.QtGui import (
    QImageReader,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPixmapCache,
    QPolygonF,
)

from app.ui.i18n import runtime_text, text as ui_text


def format_file_size(value: int) -> str:
    size = max(0.0, float(value or 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return "0 B"


PLATFORM_TEXT = {
    "douyin": "Douyin",
    "bilibili": "Bilibili",
    "tencent": "WeChat Channels",
    "kuaishou": "Kuaishou",
    "toutiao": "Toutiao",
    "xiaohongshu": "Xiaohongshu",
    "baijiahao": "Baijiahao",
    "alipay": "Alipay Life",
    "weibo": "Weibo",
    "hupu": "Hupu",
    "youtube": "YouTube",
    "tiktok": "TikTok",
}


PLATFORM_ICON_META = {
    "youtube": ("", "#ff0033"),
    # ASCII marks keep badges legible even on a clean Windows installation
    # where a CJK font may not be available to Qt's rasterizer.
    "douyin": ("DY", "#111111"),
    "bilibili": ("B", "#00a1d6"),
    "tencent": ("VX", "#07c160"),
    "kuaishou": ("KS", "#ff6a00"),
    "toutiao": ("TT", "#e1251b"),
    "xiaohongshu": ("XHS", "#ff2442"),
    "baijiahao": ("BJ", "#2676ff"),
    "alipay": ("AP", "#1677ff"),
    "weibo": ("WB", "#e6162d"),
    "hupu": ("HP", "#20242b"),
    "tiktok": ("TK", "#111111"),
    "generic": ("WEB", "#7d8796"),
}


def platform_label(platform: str) -> str:
    if platform in PLATFORM_TEXT:
        return ui_text(PLATFORM_TEXT[platform])
    return runtime_text(platform)


def platform_icon_pixmap(platform: str, size: int = 30) -> QPixmap:
    """Create a small dependency-free platform badge for task cards."""
    glyph, color = PLATFORM_ICON_META.get(platform, PLATFORM_ICON_META["generic"])
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    painter.setBrush(color)
    painter.drawRoundedRect(1, 1, size - 2, size - 2, 7, 7)
    painter.setPen(Qt.white)
    white_pen = QPen(Qt.white, max(1.5, size * 0.09), Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    painter.setPen(white_pen)
    painter.setBrush(Qt.NoBrush)
    if platform == "youtube":
        margin = size * 0.31
        triangle = QPolygonF([
            QPointF(size * 0.43, margin),
            QPointF(size * 0.43, size - margin),
            QPointF(size * 0.76, size / 2),
        ])
        painter.setBrush(Qt.white)
        painter.drawPolygon(triangle)
    elif platform in {"douyin", "tiktok"}:
        # Compact music-note mark shared by short-video platforms.
        x, y = size * 0.47, size * 0.28
        painter.drawLine(x, y, x, size * 0.67)
        painter.drawLine(x, y, size * 0.72, size * 0.34)
        painter.setBrush(Qt.white)
        painter.drawEllipse(QPointF(size * 0.34, size * 0.64), size * 0.14, size * 0.11)
    elif platform == "bilibili":
        painter.drawRoundedRect(size * 0.22, size * 0.30, size * 0.56, size * 0.42, size * 0.08, size * 0.08)
        painter.drawLine(size * 0.39, size * 0.30, size * 0.31, size * 0.17)
        painter.drawLine(size * 0.61, size * 0.30, size * 0.69, size * 0.17)
        painter.setBrush(Qt.white)
        painter.drawEllipse(QPointF(size * 0.40, size * 0.49), size * 0.035, size * 0.05)
        painter.drawEllipse(QPointF(size * 0.60, size * 0.49), size * 0.035, size * 0.05)
    elif platform == "tencent":
        painter.setBrush(Qt.white)
        painter.drawEllipse(QPointF(size * 0.50, size * 0.47), size * 0.24, size * 0.24)
        painter.drawPolygon(QPolygonF([
            QPointF(size * 0.37, size * 0.65), QPointF(size * 0.29, size * 0.79), QPointF(size * 0.52, size * 0.66)
        ]))
    elif platform == "kuaishou":
        painter.drawRoundedRect(size * 0.22, size * 0.29, size * 0.56, size * 0.42, size * 0.10, size * 0.10)
        painter.drawEllipse(QPointF(size * 0.50, size * 0.50), size * 0.13, size * 0.13)
        painter.drawLine(size * 0.30, size * 0.24, size * 0.43, size * 0.24)
    elif platform == "toutiao":
        painter.drawLine(size * 0.27, size * 0.32, size * 0.73, size * 0.32)
        painter.drawLine(size * 0.27, size * 0.50, size * 0.67, size * 0.50)
        painter.drawLine(size * 0.27, size * 0.68, size * 0.56, size * 0.68)
    elif platform == "xiaohongshu":
        path = QPainterPath()
        path.moveTo(size * 0.50, size * 0.76)
        path.cubicTo(size * 0.08, size * 0.52, size * 0.27, size * 0.20, size * 0.50, size * 0.38)
        path.cubicTo(size * 0.73, size * 0.20, size * 0.92, size * 0.52, size * 0.50, size * 0.76)
        painter.setBrush(Qt.white)
        painter.drawPath(path)
    elif platform == "baijiahao":
        painter.drawLine(size * 0.28, size * 0.31, size * 0.50, size * 0.40)
        painter.drawLine(size * 0.50, size * 0.40, size * 0.72, size * 0.31)
        painter.drawLine(size * 0.28, size * 0.31, size * 0.28, size * 0.70)
        painter.drawLine(size * 0.72, size * 0.31, size * 0.72, size * 0.70)
        painter.drawLine(size * 0.28, size * 0.70, size * 0.50, size * 0.61)
        painter.drawLine(size * 0.50, size * 0.61, size * 0.72, size * 0.70)
    elif platform == "alipay":
        painter.drawLine(size * 0.25, size * 0.61, size * 0.43, size * 0.41)
        painter.drawLine(size * 0.43, size * 0.41, size * 0.73, size * 0.62)
        painter.drawLine(size * 0.31, size * 0.70, size * 0.67, size * 0.70)
    elif platform == "weibo":
        painter.drawArc(size * 0.26, size * 0.30, size * 0.40, size * 0.40, 35 * 16, 210 * 16)
        painter.drawArc(size * 0.38, size * 0.42, size * 0.36, size * 0.30, 35 * 16, 210 * 16)
        painter.setBrush(Qt.white)
        painter.drawEllipse(QPointF(size * 0.50, size * 0.67), size * 0.06, size * 0.06)
    elif platform == "hupu":
        painter.setBrush(Qt.white)
        painter.drawPolygon(QPolygonF([
            QPointF(size * 0.50, size * 0.20), QPointF(size * 0.76, size * 0.31),
            QPointF(size * 0.70, size * 0.68), QPointF(size * 0.50, size * 0.80),
            QPointF(size * 0.30, size * 0.68), QPointF(size * 0.24, size * 0.31),
        ]))
    else:
        # Generic globe for unknown hosts.
        painter.drawEllipse(size * 0.25, size * 0.25, size * 0.50, size * 0.50)
        painter.drawArc(size * 0.37, size * 0.25, size * 0.26, size * 0.50, 0, 360 * 16)
        painter.drawLine(size * 0.25, size * 0.50, size * 0.75, size * 0.50)
    painter.end()
    pixmap.setDevicePixelRatio(1)
    return pixmap


def thumbnail_pixmap(
    path: str,
    width: int,
    height: int,
    aspect_mode=Qt.KeepAspectRatio,
) -> QPixmap:
    """Decode a card thumbnail near its display size and reuse it globally.

    ``QPixmap(path).scaled(...)`` first decodes the full source image. A large
    4K cover therefore blocks the GUI and allocates tens of megabytes even
    though a card displays only about 150×90 pixels. QImageReader asks the
    image plugin for a reduced decode, and QPixmapCache avoids repeating that
    work when the same media appears in the task and completed lists.
    """
    source = Path(str(path or ""))
    target = QSize(max(1, int(width)), max(1, int(height)))
    try:
        if not source.is_file():
            return QPixmap()
        stat = source.stat()
        resolved = source.resolve()
    except OSError:
        return QPixmap()
    cache_key = (
        f"huifa-thumbnail:{resolved}:{stat.st_mtime_ns}:{stat.st_size}:"
        f"{target.width()}x{target.height()}:{aspect_mode}"
    )
    cached = QPixmapCache.find(cache_key)
    if cached is not None and not cached.isNull():
        return cached

    reader = QImageReader(str(source))
    reader.setAutoTransform(True)
    source_size = reader.size()
    if source_size.isValid():
        decode_size = source_size.scaled(target, aspect_mode)
        if decode_size.isValid():
            reader.setScaledSize(decode_size)
    image = reader.read()
    if image.isNull():
        return QPixmap()
    pixmap = QPixmap.fromImage(image)
    if aspect_mode == Qt.KeepAspectRatioByExpanding:
        expanded = pixmap.scaled(target, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        left = max(0, (expanded.width() - target.width()) // 2)
        top = max(0, (expanded.height() - target.height()) // 2)
        pixmap = expanded.copy(left, top, target.width(), target.height())
    else:
        pixmap = pixmap.scaled(target, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    QPixmapCache.insert(cache_key, pixmap)
    return pixmap


def error_category_text(category: str) -> str:
    return str(category or "未知")


def compact_path_display(value: str) -> str:
    """Keep absolute runtime paths out of the normal settings presentation."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    normalized = raw.replace("/", "\\")
    parts = [part for part in normalized.split("\\") if part]
    tools_index = next(
        (index for index, part in enumerate(parts) if part.casefold() == "tools"),
        -1,
    )
    if tools_index >= 0:
        return "\\".join(parts[tools_index:])
    if len(parts) >= 2:
        return f"…\\{parts[-2]}\\{parts[-1]}"
    return parts[-1] if parts else raw
