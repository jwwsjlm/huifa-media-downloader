from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.atomic_json import write_json_atomic


INSTALL_INTENT_FILENAME = "install-intent.json"
INSTALL_RECEIPT_FILENAME = "install-receipt.json"


@dataclass(frozen=True, slots=True)
class UpdateInstallReceipt:
    """Durable result shown once after an application-update restart."""

    status: str
    from_version: str
    to_version: str
    current_version: str
    delivery_kind: str
    message: str
    finished_at: str

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

    def installed_version_matches(self, version: str) -> bool:
        expected = _normalized_version(self.to_version)
        actual = _normalized_version(version)
        reported = _normalized_version(self.current_version)
        return bool(expected and actual == expected and (not reported or reported == actual))


def write_update_install_intent(state_dir: str | Path, update: Any) -> Path:
    """Persist the exact update the user confirmed before launching an updater."""

    root = Path(state_dir)
    target = root / INSTALL_INTENT_FILENAME
    payload = {
        "schema_version": 1,
        "from_version": _safe_text(getattr(update, "current_version", ""), 128),
        "to_version": _safe_text(getattr(update, "version", ""), 128),
        "delivery_kind": _safe_text(getattr(update, "delivery_kind", "velopack"), 64)
        or "velopack",
        "scheduled_at": _utc_now(),
    }
    if not payload["to_version"]:
        raise ValueError("更新目标版本不能为空")
    write_json_atomic(target, payload)
    return target


def clear_update_install_intent(state_dir: str | Path) -> None:
    _unlink_quietly(Path(state_dir) / INSTALL_INTENT_FILENAME)


def record_update_install_result(
    state_dir: str | Path,
    *,
    status: str,
    current_version: str,
    message: str = "",
    delivery_kind: str = "",
) -> UpdateInstallReceipt:
    """Finalize a locally scheduled update into a one-shot startup receipt."""

    normalized_status = str(status or "").strip().casefold()
    if normalized_status not in {"succeeded", "failed"}:
        raise ValueError("更新安装结果状态无效")

    root = Path(state_dir)
    intent = _read_json(root / INSTALL_INTENT_FILENAME)
    from_version = _safe_text(intent.get("from_version"), 128)
    to_version = _safe_text(intent.get("to_version"), 128)
    effective_current = _safe_text(current_version, 128)
    if not to_version:
        to_version = effective_current
    effective_delivery = (
        _safe_text(delivery_kind, 64)
        or _safe_text(intent.get("delivery_kind"), 64)
        or "unknown"
    )
    receipt = UpdateInstallReceipt(
        status=normalized_status,
        from_version=from_version,
        to_version=to_version,
        current_version=effective_current,
        delivery_kind=effective_delivery,
        message=_safe_text(message, 2000),
        finished_at=_utc_now(),
    )
    write_json_atomic(root / INSTALL_RECEIPT_FILENAME, _receipt_payload(receipt))
    clear_update_install_intent(root)
    return receipt


def consume_update_install_receipt(state_dir: str | Path) -> UpdateInstallReceipt | None:
    """Read and remove the last updater result so it is presented only once."""

    root = Path(state_dir)
    path = root / INSTALL_RECEIPT_FILENAME
    payload = _read_json(path)
    if not payload:
        return None
    try:
        if int(payload.get("schema_version") or 0) != 1:
            raise ValueError("unsupported receipt schema")
        status = _safe_text(payload.get("status"), 32).casefold()
        if status not in {"succeeded", "failed"}:
            raise ValueError("invalid receipt status")
        receipt = UpdateInstallReceipt(
            status=status,
            from_version=_safe_text(payload.get("from_version"), 128),
            to_version=_safe_text(payload.get("to_version"), 128),
            current_version=_safe_text(payload.get("current_version"), 128),
            delivery_kind=_safe_text(payload.get("delivery_kind"), 64) or "unknown",
            message=_safe_text(payload.get("message"), 2000),
            finished_at=_safe_text(payload.get("finished_at"), 128),
        )
    except (TypeError, ValueError):
        _unlink_quietly(path)
        clear_update_install_intent(root)
        return None
    _unlink_quietly(path)
    clear_update_install_intent(root)
    return receipt


def _receipt_payload(receipt: UpdateInstallReceipt) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": receipt.status,
        "from_version": receipt.from_version,
        "to_version": receipt.to_version,
        "current_version": receipt.current_version,
        "delivery_kind": receipt.delivery_kind,
        "message": receipt.message,
        "finished_at": receipt.finished_at,
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (FileNotFoundError, OSError, UnicodeError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _safe_text(value: Any, limit: int) -> str:
    return str(value or "").replace("\x00", "").strip()[: max(0, int(limit))]


def _normalized_version(value: str) -> str:
    return str(value or "").strip().lstrip("vV").casefold()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
