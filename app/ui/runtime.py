from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Iterable

from PySide6.QtCore import QLocale, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from app.core.language_packs import DEFAULT_LOCALE, discover_language_packs, match_language_pack


CJK_FONT_CANDIDATES = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "SimHei",
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "PingFang SC",
)
LATIN_FONT_CANDIDATES = (
    "Segoe UI",
    "Arial",
    "DejaVu Sans",
    "Liberation Sans",
)


@dataclass(frozen=True)
class FontDiagnostics:
    family: str
    cjk_supported: bool
    latin_supported: bool
    locale: str

def _families() -> set[str]:
    try:
        return {str(item) for item in QFontDatabase.families()}
    except (RuntimeError, TypeError):
        return set()


def _first_available(families: Iterable[str], installed: set[str]) -> str:
    for family in families:
        if family in installed:
            return family
    return ""


def configure_font(app: QApplication, requested_locale: str = "auto") -> FontDiagnostics:
    """Configure a deterministic font and expose diagnostics to tests/UI.

    HUIFA_UI_LOCALE can force ``zh-CN`` or ``en-US`` for screenshot tests.
    Otherwise the configured language is matched against app-local language
    packs and the operating system UI languages. Missing packs fall back to
    Simplified Chinese.
    """
    installed = _families()
    cjk = _first_available(CJK_FONT_CANDIDATES, installed)
    latin = _first_available(LATIN_FONT_CANDIDATES, installed)
    forced = os.environ.get("HUIFA_UI_LOCALE", "").strip().casefold()
    if forced in {"en", "en-us", "english"}:
        locale = "en-US"
        pack = match_language_pack(locale, (), discover_language_packs())
        translations = dict(pack.translations) if pack is not None and pack.locale == locale else {}
    elif forced in {"zh", "zh-cn", "chinese"}:
        locale = "zh-CN"
        pack = match_language_pack(locale, (), discover_language_packs())
        translations = dict(pack.translations) if pack is not None else {}
    else:
        packs = discover_language_packs()
        try:
            system_languages = QLocale.system().uiLanguages()
        except (AttributeError, RuntimeError):
            system_languages = [QLocale.system().name()]
        pack = match_language_pack(requested_locale, system_languages, packs)
        locale = pack.locale if pack is not None else DEFAULT_LOCALE
        translations = dict(pack.translations) if pack is not None else {}
    family = cjk if locale == "zh-CN" else (latin or cjk)
    if family:
        app.setFont(QFont(family, 10))
    app.setProperty("huifa.ui_locale", locale)
    app.setProperty("huifa.ui_translations", translations)
    app.setProperty("huifa.font_family", family)
    app.setProperty("huifa.cjk_supported", bool(cjk))
    app.setProperty("huifa.latin_supported", bool(latin or cjk))
    return FontDiagnostics(family, bool(cjk), bool(latin or cjk), locale)


def ui_locale(app: QApplication | None = None) -> str:
    app = app or QApplication.instance()
    return str(app.property("huifa.ui_locale") or "zh-CN") if app else "zh-CN"


@lru_cache(maxsize=8)
def _language_pack_fallback(locale: str) -> dict[str, str]:
    """Load an exact app-local pack when UI is used before app bootstrap.

    Production startup installs the selected pack on QApplication. Tests and
    small utility dialogs may construct widgets directly, so their fallback
    must still come from JSON rather than a second language embedded in code.
    """
    normalized = str(locale or "").casefold()
    packs = discover_language_packs()
    pack = packs.get(normalized)
    return dict(pack.translations) if pack is not None else {}


def ui_text(
    key: str,
    app: QApplication | None = None,
    *,
    context: str = "",
) -> str:
    app = app or QApplication.instance()
    locale = ui_locale(app)
    translations = app.property("huifa.ui_translations") if app else None
    if isinstance(translations, dict):
        translated = translations.get(f"{context}::{key}") if context else None
        if not isinstance(translated, str) or not translated:
            translated = translations.get(key)
        if isinstance(translated, str) and translated:
            return translated
    fallback = _language_pack_fallback(locale)
    translated = fallback.get(f"{context}::{key}") if context else None
    if not isinstance(translated, str) or not translated:
        translated = fallback.get(key)
    if isinstance(translated, str) and translated:
        return translated
    # Translation keys are readable English source strings. If a language
    # pack is missing or incomplete, showing the key is safer than embedding
    # another language in application source or returning an empty control.
    return str(key)


def configure_high_dpi() -> None:
    """Keep fractional Windows scaling precise before QApplication exists."""
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
