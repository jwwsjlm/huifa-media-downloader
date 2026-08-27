from __future__ import annotations

import argparse
from pathlib import Path
from urllib.parse import quote


def release_asset_url(repository: str, tag: str, filename: str) -> str:
    repository = repository.strip().strip("/")
    tag = tag.strip()
    if not repository or "/" not in repository:
        raise ValueError("repository must use owner/name format")
    if not tag:
        raise ValueError("tag is required")
    return (
        f"https://github.com/{repository}/releases/download/"
        f"{quote(tag, safe='')}/{quote(filename, safe='')}"
    )


def build_release_body(
    repository: str,
    tag: str,
    version: str,
    release_notes: str,
) -> str:
    version = version.strip()
    if not version:
        raise ValueError("version is required")
    notes = release_notes.strip()
    if not notes:
        raise ValueError("release notes are empty")

    portable_name = f"HuifaMediaDownloader-{version}-portable-win-x64.zip"
    installer_name = f"HuifaMediaDownloader-{version}-installer-win-x64.zip"
    portable_url = release_asset_url(repository, tag, portable_name)
    installer_url = release_asset_url(repository, tag, installer_name)

    download_section = f"""## 直接下载 / Direct downloads

> 普通用户只需选择下面一种版本。页面底部的其他资源用于自动更新和完整性校验，无需手动下载。  
> Most users only need one of the two downloads below. The remaining assets are for automatic updates and integrity checks.

| 版本 / Edition | 下载 / Download | 使用方式 / Usage |
| --- | --- | --- |
| 便携版 / Portable | **[下载便携版 ZIP / Download portable ZIP]({portable_url})** | 完整解压后运行根目录的 `Huifa Media Downloader.exe`；请勿只复制 EXE / Extract the complete folder and run the root EXE |
| 安装版 / Installer | **[下载安装包 ZIP / Download installer ZIP]({installer_url})** | 解压后运行 `HuifaMediaDownloader-Setup.exe` / Extract and run Setup |

---
"""
    return download_section + "\n" + notes + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a GitHub Release body with prominent edition links."
    )
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--notes", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    notes = args.notes.read_text(encoding="utf-8")
    body = build_release_body(
        args.repository,
        args.tag,
        args.version,
        notes,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(body, encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
