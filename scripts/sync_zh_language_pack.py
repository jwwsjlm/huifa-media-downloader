from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ZH_OUTPUT = ROOT / "languages" / "zh-CN.json"
EN_OUTPUT = ROOT / "languages" / "en-US.json"
OUTPUT = ZH_OUTPUT
CALL_NAMES = {"ui_text", "ui_format", "text", "format_text"}
DYNAMIC_KEY_MAPS = {
    "STATUS_TEXT", "STAGE_TEXT", "PLATFORM_TEXT", "SUBTITLE_LANGUAGE_LABELS",
    "PUBLISH_STATUS_TEXT", "COVER_PRESET_LABEL_KEYS", "COVER_PRESET_HINTS",
    "_STANDARD_EDIT_ACTIONS",
    "THIRD_PARTY_ACKNOWLEDGEMENTS",
}
UI_CONSTRUCTORS = {
    "QAction", "QCheckBox", "QGroupBox", "QLabel", "QLineEdit", "QMenu",
    "QProgressDialog", "QPushButton", "QRadioButton", "QTreeWidgetItem",
}
UI_METHODS = {
    "addAction", "addButton", "addItem", "addItems", "addRow", "addTab", "critical",
    "getExistingDirectory", "getItem", "getMultiLineText", "getOpenFileName", "getSaveFileName", "getText",
    "information", "question", "setAccessibleDescription", "setAccessibleName",
    "insertItem", "insertTab", "setDetailedText", "setFormat", "setHeaderLabels",
    "setHorizontalHeaderLabels", "setInformativeText", "setItemText", "setLabelText",
    "setPlaceholderText", "setPrefix", "setStatusTip", "setSuffix", "setText",
    "setTabText", "setTitle", "setToolTip", "setWhatsThis", "setWindowTitle", "showMessage",
    "warning",
}
UI_TEXT_ARGUMENTS = {
    "QAction": (0, 1),
    "QCheckBox": (0,),
    "QGroupBox": (0,),
    "QLabel": (0,),
    "QMenu": (0,),
    "QProgressDialog": (0, 1),
    "QPushButton": (0,),
    "QRadioButton": (0,),
    "QTreeWidgetItem": (0,),
    "addAction": (0, 1),
    "addButton": (0,),
    "addItem": (0,),
    "addItems": (0,),
    "addRow": (0,),
    "addTab": (1, 2),
    "critical": (1, 2),
    "getExistingDirectory": (1,),
    "getItem": (1, 2),
    "getMultiLineText": (1, 2),
    "getOpenFileName": (1, 3),
    "getSaveFileName": (1, 3),
    "getText": (1, 2),
    "information": (1, 2),
    "insertItem": (1,),
    "insertTab": (1, 2),
    "question": (1, 2),
    "setAccessibleDescription": (0,),
    "setAccessibleName": (0,),
    "setDetailedText": (0,),
    "setFormat": (0,),
    "setHeaderLabels": (0,),
    "setHorizontalHeaderLabels": (0,),
    "setInformativeText": (0,),
    "setItemText": (1,),
    "setLabelText": (0,),
    "setPlaceholderText": (0,),
    "setPrefix": (0,),
    "setStatusTip": (0,),
    "setSuffix": (0,),
    "setText": (0,),
    "setTabText": (1,),
    "setTitle": (0,),
    "setToolTip": (0,),
    "setWhatsThis": (0,),
    "setWindowTitle": (0,),
    "showMessage": (0, 1),
    "warning": (1, 2),
}
NATIVE_UI_LITERALS = {
    # Protocol, browser and media-runtime names are intentionally shown in
    # their native spelling; translating them makes diagnostics harder to use.
    "Secure", "HttpOnly", "SameSite", "Chrome", "Edge", "Firefox", "Brave",
    "YouTube", "GitHub", "FFmpeg", "FFprobe", "Deno", "yt-dlp", "yt-dlp-ejs",
    "JPG", "1080p", "720p", "default",
}


