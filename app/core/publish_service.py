from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot

from app.adapters.sau_adapter import (
    UPLOAD_VIDEO_ACTION,
    account_action,
    build_upload_request,
    get_sau_platform_capability,
    probe_sau_compatibility,
    publish,
)
from app.core.media_identity import media_identity
from app.core.qt_lifecycle import delete_unstarted_worker
from app.integrations.social_auto_upload.runtime import open_download_cookie_browser
from app.storage.database import Database
from app.storage.models import MediaItem, PublishTask


class PublishWorker(QObject):
    result = Signal(int, bool, str)
    finished = Signal()

    def __init__(self, task_id: int, row, media: MediaItem):
        super().__init__()
        self.task_id = task_id
        self.row = row
        self.media = media
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @Slot()
    def run(self) -> None:
        try:
            metadata = {
                "title": self.row["title"],
                "description": self.row["description"],
                "tags": json.loads(self.row["topics"] or "[]"),
            }
            settings = json.loads(self.row["settings"] or "{}")
            platform = str(self.row["platform"] or "").strip().casefold()
            capability = get_sau_platform_capability(platform)
            if capability is None:
                self.result.emit(
                    self.task_id,
                    False,
                    f"平台 {platform or '未知'} 尚未接入视频发布",
                )
                return
            payload = {"media": self.media.__dict__, "metadata": metadata, "settings": settings}
            if self._cancel.is_set():
                self.result.emit(self.task_id, False, "发布任务已取消")
                return
            account = str(settings.get("account") or self.row["account"] or "default").strip() or "default"
            compatibility = probe_sau_compatibility(platform, UPLOAD_VIDEO_ACTION)
            if not compatibility.compatible:
                self.result.emit(self.task_id, False, compatibility.user_message())
                return
            # Publishing accounts use SAU's per-platform store. Validate them
            # immediately before upload so an expired login cannot start work.
            cookie_ok, cookie_result = account_action(
                platform,
                "check",
                account,
                cancel_event=self._cancel,
            )
            if self._cancel.is_set():
                self.result.emit(self.task_id, False, "发布任务已取消")
                return
            if not cookie_ok:
                detail = cookie_result.strip()
                message = (
                    f"发布账号 Cookie 无效、缺失或已过期：{platform} / {account}。"
                    "请在发布编辑页点击“登录”，完成登录后点击“检查”，再重试任务。"
                )
                if detail:
                    message += f"\n{detail}"
                self.result.emit(self.task_id, False, message)
                return
            ok, result = publish(platform, payload, cancel_event=self._cancel)
            self.result.emit(self.task_id, ok, result)
        except Exception as exc:
            self.result.emit(self.task_id, False, str(exc))
        finally:
            self.finished.emit()


class AccountWorker(QObject):
    result = Signal(str, str, str, bool, str)
    finished = Signal()

    def __init__(
        self,
        platform: str,
        account: str,
        action: str,
        *,
        vault_profile_id: str = "",
    ):
        super().__init__()
        self.platform = platform
        self.account = account
        self.action = action
        self.vault_profile_id = str(vault_profile_id or "")
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    @Slot()
    def run(self) -> None:
        try:
            if self.action == "login" and self.vault_profile_id:
                result = open_download_cookie_browser(
                    self.vault_profile_id,
                    cancel_event=self._cancel,
                )
                ok = bool(result.get("success"))
                output = str(result.get("message") or ("登录完成" if ok else "登录失败"))
                self.result.emit(self.platform, self.account, self.action, ok, output)
                return
            ok, output = account_action(
                self.platform, self.action, self.account, cancel_event=self._cancel
            )
            # Publishing-account login is followed by that platform's own
            # validation. The generic download-cookie browser returned above
            # intentionally has no platform-specific login check.
            if (
                self.action == "login"
                and ok
                and not self._cancel.is_set()
                and not self.vault_profile_id
            ):
                check_ok, check_output = account_action(
                    self.platform,
                    "check",
                    self.account,
                    cancel_event=self._cancel,
                )
                ok = check_ok
                if check_output.strip():
                    output = (output.rstrip() + "\nCookie 校验：" + check_output.strip()).strip()
            self.result.emit(self.platform, self.account, self.action, ok, output)
        except Exception as exc:
            self.result.emit(self.platform, self.account, self.action, False, str(exc))
        finally:
            self.finished.emit()


