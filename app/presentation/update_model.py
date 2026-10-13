from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Property, QTimer, Signal, Slot

from app.core.application_updater import ApplicationUpdate
from app.core.transcode_service import clear_ffmpeg_encoder_cache
from app.core.update_receipt import consume_update_install_receipt
from app.core.update_service import select_release_asset
from app.core.version import APP_UPDATE_REPOSITORY, APP_VERSION


class UpdateModel(QObject):
    """Expose application and runtime-tool updates directly to QML."""

    changed = Signal()
    message = Signal(str)
    restartRequested = Signal()

    _COMPONENT_ORDER = ("yt-dlp", "yt-dlp-ejs", "Deno", "FFmpeg")

    def __init__(self, runtime: Any, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._application_status = "尚未检查程序更新"
        self._application_update: dict[str, Any] = {}
        self._application_progress = 0
        self._runtime_status = "尚未检查运行时组件"
        self._runtime_results: dict[str, dict[str, Any]] = {}
        self._runtime_checking = False
        self._installing_component = ""
        self._initialized = False
        self._shutting_down = False
        self._auto_check_timer = QTimer(self)
        self._auto_check_timer.setSingleShot(True)
        self._auto_check_timer.setInterval(8_000)
        self._auto_check_timer.timeout.connect(self._auto_check)
        self._connect_services()

    @Property(bool, notify=changed)
    def applicationSupported(self) -> bool:
        return bool(self._runtime.application_updates_supported)

    @Property(bool, notify=changed)
    def applicationBusy(self) -> bool:
        return bool(self._runtime.application_update_service.busy)

    @Property(str, notify=changed)
    def applicationStatus(self) -> str:
        return self._application_status

    @Property(int, notify=changed)
    def applicationProgress(self) -> int:
        return self._application_progress

    @Property("QVariantMap", notify=changed)
    def applicationUpdate(self) -> dict[str, Any]:
        return dict(self._application_update)

    @Property(bool, notify=changed)
    def runtimeBusy(self) -> bool:
        return self._runtime_checking or bool(self._installing_component)

    @Property(str, notify=changed)
    def runtimeStatus(self) -> str:
        return self._runtime_status

    @Property(str, notify=changed)
    def installingComponent(self) -> str:
        return self._installing_component

    @Property("QVariantList", notify=changed)
    def runtimeResults(self) -> list[dict[str, Any]]:
        ordered = []
        remaining = dict(self._runtime_results)
        for name in self._COMPONENT_ORDER:
            row = remaining.pop(name, None)
            if row is not None:
                ordered.append(self._present_runtime_row(row))
        ordered.extend(
            self._present_runtime_row(row)
            for _name, row in sorted(remaining.items())
        )
        return ordered

    @Slot()
    def initialize(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        if not self.applicationSupported:
            self._application_status = "当前为开发环境；正式发布版才支持程序自动更新"
            self.changed.emit()
            return
        if not self._configure_application_updates(silent=True):
            return
        receipt = consume_update_install_receipt(self._runtime.application_update_dir)
        if receipt is not None:
            if receipt.succeeded and receipt.installed_version_matches(APP_VERSION):
                self._application_status = f"已成功更新到 {APP_VERSION}"
            elif receipt.succeeded:
                self._application_status = "更新器报告成功，但当前版本仍需确认"
            else:
                self._application_status = "上次程序更新安装失败，可重新检查更新"
            self.changed.emit()
        QTimer.singleShot(0, self._restore_pending_update)
        if self._runtime.app_settings.get_bool("auto_check_updates", True):
            self._auto_check_timer.start()

    @Slot(result=bool)
    def checkApplication(self) -> bool:
        if self._shutting_down or not self._configure_application_updates():
            return False
        service = self._runtime.application_update_service
        if service.busy:
            self._application_status = "程序更新操作正在进行，请稍候"
            self.changed.emit()
            return False
        pending = service.current_update
        if pending is not None and pending.downloaded:
            self._set_application_update(pending)
            self._application_status = f"{pending.version} 已下载，重启后即可安装"
            self.changed.emit()
            return True
        self._application_progress = 0
        self._application_status = "正在检查程序更新…"
        self.changed.emit()
        started = service.check()
        if not started:
            self._application_status = "无法开始程序更新检查"
            self.changed.emit()
        return bool(started)

    @Slot(result=bool)
    def downloadApplication(self) -> bool:
        if self._shutting_down:
            return False
        service = self._runtime.application_update_service
        if not self._application_update or service.busy:
            return False
        self._application_progress = 0
        self._application_status = "正在下载程序更新…"
        self.changed.emit()
        started = service.download()
        if not started:
            self._application_status = "无法开始程序更新下载"
            self.changed.emit()
        return bool(started)

    @Slot(result=bool)
    def cancelApplicationDownload(self) -> bool:
        cancelled = bool(self._runtime.application_update_service.cancel_download())
        if cancelled:
            self._application_status = "已暂停程序更新下载"
            self.changed.emit()
        return cancelled

    @Slot(result=bool)
    def installApplication(self) -> bool:
        if self._shutting_down:
            return False
        service = self._runtime.application_update_service
        try:
            service.schedule_install_and_restart(confirmed=True)
        except Exception as exc:
            self._application_status = f"无法安排程序更新安装：{exc}"
            self.changed.emit()
            self.message.emit(self._application_status)
            return False
        self._application_status = "更新已就绪，正在安全退出并安装…"
        self.changed.emit()
        self.restartRequested.emit()
        return True

    @Slot(result=bool)
    def checkRuntimeComponents(self) -> bool:
        if self._shutting_down:
            return False
        service = self._runtime.update_service
        service.start_background_route_probe()
        if service.runtime_active("check"):
            self._runtime_status = "运行时组件检查正在进行…"
            self._runtime_checking = True
            self.changed.emit()
            return False
        self._runtime_results = {}
        self._runtime_checking = True
        self._runtime_status = "正在检查 yt-dlp、FFmpeg、Deno 等运行时组件…"
        self.changed.emit()
        started = service.check("")
        if not started:
            self._runtime_checking = False
            self._runtime_status = "无法开始运行时组件检查"
            self.changed.emit()
        return bool(started)

    @Slot(str, result=bool)
    def componentNeedsChecksumConfirmation(self, component: str) -> bool:
        asset = self._component_asset(component)
        return bool(asset) and not bool(
            re.fullmatch(r"sha256:[0-9a-fA-F]{64}", str(asset.get("digest") or ""))
        )

    @Slot(str, bool, result=bool)
    def installRuntimeComponent(
        self,
        component: str,
        allow_unverified: bool = False,
    ) -> bool:
        if self._shutting_down or self.runtimeBusy:
            return False
        name, result = self._runtime_result(component)
        asset = self._component_asset(component)
        if not name or not result or asset is None:
            self._runtime_status = "请先完成运行时组件检查"
            self.changed.emit()
            return False
        if not result.get("auto_install_supported") or not (
            result.get("install_available") or result.get("has_update")
        ):
            self._runtime_status = f"{name} 当前没有可安装的更新"
            self.changed.emit()
            return False
        digest = str(asset.get("digest") or "")
        if not allow_unverified and not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
            self._runtime_status = f"{name} 未提供发布方 SHA-256，请确认后再下载"
            self.changed.emit()
            return False
        self._installing_component = name
        self._runtime_status = f"正在下载并安装 {name}…"
        self.changed.emit()
        self._runtime.update_service.download_asset(asset, name)
        return True

    @Slot()
    def requestShutdown(self) -> None:
        self._shutting_down = True
        self._auto_check_timer.stop()

    def _connect_services(self) -> None:
        application = self._runtime.application_update_service
        application.update_available.connect(self._application_update_available)
        application.no_update.connect(self._application_no_update)
        application.downloaded.connect(self._application_downloaded)
        application.pending_restart_available.connect(self._application_downloaded)
        application.no_pending_restart.connect(self._application_no_pending_restart)
        application.progress.connect(self._application_progress_changed)
        application.download_cancelled.connect(self._application_download_cancelled)
        application.failed.connect(self._application_failed)
        application.busy_changed.connect(self._application_busy_changed)

        runtime = self._runtime.update_service
        runtime.result_ready.connect(self._runtime_result_ready)
        runtime.finished.connect(self._runtime_finished)
        runtime.failed.connect(self._runtime_failed)
        runtime.install_finished.connect(self._runtime_installed)
        runtime.download_failed.connect(self._runtime_install_failed)
        runtime.install_failed.connect(self._runtime_install_failed)

    @Slot(bool)
    def _application_busy_changed(self, _busy: bool) -> None:
        self.changed.emit()

    def _configure_application_updates(self, *, silent: bool = False) -> bool:
        if not self.applicationSupported:
            self._application_status = "当前为开发环境；正式发布版才支持程序自动更新"
            self.changed.emit()
            return False
        try:
            self._runtime.application_update_service.configure(
                APP_UPDATE_REPOSITORY,
                prerelease=self._runtime.app_settings.get_bool("update_prerelease", False),
                access_token=os.environ.get("GITHUB_TOKEN", "").strip() or None,
                channel=self._runtime.app_settings.get("update_channel") or None,
            )
        except Exception as exc:
            self._application_status = f"程序更新配置不可用：{exc}"
            self.changed.emit()
            if not silent:
                self.message.emit(self._application_status)
            return False
        return True

    @Slot()
    def _restore_pending_update(self) -> None:
        if self._shutting_down or not self._configure_application_updates(silent=True):
            return
        service = self._runtime.application_update_service
        if service.busy:
            return
        self._application_status = "正在恢复已下载的程序更新…"
        self.changed.emit()
        if not service.restore_pending_restart():
            self._application_status = f"当前版本：{APP_VERSION}"
            self.changed.emit()

    @Slot()
    def _auto_check(self) -> None:
        if self._shutting_down or not self._runtime.app_settings.get_bool("auto_check_updates", True):
            return
        if not self._configure_application_updates(silent=True):
            return
        service = self._runtime.application_update_service
        if service.busy or not service.is_auto_check_due():
            return
        self._application_status = "正在后台检查程序更新…"
        self.changed.emit()
        service.check(automatic=True)

    @Slot(object)
    def _application_update_available(self, update: object) -> None:
        if not isinstance(update, ApplicationUpdate):
            return
        self._set_application_update(update)
        self._application_status = f"发现新版本 {update.version}"
        self.changed.emit()

    @Slot()
    def _application_no_update(self) -> None:
        self._application_update = {}
        self._application_status = f"当前已是最新版本（{APP_VERSION}）"
        self.changed.emit()

    @Slot(object)
    def _application_downloaded(self, update: object) -> None:
        if isinstance(update, ApplicationUpdate):
            self._set_application_update(update)
            self._application_progress = 100
            self._application_status = f"{update.version} 已下载，重启后即可安装"
            self.changed.emit()

    @Slot()
    def _application_no_pending_restart(self) -> None:
        self._application_status = f"当前版本：{APP_VERSION}，没有等待安装的更新"
        self.changed.emit()

    @Slot(int)
    def _application_progress_changed(self, progress: int) -> None:
        self._application_progress = max(0, min(100, int(progress)))
        self.changed.emit()

    @Slot()
    def _application_download_cancelled(self) -> None:
        self._application_status = "程序更新下载已暂停，可稍后继续"
        self.changed.emit()

    @Slot(str)
    def _application_failed(self, error: str) -> None:
        self._application_status = str(error or "程序更新失败")
        self.changed.emit()
        self.message.emit(self._application_status)

    @Slot(object)
    def _runtime_result_ready(self, result: object) -> None:
        if not isinstance(result, dict):
            return
        name = str(result.get("name") or "").strip()
        if not name:
            return
        self._runtime_results[name] = dict(result)
        self.changed.emit()

    @Slot(object)
    def _runtime_finished(self, results: object) -> None:
        self._runtime_results = {
            str(row.get("name") or "").strip(): dict(row)
            for row in (results if isinstance(results, list) else [])
            if isinstance(row, dict) and str(row.get("name") or "").strip()
        }
        self._runtime_checking = False
        failures = sum(1 for row in self._runtime_results.values() if row.get("error"))
        self._runtime_status = (
            f"运行时组件检查完成：{len(self._runtime_results)} 项结果"
            + (f"，{failures} 项失败" if failures else "")
        )
        self.changed.emit()

    @Slot(str)
    def _runtime_failed(self, error: str) -> None:
        self._runtime_checking = False
        self._runtime_status = str(error or "运行时组件检查失败")
        self.changed.emit()
        self.message.emit(self._runtime_status)

    @Slot(object)
    def _runtime_installed(self, result: object) -> None:
        component = str(getattr(result, "component", "") or "")
        warning = ""
        try:
            self._apply_installed_paths(
                component,
                tuple(getattr(result, "paths", ()) or ()),
            )
        except Exception as exc:
            warning = f"{component} 已安装，但工具路径未能完全应用：{exc}"
        name, row = self._runtime_result(component)
        if name:
            row["has_update"] = False
            row["install_available"] = False
            if row.get("latest"):
                row["current"] = row["latest"]
        self._installing_component = ""
        self._runtime_status = warning or f"{component} 已安装到应用工具目录"
        self.changed.emit()
        self.message.emit(self._runtime_status)

    @Slot(str)
    def _runtime_install_failed(self, error: str) -> None:
        if not self._installing_component:
            return
        self._installing_component = ""
        self._runtime_status = str(error or "运行时组件安装失败")
        self.changed.emit()
        self.message.emit(self._runtime_status)

    def _set_application_update(self, update: ApplicationUpdate) -> None:
        self._application_update = {
            "version": update.version,
            "currentVersion": update.current_version,
            "sizeBytes": int(update.size_bytes),
            "releaseNotes": update.release_notes_markdown,
            "downloaded": bool(update.downloaded),
            "portable": bool(update.is_portable),
        }

    def _runtime_result(self, component: str) -> tuple[str, dict[str, Any]]:
        requested = str(component or "").strip()
        if requested.casefold() == "ffprobe":
            requested = "FFmpeg"
        for name, result in self._runtime_results.items():
            if name.casefold() == requested.casefold():
                return name, result
        return "", {}

    def _component_asset(self, component: str) -> dict[str, Any] | None:
        name, result = self._runtime_result(component)
        if not name:
            return None
        asset = select_release_asset(name, result.get("assets") or [])
        return dict(asset) if isinstance(asset, dict) else None

    def _present_runtime_row(self, result: dict[str, Any]) -> dict[str, Any]:
        row = dict(result)
        name = str(row.get("name") or "")
        asset = self._component_asset(name)
        row["actionable"] = bool(
            asset
            and row.get("auto_install_supported")
            and (row.get("install_available") or row.get("has_update"))
        )
        row["installing"] = self._installing_component.casefold() == name.casefold()
        row["checksumAvailable"] = bool(
            asset and re.fullmatch(r"sha256:[0-9a-fA-F]{64}", str(asset.get("digest") or ""))
        )
        return row

    def _apply_installed_paths(self, component: str, paths: tuple[str, ...]) -> None:
        installed = {Path(path).name.casefold(): str(path) for path in paths}
        values: dict[str, str] = {}
        if component.casefold() == "deno" and installed.get("deno.exe"):
            values["deno_path"] = installed["deno.exe"]
        elif component.casefold() == "ffmpeg":
            if installed.get("ffmpeg.exe"):
                values["ffmpeg_path"] = installed["ffmpeg.exe"]
            if installed.get("ffprobe.exe"):
                values["ffprobe_path"] = installed["ffprobe.exe"]
        if not values:
            return
        stored = self._runtime.app_settings.set_many(values)
        if stored.get("ffprobe_path"):
            clear_ffmpeg_encoder_cache()
        self._runtime.download_service.configure_runtime_settings(
            deno_path=stored.get("deno_path") if stored.get("deno_path") else None,
            ffprobe_path=(
                stored.get("ffprobe_path") if stored.get("ffprobe_path") else None
            ),
        )
        self._runtime.update_service.set_tool_overrides({
            "ffmpeg": self._runtime.app_settings.get("ffmpeg_path"),
            "ffprobe": self._runtime.app_settings.get("ffprobe_path"),
            "deno": self._runtime.app_settings.get("deno_path"),
        })
