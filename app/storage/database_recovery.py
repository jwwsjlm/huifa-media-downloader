from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_BACKUP_COUNT = 3
DEFAULT_BACKUP_INTERVAL_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class DatabaseRecoveryReport:
    """Describe the database state selected before the application opens it."""

    status: str = "healthy"
    detail: str = ""
    restored_from: str = ""
    quarantine_dir: str = ""

    @property
    def requires_notice(self) -> bool:
        return self.status in {"restored", "reset", "schema_reset"}

    def as_dict(self) -> dict[str, str | bool]:
        payload: dict[str, str | bool] = asdict(self)
        payload["requires_notice"] = self.requires_notice
        return payload


def database_backup_paths(
    database_path: str | Path,
    *,
    count: int = DEFAULT_BACKUP_COUNT,
) -> tuple[Path, ...]:
    database = Path(database_path)
    backup_dir = database.parent / "backups"
    return tuple(
        backup_dir / f"{database.stem}.backup-{index}{database.suffix}"
        for index in range(1, max(1, int(count)) + 1)
    )


def database_integrity(database_path: str | Path) -> tuple[bool, str]:
    """Run SQLite's bounded quick check without mutating forensic sidecars."""

    database = Path(database_path)
    if not database.is_file():
        return False, "数据库文件不存在"

    # SQLite can create/rebuild SHM state even for a mode=ro connection when a
    # WAL is present and its directory is writable. Inspect any recovery
    # sidecars only on a private copy so startup validation never changes the
    # evidence that may need to be quarantined.
    if any(path.is_file() for path in _database_files(database)[1:]):
        return _database_integrity_copy(database)
    return _database_integrity_readonly(database)


def _quick_check(connection: sqlite3.Connection) -> tuple[bool, str]:
    connection.execute("PRAGMA query_only=ON")
    rows = connection.execute("PRAGMA quick_check(1)").fetchall()
    messages = [str(row[0] or "").strip() for row in rows]
    if messages and all(message.casefold() == "ok" for message in messages):
        return True, "ok"
    detail = "; ".join(message for message in messages if message)[:1000]
    return False, detail or "完整性检查未返回结果"


def _database_integrity_readonly(database: Path) -> tuple[bool, str]:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        return _quick_check(connection)
    except (OSError, sqlite3.DatabaseError) as exc:
        return False, str(exc)[:1000]
    finally:
        if connection is not None:
            connection.close()


def _database_integrity_copy(database: Path) -> tuple[bool, str]:
    try:
        with tempfile.TemporaryDirectory(prefix="huifa-db-check-") as directory:
            temporary_root = Path(directory)
            temporary_database = temporary_root / database.name
            for source in _database_files(database):
                if source.is_file():
                    shutil.copy2(source, temporary_root / source.name)
            connection: sqlite3.Connection | None = None
            try:
                connection = sqlite3.connect(temporary_database, timeout=5)
                return _quick_check(connection)
            except (OSError, sqlite3.DatabaseError) as exc:
                return False, str(exc)[:1000]
            finally:
                if connection is not None:
                    connection.close()
    except OSError as exc:
        return False, str(exc)[:1000]


