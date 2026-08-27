from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QAction, QActionGroup, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import (
    QComboBox,
    QMenu,
    QPushButton,
    QStyle,
    QStyleOptionButton,
    QStyleOptionComboBox,
    QStylePainter,
    QWidget,
)

from app.core.download_options import AUDIO_TRACKS
from app.ui.download_control_presentation import (
    AUDIO_TRACK_LABELS,
    DOWNLOAD_QUALITY_VALUES,
    SUBTITLE_LANGUAGE_LABELS,
    download_quality_text,
)
from app.ui.i18n import text as ui_text


def _centered_compact_control_text_rect(
    rect,
    text_width: int,
    *,
    gutter: int,
    reserve: int,
    right_bias: int,
):
    """Visually center text while keeping it clear of the indicator."""

    available = rect.adjusted(gutter, 0, -reserve, 0)
    width = max(0, min(int(text_width), available.width()))
    centered_left = (
        rect.left()
        + max(0, (rect.width() - width) // 2)
        + max(0, int(right_bias))
    )
    latest_safe_left = max(available.left(), available.right() - width + 1)
    available.setLeft(
        max(available.left(), min(centered_left, latest_safe_left))
    )
    available.setWidth(width)
    return available


_COMPACT_ICON_SIZE = 14
_COMPACT_ICON_LEFT_INSET = 11
_COMPACT_TEXT_GUTTER = 4
_COMPACT_INDICATOR_RESERVE = 28


def _paint_compact_icon(painter, key: str, rect: QRectF, color) -> None:
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(QPen(color, 1.35, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    painter.setBrush(Qt.NoBrush)
    x = rect.left()
    y = rect.top()

    if key == "content":
        painter.drawRoundedRect(QRectF(x + 1, y + 2, 12, 10), 2, 2)
        painter.setBrush(color)
        painter.drawPolygon(QPolygonF([
            QPointF(x + 5.5, y + 4.7),
            QPointF(x + 5.5, y + 9.3),
            QPointF(x + 9.5, y + 7),
        ]))
    elif key == "quality":
        painter.drawLine(QPointF(x + 7, y + 1), QPointF(x + 7, y + 13))
        painter.drawLine(QPointF(x + 1, y + 7), QPointF(x + 13, y + 7))
        painter.drawLine(QPointF(x + 3, y + 3), QPointF(x + 11, y + 11))
        painter.drawLine(QPointF(x + 11, y + 3), QPointF(x + 3, y + 11))
        painter.setBrush(color)
        painter.drawEllipse(QRectF(x + 5.5, y + 5.5, 3, 3))
    elif key == "format":
        painter.drawRoundedRect(QRectF(x + 2, y + 1, 10, 12), 1.5, 1.5)
        painter.drawLine(QPointF(x + 8, y + 1), QPointF(x + 12, y + 5))
        painter.drawLine(QPointF(x + 8, y + 1), QPointF(x + 8, y + 5))
        painter.drawLine(QPointF(x + 8, y + 5), QPointF(x + 12, y + 5))
        painter.drawLine(QPointF(x + 4, y + 8), QPointF(x + 10, y + 8))
        painter.drawLine(QPointF(x + 4, y + 10.5), QPointF(x + 9, y + 10.5))
    painter.restore()


def _draw_compact_control_content(
    painter,
    option,
    visible_text: str,
    icon_key: str,
) -> None:
    has_icon = bool(icon_key)
    available_width = max(
        0,
        option.rect.width()
        - _COMPACT_TEXT_GUTTER
        - _COMPACT_INDICATOR_RESERVE,
    )
    elided_text = option.fontMetrics.elidedText(
        visible_text,
        Qt.ElideRight,
        available_width,
    )
    text_width = option.fontMetrics.horizontalAdvance(elided_text)
    text_rect = _centered_compact_control_text_rect(
        option.rect,
        text_width,
        gutter=_COMPACT_TEXT_GUTTER,
        reserve=_COMPACT_INDICATOR_RESERVE,
        right_bias=0,
    )
    color = option.palette.buttonText().color()
    if has_icon:
        icon_top = option.rect.center().y() - (_COMPACT_ICON_SIZE / 2)
        _paint_compact_icon(
            painter,
            icon_key,
            QRectF(
                option.rect.left() + _COMPACT_ICON_LEFT_INSET,
                icon_top,
                _COMPACT_ICON_SIZE,
                _COMPACT_ICON_SIZE,
            ),
            color,
        )
    painter.setPen(color)
    painter.drawText(
        text_rect,
        Qt.AlignLeft | Qt.AlignVCenter | Qt.TextSingleLine,
        elided_text,
    )


class InsetMenuButton(QPushButton):
    """Keep the label centered and paint an icon in the left inset."""

    def setCompactIcon(self, key: str) -> None:
        self._compact_icon_key = str(key or "")
        self.update()

    def compactIconKey(self) -> str:
        return str(getattr(self, "_compact_icon_key", ""))

    def paintEvent(self, _event) -> None:
        option = QStyleOptionButton()
        self.initStyleOption(option)
        visible_text = option.text
        option.text = ""
        painter = QStylePainter(self)
        painter.drawControl(QStyle.CE_PushButton, option)
        _draw_compact_control_content(
            painter,
            option,
            visible_text,
            self.compactIconKey(),
        )


class InsetComboBox(QComboBox):
    """Keep selected text centered and paint an icon in the left inset."""

    def setCompactIcon(self, key: str) -> None:
        self._compact_icon_key = str(key or "")
        self.update()

    def compactIconKey(self) -> str:
        return str(getattr(self, "_compact_icon_key", ""))

    def paintEvent(self, _event) -> None:
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        visible_text = option.currentText
        option.currentText = ""
        painter = QStylePainter(self)
        painter.drawComplexControl(QStyle.CC_ComboBox, option)
        _draw_compact_control_content(
            painter,
            option,
            visible_text,
            self.compactIconKey(),
        )


@dataclass(slots=True)
class QuickContentSelector:
    button: InsetMenuButton
    content_mode: QComboBox
    subtitle_language: QComboBox
    audio_track: QComboBox
    content_actions: dict[str, QAction]
    subtitle_actions: dict[str, QAction]
    audio_track_actions: dict[str, QAction]


@dataclass(slots=True)
class QuickQualitySelector:
    button: InsetMenuButton
    quality: QComboBox
    video_fps: QComboBox
    source_codec: QComboBox
    vr_mode: QComboBox
    quality_actions: dict[str, QAction]
    fps_actions: dict[str, QAction]
    codec_actions: dict[str, QAction]
    vr_actions: dict[str, QAction]
    fps_menu: QMenu
    codec_menu: QMenu
    vr_menu: QMenu


def set_combo_value(combo: QComboBox, value: str) -> None:
    index = combo.findData(value)
    if index >= 0:
        combo.setCurrentIndex(index)


def _add_choice_action(
    menu: QMenu,
    group: QActionGroup,
    combo: QComboBox,
    label: str,
    value: str,
    actions: dict[str, QAction],
) -> None:
    action = menu.addAction(label)
    action.setCheckable(True)
    group.addAction(action)
    action.triggered.connect(
        lambda _checked=False, target=combo, selected=value:
        set_combo_value(target, selected)
    )
    actions[value] = action


def build_quick_content_selector(parent: QWidget) -> QuickContentSelector:
    content_mode = QComboBox(parent)
    content_mode.addItem(ui_text('Manual'), 'manual')
    content_mode.addItem(ui_text('Video'), 'video')
    content_mode.addItem(ui_text('Audio'), 'audio')
    content_mode.hide()
    content_mode.setToolTip(ui_text(
        'Changes here are saved immediately and stay synchronized with Download Settings. Manual asks you to choose video or audio after parsing the submitted link.',
    ))
    content_mode.setAccessibleName(ui_text('Download Content'))

    button = InsetMenuButton()
    button.setCompactIcon("content")
    button.setObjectName('downloadOptionMenuButton')
    button.setMinimumWidth(112)
    button.setMaximumWidth(150)
    button.setAccessibleName(ui_text('Download Content'))
    menu = QMenu(button)
    menu.setMinimumWidth(166)
    button.setMenu(menu)

    content_actions: dict[str, QAction] = {}
    content_group = QActionGroup(menu)
    content_group.setExclusive(True)
    for label, value in (
        (ui_text('Manual'), 'manual'),
        (ui_text('Video'), 'video'),
        (ui_text('Audio'), 'audio'),
    ):
        _add_choice_action(
            menu,
            content_group,
            content_mode,
            label,
            value,
            content_actions,
        )

    menu.addSeparator()
    subtitle_menu = menu.addMenu(ui_text('Subtitles'))
    subtitle_menu.setMinimumWidth(210)
    subtitle_group = QActionGroup(subtitle_menu)
    subtitle_group.setExclusive(True)
    subtitle_actions: dict[str, QAction] = {}
    subtitle_language = QComboBox(parent)
    for language_code, translation_key in SUBTITLE_LANGUAGE_LABELS.items():
        label = ui_text(translation_key)
        if language_code not in {'none', 'all'}:
            label = f'{label} ({language_code})'
        subtitle_language.addItem(label, language_code)
        _add_choice_action(
            subtitle_menu,
            subtitle_group,
            subtitle_language,
            label,
            language_code,
            subtitle_actions,
        )
    subtitle_language.hide()
    subtitle_language.setToolTip(ui_text(
        'Changes here are saved immediately. Uploaded subtitles are preferred, with automatic subtitles used as a fallback.',
    ))
    subtitle_language.setAccessibleName(ui_text('Subtitles'))

    audio_menu = menu.addMenu(ui_text('Audio Track'))
    audio_menu.setMinimumWidth(210)
    audio_group = QActionGroup(audio_menu)
    audio_group.setExclusive(True)
    audio_track_actions: dict[str, QAction] = {}
    audio_track = QComboBox(parent)
    for track in AUDIO_TRACKS:
        label = ui_text(AUDIO_TRACK_LABELS[track])
        if track not in {'default', 'original', 'all'}:
            label = f'{label} ({track})'
        audio_track.addItem(label, track)
        _add_choice_action(
            audio_menu,
            audio_group,
            audio_track,
            label,
            track,
            audio_track_actions,
        )
    audio_track.hide()
    audio_track.setToolTip(ui_text(
        'Changes here are saved immediately. A preferred language falls back to the default audio track when unavailable. All audio tracks enables yt-dlp multi-audio merging.',
    ))
    audio_track.setAccessibleName(ui_text('Audio Track'))

    return QuickContentSelector(
        button=button,
        content_mode=content_mode,
        subtitle_language=subtitle_language,
        audio_track=audio_track,
        content_actions=content_actions,
        subtitle_actions=subtitle_actions,
        audio_track_actions=audio_track_actions,
    )


def build_quick_quality_selector(parent: QWidget) -> QuickQualitySelector:
    quality = QComboBox(parent)
    for value in DOWNLOAD_QUALITY_VALUES:
        quality.addItem(download_quality_text(value), value)
    quality.hide()
    quality.setToolTip(ui_text(
        'Changes here are saved immediately. Manual selection asks for the exact format whenever an eligible video is downloaded.',
    ))
    quality.setAccessibleName(ui_text('Download Quality'))

    button = InsetMenuButton()
    button.setCompactIcon("quality")
    button.setObjectName('downloadOptionMenuButton')
    button.setMinimumWidth(118)
    button.setMaximumWidth(170)
    button.setAccessibleName(ui_text('Download Quality'))
    menu = QMenu(button)
    menu.setMinimumWidth(190)
    button.setMenu(menu)

    quality_actions: dict[str, QAction] = {}
    quality_group = QActionGroup(menu)
    quality_group.setExclusive(True)
    for value in DOWNLOAD_QUALITY_VALUES:
        _add_choice_action(
            menu,
            quality_group,
            quality,
            download_quality_text(value),
            value,
            quality_actions,
        )
    menu.addSeparator()

    fps_menu = menu.addMenu(ui_text('Frame Rate'))
    fps_menu.setMinimumWidth(150)
    video_fps = QComboBox(parent)
    fps_actions: dict[str, QAction] = {}
    fps_group = QActionGroup(fps_menu)
    fps_group.setExclusive(True)
    for value in ('best', '240', '120', '60', '50', '30', '25', '24'):
        label = ui_text('Highest') if value == 'best' else f'{value} FPS'
        video_fps.addItem(label, value)
        _add_choice_action(
            fps_menu,
            fps_group,
            video_fps,
            label,
            value,
            fps_actions,
        )
    video_fps.hide()

    codec_menu = menu.addMenu(ui_text('Video Codec'))
    codec_menu.setMinimumWidth(165)
    source_codec = QComboBox(parent)
    codec_actions: dict[str, QAction] = {}
    codec_group = QActionGroup(codec_menu)
    codec_group.setExclusive(True)
    for label, value in (
        (ui_text('Automatic'), 'auto'),
        ('H.264 / AVC', 'h264'),
        ('H.265 / HEVC', 'h265'),
        ('AV1', 'av1'),
        ('VP9', 'vp9'),
    ):
        source_codec.addItem(label, value)
        _add_choice_action(
            codec_menu,
            codec_group,
            source_codec,
            label,
            value,
            codec_actions,
        )
    source_codec.hide()

    vr_menu = menu.addMenu(ui_text('VR'))
    vr_menu.setMinimumWidth(150)
    vr_mode = QComboBox(parent)
    vr_actions: dict[str, QAction] = {}
    vr_group = QActionGroup(vr_menu)
    vr_group.setExclusive(True)
    for label, value in (
        (ui_text('Any'), 'any'),
        ('2D / 360°', '2d360'),
        ('3D / 180°', '3d180'),
        ('3D / 360°', '3d360'),
        (ui_text('No VR'), 'none'),
    ):
        vr_mode.addItem(label, value)
        _add_choice_action(
            vr_menu,
            vr_group,
            vr_mode,
            label,
            value,
            vr_actions,
        )
    vr_mode.hide()

    return QuickQualitySelector(
        button=button,
        quality=quality,
        video_fps=video_fps,
        source_codec=source_codec,
        vr_mode=vr_mode,
        quality_actions=quality_actions,
        fps_actions=fps_actions,
        codec_actions=codec_actions,
        vr_actions=vr_actions,
        fps_menu=fps_menu,
        codec_menu=codec_menu,
        vr_menu=vr_menu,
    )
