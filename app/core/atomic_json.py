from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping


def write_json_atomic(
    path: str | Path,
    payload: Mapping[str, Any],
    *,
    ensure_ascii: bool = False,
) -> None:
    """Durably replace a JSON object without exposing a partial target file."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    encoded = (
        json.dumps(
            dict(payload),
            ensure_ascii=ensure_ascii,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    try:
        with temporary.open("wb") as stream:
            written = stream.write(encoded)
            if written != len(encoded):
                raise OSError("JSON 状态文件写入不完整")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