def find_non_ui_translation_usage() -> list[str]:
    """Keep language-pack lookups out of services, adapters and storage.

    ``app/main.py`` is allowed because its translated strings are startup
    message-box controls. All other translation calls belong under ``app/ui``.
    """
    findings: list[str] = []
    allowed_main = ROOT / "app" / "main.py"
    ui_root = ROOT / "app" / "ui"
    for source in sorted((ROOT / "app").rglob("*.py")):
        if source == allowed_main or ui_root in source.parents:
            continue
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and str(node.module or "") in {
                "app.ui.i18n", "app.ui.runtime",
            }:
                imported = {alias.name for alias in node.names}
                if imported.intersection({"text", "format_text", "ui_text"}):
                    findings.append(
                        f"{source.relative_to(ROOT)}:{node.lineno}: 服务层不得导入界面翻译函数"
                    )
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in CALL_NAMES
            ):
                findings.append(
                    f"{source.relative_to(ROOT)}:{node.lineno}: 翻译调用只能用于界面控件"
                )
    return findings


def _constant_string(node: ast.AST) -> str | None:
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, str) else None


def _direct_strings(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.List, ast.Tuple)):
        return [
            item.value
            for item in node.elts
            if isinstance(item, ast.Constant) and isinstance(item.value, str)
        ]
    if isinstance(node, ast.JoinedStr):
        return [
            "".join(
                item.value
                for item in node.values
                if isinstance(item, ast.Constant) and isinstance(item.value, str)
            )
        ]
    return []


def _ui_sources() -> list[Path]:
    sources = sorted((ROOT / "app" / "ui").rglob("*.py"))
    main_source = ROOT / "app" / "main.py"
    if main_source.is_file():
        sources.append(main_source)
    return sources


def _call_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _visible_arguments(node: ast.Call, name: str) -> list[ast.AST]:
    positions = UI_TEXT_ARGUMENTS.get(name, ())
    return [node.args[index] for index in positions if index < len(node.args)]


def _looks_like_fixed_english_ui(value: str) -> bool:
    text = value.strip()
    if not text or text in NATIVE_UI_LITERALS:
        return False
    if not any(character.isalpha() and character.isascii() for character in text):
        return False
    # Pure HTML scaffolding and printf/format fragments carry runtime values,
    # not fixed UI wording. Human-readable text inside them still contains
    # words outside tags and is caught after the tags/placeholders are removed.
    without_markup = re.sub(r"<[^>]+>", " ", text)
    without_placeholders = re.sub(
        r"\{[^{}]+\}|%\([^)]+\)[#0+\- .0-9]*[a-zA-Z]",
        " ",
        without_markup,
    )
    letters = "".join(
        character.casefold()
        for character in without_placeholders
        if character.isalpha() and character.isascii()
    )
    return bool(letters and letters not in {"p", "fps"})


def find_bare_english_ui_strings() -> list[str]:
    """Find fixed English chrome that bypasses ``ui_text``/``ui_format``.

    English is the stable source-key language, but visible literals must still
    pass through the language pack so the Chinese locale can replace them.
    Dynamic service output, user values and native protocol/runtime names are
    intentionally outside this check.
    """
    findings: list[str] = []
    for source in _ui_sources():
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name not in UI_CONSTRUCTORS | UI_METHODS:
                continue
            for argument in _visible_arguments(node, name):
                if (
                    isinstance(argument, ast.Call)
                    and isinstance(argument.func, ast.Name)
                    and argument.func.id in CALL_NAMES | {"runtime_text"}
                ):
                    continue
                for value in _direct_strings(argument):
                    if _looks_like_fixed_english_ui(value):
                        preview = value.replace("\n", "\\n")[:160]
                        findings.append(
                            f"{source.relative_to(ROOT)}:{node.lineno}: {name}: {preview!r}"
                        )
    return findings


def find_bare_chinese_ui_strings() -> list[str]:
    """Find fixed Chinese control text that bypasses a language-pack key."""
    findings: list[str] = []
    for source in _ui_sources():
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            )
            if name not in UI_CONSTRUCTORS | UI_METHODS:
                continue
            for argument in node.args:
                for value in _direct_strings(argument):
                    if any("\u3400" <= character <= "\u9fff" for character in value):
                        preview = value.replace("\n", "\\n")[:160]
                        findings.append(
                            f"{source.relative_to(ROOT)}:{node.lineno}: {name}: {preview!r}"
                        )
    return findings


