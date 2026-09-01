from __future__ import annotations

import json
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field
from math import isfinite
from typing import Any


@dataclass(slots=True)
class TaskRowReader:
    """Typed, diagnostic reader for one persisted download-task row."""

    row: Any
    issues: list[str] = field(default_factory=list)

    def issue(self, key: str) -> None:
        if key not in self.issues:
            self.issues.append(key)

    def value(self, key: str, default: Any = "") -> Any:
        try:
            return self.row[key]
        except (IndexError, KeyError, TypeError):
            self.issue(key)
            return default

    def text(self, key: str, default: str = "") -> str:
        value = self.value(key, default)
        try:
            return str(value if value is not None else default)
        except Exception:
            self.issue(key)
            return default

    def integer(self, key: str, default: int = 0, *, minimum: int = 0) -> int:
        value = self.value(key, default)
        try:
            parsed = int(value or 0)
        except (TypeError, ValueError, OverflowError):
            self.issue(key)
            return default
        if parsed < minimum:
            self.issue(key)
            return default
        return parsed

    def floating(
        self,
        key: str,
        default: float = 0.0,
        *,
        minimum: float = 0.0,
        maximum: float | None = None,
    ) -> float:
        value = self.value(key, default)
        try:
            parsed = float(value or 0.0)
        except (TypeError, ValueError, OverflowError):
            self.issue(key)
            return default
        if not isfinite(parsed) or parsed < minimum or (
            maximum is not None and parsed > maximum
        ):
            self.issue(key)
            return default
        return parsed

    def boolean(self, key: str, default: bool = False) -> bool:
        return bool(self.integer(key, int(default), minimum=0))

    def options(self) -> dict[str, Any]:
        raw = self.value("options_json", "{}")
        if not raw or raw == "{}":
            return {}
        try:
            document = json.loads(str(raw or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            self.issue("options_json")
            return {}
        if not isinstance(document, Mapping):
            self.issue("options_json")
            return {}
        options = dict(document)
        categories = options.get("sponsorblock_categories")
        if categories is not None and not isinstance(categories, list):
            self.issue("options_json")
            return {}
        return options


@dataclass(frozen=True, slots=True)
class RestoredTaskHierarchy:
    parent_task_id: str
    root_task_id: str
    invalid_reason: str = ""


@dataclass(slots=True)
class TaskRestorePlan:
    immediate_rows: list[Any] = field(default_factory=list)
    deferred_rows: deque[Any] = field(default_factory=deque)
    deferred_parent_ids: set[str] = field(default_factory=set)
    hierarchy: dict[str, RestoredTaskHierarchy] = field(default_factory=dict)
    missing_media_urls: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _RestoreRowSummary:
    row: Any
    task_id: str
    task_kind: str
    parent_task_id: str
    status: str
    media_path: str
    source_url: str


def _summarize_rows(
    rows: list[Any],
) -> tuple[list[_RestoreRowSummary], dict[str, tuple[str, str]]]:
    summaries: list[_RestoreRowSummary] = []
    records: dict[str, tuple[str, str]] = {}
    for row in rows:
        reader = TaskRowReader(row)
        summary = _RestoreRowSummary(
            row=row,
            task_id=reader.text("id"),
            task_kind=reader.text("task_kind", "video") or "video",
            parent_task_id=reader.text("parent_task_id"),
            status=reader.text("status", "queued") or "queued",
            media_path=reader.text("media_path"),
            source_url=reader.text("url"),
        )
        summaries.append(summary)
        records[summary.task_id] = (
            summary.task_kind,
            summary.parent_task_id,
        )
    return summaries, records


def _invalid_hierarchy(
    task_id: str,
    task_kind: str,
    reason: str,
) -> RestoredTaskHierarchy:
    return RestoredTaskHierarchy(
        parent_task_id="",
        root_task_id=task_id if task_kind == "collection" else "",
        invalid_reason=reason,
    )


def _resolve_hierarchy_records(
    records: Mapping[str, tuple[str, str]],
) -> dict[str, RestoredTaskHierarchy]:
    """Resolve collection roots in linear time using iterative path compression."""

    resolved: dict[str, RestoredTaskHierarchy] = {}
    for start_id in records:
        if start_id in resolved:
            continue
        path: list[str] = []
        path_ids: set[str] = set()
        current = start_id
        root_id = ""
        invalid_reason = ""

        while True:
            cached = resolved.get(current)
            if cached is not None:
                if cached.invalid_reason:
                    invalid_reason = cached.invalid_reason
                else:
                    root_id = cached.root_task_id
                break
            if current in path_ids:
                invalid_reason = "任务层级存在循环引用"
                break

            record = records.get(current)
            if record is None:
                invalid_reason = "父合集记录不存在"
                break
            task_kind, parent_id = record
            path.append(current)
            path_ids.add(current)

            if not parent_id:
                root_id = current if task_kind == "collection" else ""
                break
            parent_record = records.get(parent_id)
            if parent_record is None:
                invalid_reason = "父合集记录不存在"
                break
            if parent_record[0] != "collection":
                invalid_reason = "父任务不是合集"
                break
            current = parent_id

        if invalid_reason:
            for task_id in path:
                task_kind, _parent_id = records[task_id]
                resolved[task_id] = _invalid_hierarchy(
                    task_id,
                    task_kind,
                    invalid_reason,
                )
            continue

        for task_id in reversed(path):
            _task_kind, parent_id = records[task_id]
            resolved[task_id] = RestoredTaskHierarchy(
                parent_task_id=parent_id,
                root_task_id=root_id,
            )
    return resolved


def build_task_restore_plan(
    rows: list[Any],
    *,
    initial_terminal_children: int,
) -> TaskRestorePlan:
    summaries, records = _summarize_rows(rows)
    plan = TaskRestorePlan()
    plan.hierarchy = _resolve_hierarchy_records(records)
    terminal_child_budget = max(0, int(initial_terminal_children))
    terminal_statuses = frozenset({
        "completed", "deleted", "failed", "canceled", "partial_failed",
    })
    seen_media_urls: set[str] = set()

    for summary in summaries:
        hierarchy = plan.hierarchy.get(
            summary.task_id,
            RestoredTaskHierarchy(
                "",
                summary.task_id if summary.task_kind == "collection" else "",
            ),
        )
        parent_id = hierarchy.parent_task_id
        is_collection = summary.task_kind == "collection"
        is_terminal_child = bool(parent_id) and summary.status in terminal_statuses

        if (
            summary.task_kind == "video"
            and summary.status in {"completed", "deleted"}
            and not summary.media_path
            and summary.source_url
            and summary.source_url not in seen_media_urls
        ):
            seen_media_urls.add(summary.source_url)
            plan.missing_media_urls.append(summary.source_url)

        restore_immediately = (
            bool(hierarchy.invalid_reason)
            or not parent_id
            or is_collection
            or not is_terminal_child
            or terminal_child_budget > 0
        )
        if restore_immediately:
            plan.immediate_rows.append(summary.row)
            if is_terminal_child:
                terminal_child_budget -= 1
        else:
            plan.deferred_rows.append(summary.row)
            plan.deferred_parent_ids.add(parent_id)
    return plan


def restored_status(
    reader: TaskRowReader,
    restorable_statuses: set[str] | frozenset[str],
) -> tuple[str, str]:
    original_status = reader.text("status", "queued").strip() or "queued"
    if original_status not in restorable_statuses:
        reader.issue("status")
        return original_status, "failed"
    status = original_status
    if status in {"downloading", "canceling", "暂停中", "waiting_selection"}:
        status = "paused"
    elif status == "processing":
        status = "completed"
    return original_status, status
