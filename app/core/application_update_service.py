from __future__ import annotations

from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot

from app.core.application_updater import (
    ApplicationUpdate,
    AutoUpdateCheckThrottle,
    UpdateConfirmationRequired,
    UpdateDownloadCancelled,
    VelopackApplicationUpdater,
    VelopackUpdaterConfig,
)
from app.core.qt_lifecycle import delete_unstarted_worker
from app.core.update_receipt import (
    clear_update_install_intent,
    write_update_install_intent,
)


class ApplicationUpdateWorker(QObject):
    checked = Signal(object)
    no_update = Signal()
    progress = Signal(int)
    downloaded = Signal(object)
    pending_restart = Signal(object)
    no_pending_restart = Signal()
    cancelled = Signal()
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        updater: Any,
        action: str,
        update: ApplicationUpdate | None = None,
        throttle: AutoUpdateCheckThrottle | None = None,
        repository: str = "",
    ) -> None:
        super().__init__()
        self.updater = updater
        self.action = action
        self.update = update
        self.throttle = throttle
        self.repository = repository

    @Slot()
    def run(self) -> None:
        try:
            if self.action == "check":
                update = self.updater.check_for_updates()
                if self.throttle is not None and self.repository:
                    self.throttle.mark_checked(self.repository)
                if update is None:
                    self.no_update.emit()
                else:
                    self.checked.emit(update)
            elif self.action == "download" and self.update is not None:
                downloaded = self.updater.download_update(
                    self.update,
                    self.progress.emit,
                    lambda: QThread.currentThread().isInterruptionRequested(),
                )
                self.downloaded.emit(downloaded)
            elif self.action == "restore":
                pending = self.updater.pending_restart()
                if pending is None:
                    self.no_pending_restart.emit()
                else:
                    self.pending_restart.emit(pending)
            else:
                raise RuntimeError("未知的本程序更新操作")
        except UpdateDownloadCancelled:
            # Shutdown/cancellation intentionally keeps the verified partial
            # file for the next Range request and must not open an error dialog.
            self.cancelled.emit()
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


@dataclass(frozen=True, slots=True)
class _ApplicationUpdateRuntime:
    """Fully wired but not-yet-started update operation."""

    thread: QThread
    worker: ApplicationUpdateWorker


