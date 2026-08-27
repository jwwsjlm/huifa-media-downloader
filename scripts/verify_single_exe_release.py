from __future__ import annotations

import argparse
from pathlib import Path


class ReleaseLayoutError(RuntimeError):
    """The single-EXE delivery directory contains an invalid artifact set."""


def validate_single_exe_release(
    release_dir: str | Path,
    expected_executable: str = "HuifaVideoDownloader.exe",
) -> Path:
    """Require one non-empty Windows EXE as the only top-level delivery file.

    Runtime directories such as ``data`` are deliberately ignored. They can
    contain a developer's local database, settings or diagnostic ZIP and must
    never be deleted by a build. Only top-level files are release artifacts.
    """
    root = Path(release_dir)
    if not root.is_dir():
        raise ReleaseLayoutError(f"发行目录不存在：{root}")

    files = sorted(
        (path for path in root.iterdir() if path.is_file()),
        key=lambda path: path.name.casefold(),
    )
    names = [path.name for path in files]
    if names != [expected_executable]:
        found = "、".join(names) if names else "无"
        raise ReleaseLayoutError(
            f"单 EXE 发行目录顶层只能包含 {expected_executable}；当前文件：{found}"
        )

    executable = files[0]
    if executable.stat().st_size <= 0:
        raise ReleaseLayoutError(f"发行 EXE 为空：{executable}")
    try:
        with executable.open("rb") as stream:
            signature = stream.read(2)
    except OSError as exc:
        raise ReleaseLayoutError(f"无法读取发行 EXE：{executable}（{exc}）") from exc
    if signature != b"MZ":
        raise ReleaseLayoutError(f"发行文件不是有效的 Windows EXE：{executable}")
    return executable.resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验汇发单 EXE 交付目录")
    parser.add_argument("--release-dir", required=True)
    parser.add_argument("--expected-exe", default="HuifaVideoDownloader.exe")
    args = parser.parse_args(argv)
    executable = validate_single_exe_release(args.release_dir, args.expected_exe)
    print(f"single_exe_release={executable}")
    print(f"single_exe_size={executable.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