def find_legacy_bilingual_calls() -> list[str]:
    """Reject the old ``ui_text(chinese, english)`` source declaration style."""
    findings: list[str] = []
    for source in sorted((ROOT / "app").rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in CALL_NAMES:
                continue
            if len(node.args) > 1:
                findings.append(
                    f"{source.relative_to(ROOT)}:{node.lineno}: {node.func.id} 只能接收一个语言键"
                )
    return findings


def find_unstable_translation_calls() -> list[str]:
    """Translation keys must be stable literals, not runtime-specific strings."""
    findings: list[str] = []
    unstable_nodes = (ast.JoinedStr, ast.BinOp, ast.IfExp)
    for source in sorted((ROOT / "app").rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in CALL_NAMES or not node.args:
                continue
            if isinstance(node.args[0], unstable_nodes):
                findings.append(
                    f"{source.relative_to(ROOT)}:{node.lineno}: {node.func.id} 必须使用稳定语言键"
                )
    return findings


def collect_translation_keys() -> set[str]:
    keys: set[str] = set()
    for source in sorted((ROOT / "app").rglob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = {target.id for target in targets if isinstance(target, ast.Name)}
            selected = names.intersection(DYNAMIC_KEY_MAPS)
            if not selected and "PIPELINE_STAGES" not in names:
                continue
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                continue
            if selected and isinstance(value, dict):
                keys.update(item for item in value.values() if isinstance(item, str))
            elif "PIPELINE_STAGES" in names and isinstance(value, (tuple, list)):
                keys.update(
                    item[1]
                    for item in value
                    if isinstance(item, (tuple, list))
                    and len(item) >= 2
                    and isinstance(item[1], str)
                )
            if "THIRD_PARTY_ACKNOWLEDGEMENTS" in names and isinstance(value, (tuple, list)):
                keys.update(
                    item[1]
                    for item in value
                    if isinstance(item, (tuple, list))
                    and len(item) >= 2
                    and isinstance(item[1], str)
                )
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in CALL_NAMES or not node.args:
                continue
            key = _constant_string(node.args[0])
            if key is None:
                continue
            context = ""
            for keyword in node.keywords:
                if keyword.arg == "context":
                    context = _constant_string(keyword.value) or ""
                    break
            keys.add(f"{context}::{key}" if context else key)
    return keys


def _load_translations(path: Path) -> dict[str, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    translations = payload.get("translations") if isinstance(payload, dict) else None
    if payload.get("schema_version") != 1 or not isinstance(translations, dict):
        raise ValueError(f"{path.name} 不是受支持的语言包")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in translations.items()):
        raise ValueError(f"{path.name} translations 必须是字符串映射")
    return translations


def collect_translations() -> dict[str, str]:
    """Compatibility helper: return the authoritative Chinese language pack."""
    return _load_translations(ZH_OUTPUT)


def validate_language_packs() -> list[str]:
    keys = collect_translation_keys()
    zh = _load_translations(ZH_OUTPUT)
    en = _load_translations(EN_OUTPUT)
    errors: list[str] = []
    missing_zh = sorted(key for key in keys if not zh.get(key))
    invalid_en = sorted(
        key for key, value in en.items()
        if value != key.split("::", 1)[-1]
    )
    legacy = sorted(
        key for key in {*zh, *en}
        if key.startswith("legacy:")
    )
    if missing_zh:
        errors.append("zh-CN.json 缺少语言键：\n" + "\n".join(missing_zh))
    if invalid_en:
        errors.append("en-US.json 包含无效覆写：\n" + "\n".join(invalid_en))
    if legacy:
        errors.append("语言包包含已废弃 legacy 键：\n" + "\n".join(legacy))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="验证界面源码与 JSON 语言包")
    parser.add_argument("--check", action="store_true", help="兼容参数；当前始终只验证，不生成翻译")
    parser.parse_args()
    findings = [
        *find_bare_chinese_ui_strings(),
        *find_bare_english_ui_strings(),
        *find_legacy_bilingual_calls(),
        *find_unstable_translation_calls(),
        *find_non_ui_translation_usage(),
    ]
    if findings:
        print("界面源码未完全使用单语言键：")
        print("\n".join(findings))
        return 1
    try:
        errors = validate_language_packs()
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        print(str(exc))
        return 1
    if errors:
        print("\n\n".join(errors))
        return 1
    print(
        f"语言包校验通过：源码 {len(collect_translation_keys())} 个固定语言键；"
        f"zh-CN.json {len(_load_translations(ZH_OUTPUT))} 条；"
        f"en-US.json {len(_load_translations(EN_OUTPUT))} 条"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