class ApplicationUpdateService(QObject):
    """Qt thread facade shared by Velopack and single-EXE update adapters."""

    update_available = Signal(object)
    no_update = Signal()
    progress = Signal(int)
    downloaded = Signal(object)
    pending_restart_available = Signal(object)
    no_pending_restart = Signal()
    download_cancelled = Signal()
    failed = Signal(str)
    busy_changed = Signal(bool)

    def __init__(
        self,
        state_dir: str | Path,
        updater_factory: Callable[[VelopackUpdaterConfig], Any] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.state_dir = Path(state_dir)
        self.throttle = AutoUpdateCheckThrottle(self.state_dir / "auto-check.json")
        self._updater_factory = updater_factory or VelopackApplicationUpdater
        self._updater: Any | None = None
        self._config: VelopackUpdaterConfig | None = None
        self._thread: QThread | None = None
        self._worker: ApplicationUpdateWorker | None = None
        self._deferred_thread_finishes: set[QThread] = set()
        self._clear_configuration_pending = False
        self.current_update: ApplicationUpdate | None = None
        self._shutting_down = False

    @property
    def busy(self) -> bool:
        # Runtime ownership continues through queued outcome delivery. A
        # stopped QThread is therefore still busy until its result and cleanup
        # callbacks have both run on this service's thread.
        return self._thread is not None

    def configure(
        self,
        repository: str,
        *,
        prerelease: bool = False,
        access_token: str | None = None,
        channel: str | None = None,
    ) -> None:
        config = VelopackUpdaterConfig(
            repository=repository,
            prerelease=bool(prerelease),
            access_token=access_token or None,
            channel=channel or None,
        )
        if config == self._config and self._updater is not None:
            return
        if self.busy:
            raise RuntimeError("本程序更新任务仍在运行，暂时无法切换更新配置")
        # Build first so a validation/SDK error cannot leave a new config
        # paired with the updater created for the previous repository.
        updater = self._updater_factory(config)
        self._config = config
        self._updater = updater
        self._clear_configuration_pending = False
        self.current_update = None

    def clear_configuration(self) -> bool:
        """Disable updates now, or immediately after the active worker exits."""
        if self.busy:
            self._clear_configuration_pending = True
            return False
        self._clear_configuration()
        return True

    def _clear_configuration(self) -> None:
        self._clear_configuration_pending = False
        self._config = None
        self._updater = None
        self.current_update = None

    def is_auto_check_due(self) -> bool:
        return bool(self._config and self.throttle.is_due(self._config.repository))

    def check(self, *, automatic: bool = False) -> bool:
        if self._shutting_down or self.busy or self._updater is None or self._config is None:
            return False
        if automatic and not self.is_auto_check_due():
            return False
        throttle = self.throttle if automatic else None
        return self._start_worker("check", throttle=throttle)

    def restore_pending_restart(self) -> bool:
        """Restore a locally downloaded Velopack update without checking the feed.

        Velopack persists a verified package outside the replaceable ``current``
        directory.  Reading that state on startup lets the UI offer installation
        again after the user previously chose "稍后", without performing another
        GitHub request or downloading the package a second time.
        """
        if self._shutting_down or self.busy or self._updater is None or self._config is None:
            return False
        return self._start_worker("restore")

    def download(self, update: ApplicationUpdate | None = None) -> bool:
        if self._shutting_down or self.busy or self._updater is None:
            return False
        selected = update or self.current_update
        if selected is None or selected.downloaded:
            return False
        return self._start_worker("download", selected)

    def cancel_download(self) -> bool:
        """Cooperatively pause the active application-package transfer."""
        thread = self._thread
        worker = self._worker
        if (
            thread is None
            or worker is None
            or not thread.isRunning()
            or worker.action != "download"
        ):
            return False
        thread.requestInterruption()
        return True

    def schedule_install_and_restart(
        self,
        update: ApplicationUpdate | None = None,
        *,
        confirmed: bool = False,
    ) -> None:
        if not confirmed:
            raise UpdateConfirmationRequired("安装程序更新前必须由用户明确确认")
        if self.busy or self._updater is None:
            raise RuntimeError("本程序更新任务仍在运行")
        selected = update or self.current_update
        if selected is None or not selected.downloaded:
            raise RuntimeError("本程序更新尚未下载完成")
        write_update_install_intent(self.state_dir, selected)
        try:
            self._updater.schedule_install_on_exit(
                selected,
                confirmed=confirmed,
                restart=True,
            )
        except Exception:
            # A failed updater launch must not be mistaken for a completed
            # install when the user next starts the unchanged application.
            clear_update_install_intent(self.state_dir)
            raise

    def request_shutdown(self) -> None:
        """Ask the update thread to stop once its current SDK call returns."""
        if self._shutting_down:
            return
        self._shutting_down = True
        thread = self._thread
        if thread is not None and thread.isRunning():
            thread.requestInterruption()
            thread.quit()

    def shutdown(self, timeout_ms: int = 5000) -> bool:
        self.request_shutdown()
        thread = self._thread
        if thread is None:
            return True
        if thread.isRunning():
            if timeout_ms <= 0 or not thread.wait(int(timeout_ms)):
                return False
        # The worker outcome and QThread.finished callbacks are queued to this
        # object's thread. Do not report a clean shutdown until those callbacks
        # have updated state and released runtime ownership.
        return self._thread is None

    def _start_worker(
        self,
        action: str,
        update: ApplicationUpdate | None = None,
        *,
        throttle: AutoUpdateCheckThrottle | None = None,
    ) -> bool:
        if self._updater is None or self._config is None:
            raise RuntimeError("尚未配置本程序更新仓库")
        try:
            runtime = self._prepare_worker_runtime(action, update, throttle)
        except Exception as exc:
            self.failed.emit(f"无法准备本程序更新线程：{exc}")
            return False
        thread = runtime.thread
        worker = runtime.worker
        # Runtime ownership is published only after every signal connection is
        # valid. A wiring failure therefore cannot strand the service in busy.
        self._thread = thread
        self._worker = worker
        try:
            thread.start()
        except Exception as exc:
            self._deferred_thread_finishes.discard(thread)
            self._clear_thread_references(thread, emit_busy=False)
            delete_unstarted_worker(worker, thread)
            self.failed.emit(f"无法启动本程序更新线程：{exc}")
            return False
        self.busy_changed.emit(True)
        return True

    def _prepare_worker_runtime(
        self,
        action: str,
        update: ApplicationUpdate | None,
        throttle: AutoUpdateCheckThrottle | None,
    ) -> _ApplicationUpdateRuntime:
        assert self._updater is not None
        assert self._config is not None
        thread = QThread(self)
        worker = ApplicationUpdateWorker(
            self._updater,
            action,
            update,
            throttle,
            self._config.repository,
        )
        runtime = _ApplicationUpdateRuntime(thread, worker)
        try:
            worker.moveToThread(thread)
            self._connect_worker_runtime(runtime)
        except Exception:
            delete_unstarted_worker(worker, thread)
            raise
        return runtime

    def _connect_worker_runtime(self, runtime: _ApplicationUpdateRuntime) -> None:
        thread = runtime.thread
        worker = runtime.worker
        thread.started.connect(worker.run)
        worker.checked.connect(self._on_checked, Qt.QueuedConnection)
        worker.no_update.connect(self._on_no_update, Qt.QueuedConnection)
        worker.progress.connect(self.progress, Qt.QueuedConnection)
        worker.downloaded.connect(self._on_downloaded, Qt.QueuedConnection)
        worker.pending_restart.connect(self._on_pending_restart, Qt.QueuedConnection)
        worker.no_pending_restart.connect(self._on_no_pending_restart, Qt.QueuedConnection)
        worker.cancelled.connect(self.download_cancelled, Qt.QueuedConnection)
        worker.failed.connect(self.failed, Qt.QueuedConnection)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._thread_finished_from_signal, Qt.QueuedConnection)

    @Slot(object)
    def _on_checked(self, update: ApplicationUpdate) -> None:
        self.current_update = update
        self.update_available.emit(update)

    @Slot()
    def _on_no_update(self) -> None:
        self.current_update = None
        self.no_update.emit()

    @Slot(object)
    def _on_downloaded(self, update: ApplicationUpdate) -> None:
        # Keep the immutable model while marking it as the installable update.
        self.current_update = update if update.downloaded else replace(update, downloaded=True)
        self.downloaded.emit(self.current_update)

    @Slot(object)
    def _on_pending_restart(self, update: ApplicationUpdate) -> None:
        # An SDK adapter should already mark pending packages as downloaded,
        # but preserve the invariant at this boundary for alternate adapters
        # and future Velopack API changes.
        self.current_update = update if update.downloaded else replace(update, downloaded=True)
        self.pending_restart_available.emit(self.current_update)

    @Slot()
    def _on_no_pending_restart(self) -> None:
        self.current_update = None
        self.no_pending_restart.emit()

    @Slot()
    def _thread_finished_from_signal(self) -> None:
        sender = self.sender()
        if isinstance(sender, QThread):
            self._defer_thread_finished(sender)

    def _defer_thread_finished(self, thread: QThread) -> None:
        """Deliver worker outcome signals before releasing the operation."""
        if thread in self._deferred_thread_finishes:
            return
        self._deferred_thread_finishes.add(thread)
        QTimer.singleShot(0, partial(self._complete_deferred_thread_finish, thread))

    def _complete_deferred_thread_finish(self, thread: QThread) -> None:
        self._deferred_thread_finishes.discard(thread)
        self._clear_thread_references(thread)
        try:
            thread.deleteLater()
        except RuntimeError:
            pass

    def _clear_thread_references(self, thread: QThread, *, emit_busy: bool = True) -> None:
        # A delayed ``finished`` signal from an older operation must not clear
        # a newer worker that has since taken ownership.
        if self._thread is not thread:
            return
        self._thread = None
        self._worker = None
        if self._clear_configuration_pending:
            self._clear_configuration()
        if emit_busy:
            self.busy_changed.emit(False)
