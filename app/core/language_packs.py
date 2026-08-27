from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from app.core.paths import application_dir, data_dir


LANGUAGE_PACK_SCHEMA_VERSION = 1
DEFAULT_LOCALE = "zh-CN"
_LOCALE_PATTERN = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


@dataclass(frozen=True)
class LanguagePack:
    locale: str
    name: str
    native_name: str
    authors: tuple[str, ...]
    translations: dict[str, str]
    path: Path


def normalize_locale(value: str | None) -> str:
    parts = str(value or "").strip().replace("_", "-").split("-")
    if not parts or not parts[0]:
        return ""
    normalized = [parts[0].lower()]
    for part in parts[1:]:
        normalized.append(part.upper() if len(part) in {2, 3} else part)
    result = "-".join(normalized)
    return result if _LOCALE_PATTERN.fullmatch(result) else ""


def language_pack_directory() -> Path:
    path = data_dir() / "languages"
    path.mkdir(parents=True, exist_ok=True)
    bundled_roots = [application_dir() / "languages"]
    extracted = getattr(sys, "_MEIPASS", "")
    if extracted:
        bundled_roots.append(Path(extracted) / "languages")
    for name in ("zh-CN.json", "en-US.json", "README.md"):
        destination = path / name
        source = next((root / name for root in bundled_roots if (root / name).is_file()), None)
        if source is None:
            continue
        # Bundled zh-CN/en-US files are the application's authoritative
        # interface packs. Refresh their writable copies on every application
        # update so an older bootstrap pack cannot shadow the complete one.
        # README remains user-editable once copied.
        if name not in {"zh-CN.json", "en-US.json"} and destination.exists():
            continue
        try:
            content = source.read_bytes()
            if destination.exists() and destination.read_bytes() == content:
                continue
        except OSError:
            continue
        temporary: Path | None = None
        try:
            temporary = destination.with_name(destination.name + ".tmp")
            temporary.write_bytes(content)
            temporary.replace(destination)
        except OSError:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return path


def language_pack_roots() -> tuple[Path, ...]:
    roots = [application_dir() / "languages"]
    extracted = getattr(sys, "_MEIPASS", "")
    if extracted:
        roots.append(Path(extracted) / "languages")
    roots.append(language_pack_directory())
    unique: list[Path] = []
    for root in roots:
        try:
            resolved = root.resolve()
        except OSError:
            resolved = root
        if resolved not in unique:
            unique.append(resolved)
    return tuple(unique)


def load_language_pack(path: str | Path) -> LanguagePack:
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != LANGUAGE_PACK_SCHEMA_VERSION:
        raise ValueError("不支持的语言包结构版本")
    locale = normalize_locale(payload.get("locale"))
    if not locale:
        raise ValueError("语言包 locale 无效")
    translations = payload.get("translations")
    if not isinstance(translations, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in translations.items()
    ):
        raise ValueError("语言包 translations 必须是字符串映射")
    authors_value = payload.get("authors") or []
    if isinstance(authors_value, str):
        authors_value = [authors_value]
    if not isinstance(authors_value, list) or any(not isinstance(item, str) for item in authors_value):
        raise ValueError("语言包 authors 必须是字符串数组")
    return LanguagePack(
        locale=locale,
        name=str(payload.get("name") or locale).strip() or locale,
        native_name=str(payload.get("native_name") or payload.get("name") or locale).strip() or locale,
        authors=tuple(item.strip() for item in authors_value if item.strip()),
        translations=dict(translations),
        path=source,
    )


def discover_language_packs(roots: Iterable[Path] | None = None) -> dict[str, LanguagePack]:
    packs: dict[str, LanguagePack] = {}
    for root in roots or language_pack_roots():
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.json")):
            try:
                pack = load_language_pack(path)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                continue
            # Later roots are writable/user-owned and intentionally override
            # the bundled pack with the same locale.
            packs[pack.locale.casefold()] = pack
    return packs


def match_language_pack(
    requested: str | None,
    system_languages: Iterable[str],
    packs: dict[str, LanguagePack],
) -> LanguagePack | None:
    candidates = list(system_languages) if str(requested or "").casefold() == "auto" else [str(requested or "")]
    normalized_packs = {key.casefold(): value for key, value in packs.items()}
    for candidate in candidates:
        locale = normalize_locale(candidate)
        if not locale:
            continue
        exact = normalized_packs.get(locale.casefold())
        if exact is not None:
            return exact
        language = locale.split("-", 1)[0].casefold()
        partial = next(
            (pack for key, pack in normalized_packs.items() if key.split("-", 1)[0] == language),
            None,
        )
        if partial is not None:
            return partial
    return normalized_packs.get(DEFAULT_LOCALE.casefold())