def recover_database_file(
    database_path: str | Path,
    *,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> DatabaseRecoveryReport:
    """Quarantine a damaged database and restore the newest healthy snapshot.

    A deliberately removed database is treated as a fresh start and never
    resurrects an older backup. Recovery is attempted only when an existing
    file fails SQLite's own integrity check.
    """

    database = Path(database_path)
    if not database.exists():
        return DatabaseRecoveryReport(status="new", detail="数据库文件不存在，将创建新数据库")

    healthy, detail = database_integrity(database)
    if healthy:
        return DatabaseRecoveryReport(status="healthy", detail="SQLite quick_check 通过")

    quarantine = _quarantine_database(database)
    for backup in database_backup_paths(database, count=backup_count):
        backup_healthy, _backup_detail = database_integrity(backup)
        if not backup_healthy:
            continue
        temporary = database.with_name(f".{database.name}.restore-{uuid.uuid4().hex}.tmp")
        try:
            shutil.copy2(backup, temporary)
            restored_healthy, restored_detail = database_integrity(temporary)
            if not restored_healthy:
                continue
            os.replace(temporary, database)
            return DatabaseRecoveryReport(
                status="restored",
                detail=f"原数据库完整性检查失败：{detail}；备份复核：{restored_detail}",
                restored_from=str(backup),
                quarantine_dir=str(quarantine),
            )
        finally:
            temporary.unlink(missing_ok=True)

    return DatabaseRecoveryReport(
        status="reset",
        detail=f"原数据库完整性检查失败且没有可用备份：{detail}",
        quarantine_dir=str(quarantine),
    )


def database_backup_due(
    database_path: str | Path,
    *,
    interval_seconds: int = DEFAULT_BACKUP_INTERVAL_SECONDS,
    backup_count: int = DEFAULT_BACKUP_COUNT,
    now: float | None = None,
) -> bool:
    database = Path(database_path)
    if not database.is_file():
        return False
    newest = database_backup_paths(database, count=backup_count)[0]
    if not newest.is_file():
        return True
    try:
        source_mtime = max(
            candidate.stat().st_mtime
            for candidate in _database_files(database)
            if candidate.is_file()
        )
        backup_mtime = newest.stat().st_mtime
    except OSError:
        return True
    current_time = time.time() if now is None else float(now)
    # A future-dated snapshot means the system clock moved backwards after it
    # was created. Refresh it now; otherwise both the normal interval check and
    # the source-mtime shortcut can suppress backups until that future time.
    if backup_mtime > current_time:
        return True
    if source_mtime <= backup_mtime:
        return False
    return current_time - backup_mtime >= max(0, int(interval_seconds))


def create_rotating_database_backup(
    connection: sqlite3.Connection,
    database_path: str | Path,
    *,
    backup_count: int = DEFAULT_BACKUP_COUNT,
) -> Path:
    """Create a transactionally consistent SQLite backup and rotate snapshots."""

    database = Path(database_path)
    backups = database_backup_paths(database, count=backup_count)
    backup_dir = backups[0].parent
    backup_dir.mkdir(parents=True, exist_ok=True)
    temporary = backup_dir / f".{database.name}.backup-{uuid.uuid4().hex}.tmp"
    destination: sqlite3.Connection | None = None
    try:
        destination = sqlite3.connect(temporary)
        connection.backup(destination, pages=256, sleep=0.01)
        destination.close()
        destination = None
        healthy, detail = database_integrity(temporary)
        if not healthy:
            raise sqlite3.DatabaseError(f"新数据库备份未通过完整性检查：{detail}")

        backups[-1].unlink(missing_ok=True)
        for index in range(len(backups) - 2, -1, -1):
            source = backups[index]
            target = backups[index + 1]
            if source.is_file():
                os.replace(source, target)
        os.replace(temporary, backups[0])
        return backups[0]
    finally:
        if destination is not None:
            destination.close()
        # WAL mode can leave sidecars next to the temporary backup after the
        # main file has been moved. They are never part of a valid rotating
        # backup and otherwise accumulate on every clean shutdown.
        for temporary_file in _database_files(temporary):
            temporary_file.unlink(missing_ok=True)


def _database_files(database: Path) -> tuple[Path, ...]:
    return (
        database,
        database.with_name(database.name + "-wal"),
        database.with_name(database.name + "-journal"),
        database.with_name(database.name + "-shm"),
    )


def _quarantine_database(database: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    quarantine = database.parent / "recovery" / f"database-{timestamp}-{uuid.uuid4().hex[:8]}"
    quarantine.mkdir(parents=True, exist_ok=False)
    # Move the main file last. If the process is interrupted, the next launch
    # either sees the original main database and retries, or sees no database
    # and safely creates a new one. Roll back a normal move error immediately.
    order = _database_files(database)[1:] + (database,)
    moved: list[tuple[Path, Path]] = []
    try:
        for source in order:
            if not source.exists():
                continue
            target = quarantine / source.name
            os.replace(source, target)
            moved.append((source, target))
    except OSError:
        for source, target in reversed(moved):
            if target.exists() and not source.exists():
                os.replace(target, source)
        try:
            quarantine.rmdir()
        except OSError:
            pass
        raise
    return quarantine
