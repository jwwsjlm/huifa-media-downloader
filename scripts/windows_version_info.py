from __future__ import annotations

from packaging.version import InvalidVersion, Version
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VSVersionInfo,
    VarFileInfo,
    VarStruct,
)


WINDOWS_EXECUTABLE_NAME = "HuifaVideoDownloader.exe"
WINDOWS_INTERNAL_NAME = "HuifaVideoDownloader"
SIMPLIFIED_CHINESE_UNICODE_TABLE = "080404B0"
SIMPLIFIED_CHINESE_LANGUAGE_ID = 2052
UNICODE_CODE_PAGE = 1200


def normalize_windows_version(value: str) -> tuple[tuple[int, int, int, int], str, bool]:
    """Map a PEP 440 version to Windows' four unsigned 16-bit fields."""
    raw = str(value or "").strip()
    if raw[:1].casefold() == "v":
        raw = raw[1:]
    try:
        parsed = Version(raw)
    except InvalidVersion as exc:
        raise ValueError(f"无效的应用版本号：{value}") from exc
    release = list(parsed.release)
    if len(release) > 4:
        raise ValueError("Windows 文件版本最多支持四段数字")
    release.extend([0] * (4 - len(release)))
    if any(part < 0 or part > 65535 for part in release):
        raise ValueError("Windows 文件版本的每一段必须位于 0~65535")
    normalized = tuple(int(part) for part in release)
    return normalized, raw, bool(parsed.is_prerelease or parsed.is_devrelease)


def build_windows_version_info(
    app_version: str,
    app_name: str,
    publisher: str,
    description: str,
    copyright_text: str,
) -> VSVersionInfo:
    """Create the VERSIONINFO resource for the packaged Windows executable."""
    version_tuple, product_version, is_prerelease = normalize_windows_version(app_version)
    file_version = ".".join(str(part) for part in version_tuple)
    return VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=version_tuple,
            prodvers=version_tuple,
            mask=0x3F,
            flags=0x2 if is_prerelease else 0,
            OS=0x40004,
            fileType=0x1,
            subtype=0x0,
            date=(0, 0),
        ),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        SIMPLIFIED_CHINESE_UNICODE_TABLE,
                        [
                            StringStruct("CompanyName", publisher),
                            StringStruct("FileDescription", description),
                            StringStruct("FileVersion", file_version),
                            StringStruct("InternalName", WINDOWS_INTERNAL_NAME),
                            StringStruct("LegalCopyright", copyright_text),
                            StringStruct("OriginalFilename", WINDOWS_EXECUTABLE_NAME),
                            StringStruct("ProductName", app_name),
                            StringStruct("ProductVersion", product_version),
                        ],
                    )
                ]
            ),
            VarFileInfo(
                [
                    VarStruct(
                        "Translation",
                        [SIMPLIFIED_CHINESE_LANGUAGE_ID, UNICODE_CODE_PAGE],
                    )
                ]
            ),
        ],
    )