@dataclass(frozen=True, slots=True)
class _PublishRuntime:
    task_id: int
    thread: QThread
    worker: PublishWorker


@dataclass(frozen=True, slots=True)
class _AccountRuntime:
    key: str
    thread: QThread
    worker: AccountWorker


class PublishService(QObject):
    status = Signal(int, str, str)
    account_status = Signal(str, str, str, bool, str)

    def __init__(self, db: Database):
        super().__init__()
        self.db = db
        self.last_created_count = 0
        self.last_existing_count = 0
        self.threads: dict[int, QThread] = {}
        self.workers: dict[int, PublishWorker] = {}
        self.account_threads: dict[str, QThread] = {}
        self.account_workers: dict[str, AccountWorker] = {}
        self._deferred_publish_finishes: set[QThread] = set()
        self._deferred_account_finishes: set[QThread] = set()
        self._pending_publish_retries: set[int] = set()
        self._shutting_down = False
        # Runtime-only validation state. Raw cookies remain in the encrypted
        # browser vault and SAU's required account file; they are never copied
        # to SQLite, settings.ini, logs or signals.
        self.account_states: dict[str, dict[str, object]] = {}

    @property
    def active_thread_count(self) -> int:
        threads = set(self.threads.values()) | set(self.account_threads.values())
        return sum(1 for thread in threads if thread.isRunning())

    def request_shutdown(self) -> None:
        """Cooperatively cancel publishing and account workers."""
        if self._shutting_down:
            return
        self._shutting_down = True
        self._pending_publish_retries.clear()
        for worker in list(self.workers.values()) + list(self.account_workers.values()):
            try:
                worker.cancel()
            except RuntimeError:
                pass
        for thread in list(self.threads.values()) + list(self.account_threads.values()):
            if thread.isRunning():
                thread.requestInterruption()
                thread.quit()

    def shutdown(self, timeout_ms: int = 2500) -> bool:
        """Request shutdown, optionally wait, and report whether workers stopped."""
        self.request_shutdown()
        threads = list(set(self.threads.values()) | set(self.account_threads.values()))
        if timeout_ms > 0 and threads:
            deadline = time.monotonic() + timeout_ms / 1000.0
            for thread in threads:
                remaining = max(0, int((deadline - time.monotonic()) * 1000))
                if thread.isRunning() and remaining > 0:
                    thread.wait(remaining)
        # ``QThread.finished`` is queued back to this service's thread.  A
        # worker thread can already be stopped while its cleanup slot has not
        # yet removed dictionary references. Keep the database/UI alive until
        # those queued cleanup slots have completed.
        return not self.threads and not self.workers and not self.account_threads and not self.account_workers

    def recover_stale_tasks(self) -> int:
        """Make interrupted uploads retryable after an application restart."""
        return self.db.recover_interrupted_publish_tasks(
            "程序上次退出时发布未完成，可点击“重试失败任务”继续"
        )

    def create_tasks(self, media: MediaItem, platforms: list[str], metadata: dict, settings: dict) -> list[int]:
        ids: list[int] = []
        self.last_created_count = 0
        self.last_existing_count = 0
        identity = media_identity(
            media.source_url,
            metadata.get("title") or media.title,
            media.video_path,
        )
        for platform in platforms:
            platform_settings = settings.get(platform, {}) or {}
            account = str(platform_settings.get("account") or "default").strip() or "default"
            # Different accounts on the same platform are independent publish
            # targets; duplicate protection must not collapse them together.
            key = hashlib.sha256(
                f"{identity}:{platform}:{account}".encode("utf-8", errors="replace")
            ).hexdigest()
            task_id, created = self.db.get_or_add_publish_task(PublishTask(
                media_id=media.id or 0,
                platform=platform,
                account=account,
                title=metadata.get("title", media.title),
                description=metadata.get("description", media.description),
                topics=metadata.get("tags", media.tags),
                settings=platform_settings,
                idempotency_key=key,
            ))
            ids.append(task_id)
            if created:
                self.last_created_count += 1
            else:
                self.last_existing_count += 1
        return ids

    def run_task(self, task_id: int) -> None:
        if self._shutting_down:
            return
        if task_id in self.workers:
            return
        row = self.db.get_publish_task(task_id)
        if not row:
            return
        if row["status"] not in {"pending", "failed"}:
            return
        media = self.db.get_media(row["media_id"])
        if not media:
            message = "媒体记录不存在"
            self.db.update_publish_status(task_id, "failed", message)
            self.status.emit(task_id, "failed", message)
            return
        try:
            runtime = self._prepare_publish_runtime(task_id, row, media)
        except Exception as exc:
            self._report_publish_start_failure(task_id, exc)
            return

        try:
            self.db.update_publish_status(task_id, "uploading")
        except Exception as exc:
            delete_unstarted_worker(runtime.worker, runtime.thread)
            self.status.emit(task_id, "failed", f"无法更新发布任务状态：{exc}")
            return
        self.status.emit(task_id, "uploading", "")
        self.threads[task_id] = runtime.thread
        self.workers[task_id] = runtime.worker
        try:
            runtime.thread.start()
        except Exception as exc:
            self._clear_publish_runtime(runtime)
            delete_unstarted_worker(runtime.worker, runtime.thread)
            self._report_publish_start_failure(task_id, exc)

    def _prepare_publish_runtime(self, task_id: int, row, media: MediaItem) -> _PublishRuntime:
        thread = QThread()
        worker = PublishWorker(task_id, row, media)
        runtime = _PublishRuntime(task_id, thread, worker)
        thread.setProperty("publish_task_id", task_id)
        try:
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.result.connect(self._on_result, Qt.QueuedConnection)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            thread.finished.connect(
                self._publish_thread_finished_from_signal,
                Qt.QueuedConnection,
            )
        except Exception:
            delete_unstarted_worker(worker, thread)
            raise
        return runtime

    def _clear_publish_runtime(self, runtime: _PublishRuntime) -> None:
        if self.threads.get(runtime.task_id) is runtime.thread:
            self.threads.pop(runtime.task_id, None)
            self.workers.pop(runtime.task_id, None)
        self._deferred_publish_finishes.discard(runtime.thread)

    def _report_publish_start_failure(self, task_id: int, error: Exception) -> None:
        message = f"无法启动发布线程：{error}"
        try:
            self.db.update_publish_status(task_id, "failed", message)
        except Exception as persist_error:
            message += f"；保存失败状态时出错：{persist_error}"
        self.status.emit(task_id, "failed", message)

    @Slot(int, bool, str)
    def _on_result(self, task_id: int, ok: bool, result: str) -> None:
        state = "success" if ok else "failed"
        self.db.update_publish_status(task_id, state, result)
        self.status.emit(task_id, state, result)

    def _thread_finished(self, task_id: int) -> None:
        self.threads.pop(task_id, None)
        self.workers.pop(task_id, None)
        if task_id in self._pending_publish_retries:
            self._pending_publish_retries.discard(task_id)
            if not self._shutting_down:
                self.run_task(task_id)

    def _defer_publish_thread_finished(
        self,
        task_id: int,
        thread: QThread | None = None,
    ) -> None:
        """Let the worker's queued result land before dropping runtime ownership."""
        owned_thread = thread or self.threads.get(task_id)
        if not isinstance(owned_thread, QThread):
            return
        if owned_thread in self._deferred_publish_finishes:
            return
        self._deferred_publish_finishes.add(owned_thread)
        QTimer.singleShot(
            0,
            partial(
                self._complete_deferred_publish_thread_finish,
                task_id,
                owned_thread,
            ),
        )

    def _complete_deferred_publish_thread_finish(
        self,
        task_id: int,
        thread: QThread,
    ) -> None:
        self._deferred_publish_finishes.discard(thread)
        if self.threads.get(task_id) is thread:
            self._thread_finished(task_id)
        try:
            thread.deleteLater()
        except RuntimeError:
            pass

    @Slot()
    def _publish_thread_finished_from_signal(self) -> None:
        thread = self.sender()
        if not isinstance(thread, QThread):
            return
        task_id = int(thread.property("publish_task_id") or 0)
        if task_id > 0:
            self._defer_publish_thread_finished(task_id, thread)

    def retry_task(self, task_id: int) -> None:
        row = self.db.get_publish_task(task_id)
        if row and row["status"] == "failed":
            if task_id in self.workers or task_id in self.threads:
                self._pending_publish_retries.add(task_id)
                return
            self.run_task(task_id)

    def run_account_action(
        self,
        platform: str,
        account: str,
        action: str,
        *,
        vault_profile_id: str = "",
    ) -> bool:
        """Run a publishing-account action or the generic Cookie browser off the UI thread."""
        if self._shutting_down:
            return False
        key = f"{platform}:{account}"
        if key in self.account_workers:
            return False
        try:
            runtime = self._prepare_account_runtime(
                key,
                platform,
                account,
                action,
                vault_profile_id,
            )
        except Exception as exc:
            message = f"无法启动账号操作线程：{exc}"
            self._on_account_result(platform, account, action, False, message)
            return False
        self.account_threads[key] = runtime.thread
        self.account_workers[key] = runtime.worker
        try:
            runtime.thread.start()
        except Exception as exc:
            self._clear_account_runtime(runtime)
            delete_unstarted_worker(runtime.worker, runtime.thread)
            message = f"无法启动账号操作线程：{exc}"
            self._on_account_result(platform, account, action, False, message)
            return False
        return True

    def is_account_action_running(self, platform: str, account: str) -> bool:
        """Return whether an account action currently owns runtime resources."""
        key = f"{platform}:{account}"
        return key in self.account_workers or key in self.account_threads

    def _prepare_account_runtime(
        self,
        key: str,
        platform: str,
        account: str,
        action: str,
        vault_profile_id: str,
    ) -> _AccountRuntime:
        thread = QThread()
        worker = AccountWorker(
            platform,
            account,
            action,
            vault_profile_id=vault_profile_id,
        )
        runtime = _AccountRuntime(key, thread, worker)
        thread.setProperty("publish_account_key", key)
        try:
            worker.moveToThread(thread)
            thread.started.connect(worker.run)
            worker.result.connect(self._on_account_result, Qt.QueuedConnection)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            thread.finished.connect(
                self._account_thread_finished_from_signal,
                Qt.QueuedConnection,
            )
        except Exception:
            delete_unstarted_worker(worker, thread)
            raise
        return runtime

    def _clear_account_runtime(self, runtime: _AccountRuntime) -> None:
        if self.account_threads.get(runtime.key) is runtime.thread:
            self.account_threads.pop(runtime.key, None)
            self.account_workers.pop(runtime.key, None)
        self._deferred_account_finishes.discard(runtime.thread)

    def _account_thread_finished(self, key: str) -> None:
        self.account_threads.pop(key, None)
        self.account_workers.pop(key, None)

    def _defer_account_thread_finished(
        self,
        key: str,
        thread: QThread | None = None,
    ) -> None:
        """Keep account runtime alive through the queued result delivery turn."""
        owned_thread = thread or self.account_threads.get(key)
        if not isinstance(owned_thread, QThread):
            return
        if owned_thread in self._deferred_account_finishes:
            return
        self._deferred_account_finishes.add(owned_thread)
        QTimer.singleShot(
            0,
            partial(
                self._complete_deferred_account_thread_finish,
                key,
                owned_thread,
            ),
        )

    def _complete_deferred_account_thread_finish(
        self,
        key: str,
        thread: QThread,
    ) -> None:
        self._deferred_account_finishes.discard(thread)
        if self.account_threads.get(key) is thread:
            self._account_thread_finished(key)
        try:
            thread.deleteLater()
        except RuntimeError:
            pass

    @Slot()
    def _account_thread_finished_from_signal(self) -> None:
        thread = self.sender()
        if not isinstance(thread, QThread):
            return
        key = str(thread.property("publish_account_key") or "")
        if key:
            self._defer_account_thread_finished(key, thread)

    @Slot(str, str, str, bool, str)
    def _on_account_result(self, platform: str, account: str, action: str, ok: bool, result: str) -> None:
        key = f"{platform}:{account}"
        self.account_states[key] = {
            "ok": bool(ok),
            "action": action,
            "checked_at": time.time(),
            "result": result,
        }
        self.account_status.emit(platform, account, action, ok, result)

    def account_state(self, platform: str, account: str) -> dict[str, object] | None:
        return self.account_states.get(f"{platform}:{account}")
