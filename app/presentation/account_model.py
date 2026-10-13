from __future__ import annotations

from typing import Any

from PySide6.QtCore import QByteArray, QAbstractListModel, QModelIndex, Property, Qt, Signal, Slot

from app.adapters.sau_adapter import SAU_SUPPORTED_PLATFORMS, get_sau_platform_capability
from app.presentation.distribution_plan import (
    distribution_target_platforms,
    serialize_distribution_target_platforms,
)
from app.presentation.media_text import platform_label


class AccountModel(QAbstractListModel):
    """QML account settings backed by the existing publishing service."""

    PlatformRole = Qt.ItemDataRole.UserRole + 1
    LabelRole = PlatformRole + 1
    AccountRole = PlatformRole + 2
    TargetRole = PlatformRole + 3
    StateTextRole = PlatformRole + 4
    StateToneRole = PlatformRole + 5
    BusyRole = PlatformRole + 6
    LoginHintRole = PlatformRole + 7

    countChanged = Signal()
    targetCountChanged = Signal()
    changed = Signal()
    saved = Signal()
    message = Signal(str)

    _ROLE_NAMES = {
        PlatformRole: b"platform",
        LabelRole: b"label",
        AccountRole: b"account",
        TargetRole: b"target",
        StateTextRole: b"stateText",
        StateToneRole: b"stateTone",
        BusyRole: b"busy",
        LoginHintRole: b"loginHint",
    }

    def __init__(self, settings: Any, publish_service: Any, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings
        self._publish_service = publish_service
        targets = set(distribution_target_platforms(settings.get("publish_target_platforms")))
        self._rows = [
            {
                "platform": platform,
                "label": platform_label(platform),
                "account": str(settings.get(f"publish_account/{platform}") or "default").strip() or "default",
                "target": platform in targets,
                "busy": False,
            }
            for platform in SAU_SUPPORTED_PLATFORMS
        ]
        publish_service.account_status.connect(self._account_status, Qt.QueuedConnection)

    def roleNames(self) -> dict[int, QByteArray]:  # type: ignore[override]
        return {role: QByteArray(name) for role, name in self._ROLE_NAMES.items()}

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # type: ignore[override]
        return 0 if parent.isValid() else len(self._rows)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:  # type: ignore[override]
        if not index.isValid() or not 0 <= index.row() < len(self._rows):
            return None
        row = self._rows[index.row()]
        platform = str(row["platform"])
        account = str(row["account"])
        state = self._publish_service.account_state(platform, account)
        state_text, tone = self._state_presentation(state)
        capability = get_sau_platform_capability(platform)
        if role == Qt.ItemDataRole.DisplayRole:
            return row["label"]
        if role == self.PlatformRole:
            return platform
        if role == self.LabelRole:
            return row["label"]
        if role == self.AccountRole:
            return account
        if role == self.TargetRole:
            return bool(row["target"])
        if role == self.StateTextRole:
            return state_text
        if role == self.StateToneRole:
            return tone
        if role == self.BusyRole:
            return bool(row["busy"])
        if role == self.LoginHintRole:
            return (
                "将在独立登录终端中完成登录"
                if capability is not None and capability.interactive_login
                else "将在应用内置浏览器中完成登录"
            )
        return None

    @Property(int, notify=countChanged)
    def count(self) -> int:
        return len(self._rows)

    @Property(int, notify=targetCountChanged)
    def targetCount(self) -> int:
        return sum(1 for row in self._rows if row["target"])

    @Slot(str, str)
    def setAccount(self, platform: str, account: str) -> None:
        index = self._index(platform)
        if index < 0:
            return
        value = str(account or "").strip() or "default"
        if value == self._rows[index]["account"]:
            return
        self._rows[index]["account"] = value
        model_index = self.index(index, 0)
        self.dataChanged.emit(model_index, model_index, list(self._ROLE_NAMES))
        self.changed.emit()

    @Slot(str, bool)
    def setTarget(self, platform: str, selected: bool) -> None:
        index = self._index(platform)
        if index < 0 or bool(self._rows[index]["target"]) == bool(selected):
            return
        self._rows[index]["target"] = bool(selected)
        model_index = self.index(index, 0)
        self.dataChanged.emit(model_index, model_index, [self.TargetRole])
        self.targetCountChanged.emit()
        self.changed.emit()

    @Slot(result=bool)
    def save(self) -> bool:
        targets = tuple(
            str(row["platform"])
            for row in self._rows
            if bool(row["target"])
        )
        try:
            target_value = serialize_distribution_target_platforms(targets)
        except ValueError as exc:
            self.message.emit(str(exc))
            return False
        values = {
            f"publish_account/{row['platform']}": str(row["account"] or "default")
            for row in self._rows
        }
        values["publish_target_platforms"] = target_value
        try:
            self._settings.set_many(values)
        except Exception as exc:
            self.message.emit(f"保存账号设置失败：{exc}")
            return False
        self.message.emit("账号和默认发布平台已保存")
        self.saved.emit()
        return True

    @Slot(str, str)
    def runAction(self, platform: str, action: str) -> None:
        index = self._index(platform)
        action = str(action or "").strip().casefold()
        if index < 0 or action not in {"login", "check"}:
            return
        row = self._rows[index]
        account = str(row["account"] or "").strip()
        if not account:
            self.message.emit("请先填写账号名称")
            return
        row["busy"] = True
        model_index = self.index(index, 0)
        self.dataChanged.emit(model_index, model_index, [self.BusyRole, self.StateTextRole, self.StateToneRole])
        try:
            started = bool(self._publish_service.run_account_action(platform, account, action))
        except Exception as exc:
            row["busy"] = False
            self.dataChanged.emit(
                model_index,
                model_index,
                [self.BusyRole, self.StateTextRole, self.StateToneRole],
            )
            self.message.emit(f"无法启动账号操作：{exc}")
            return
        if started:
            return
        row["busy"] = False
        self.dataChanged.emit(model_index, model_index, [self.BusyRole, self.StateTextRole, self.StateToneRole])
        self.message.emit("该账号已有操作正在执行，或发布服务正在关闭")

    @Slot()
    def refresh(self) -> None:
        if not self._rows:
            return
        self.dataChanged.emit(self.index(0, 0), self.index(len(self._rows) - 1, 0), list(self._ROLE_NAMES))

    @Slot()
    def reloadSettings(self) -> None:
        if not self._rows:
            return
        previous_target_count = self.targetCount
        targets = set(
            distribution_target_platforms(
                self._settings.get("publish_target_platforms")
            )
        )
        for row in self._rows:
            platform = str(row["platform"])
            if not row["busy"]:
                row["account"] = (
                    str(self._settings.get(f"publish_account/{platform}") or "default").strip()
                    or "default"
                )
            row["target"] = platform in targets
        self.dataChanged.emit(self.index(0, 0), self.index(len(self._rows) - 1, 0), list(self._ROLE_NAMES))
        if self.targetCount != previous_target_count:
            self.targetCountChanged.emit()

    @Slot(str, str, str, bool, str)
    def _account_status(
        self,
        platform: str,
        account: str,
        _action: str,
        ok: bool,
        result: str,
    ) -> None:
        index = self._index(platform)
        if index < 0:
            return
        row = self._rows[index]
        if str(row["account"]) != str(account or ""):
            return
        row["busy"] = False
        model_index = self.index(index, 0)
        self.dataChanged.emit(model_index, model_index, [self.BusyRole, self.StateTextRole, self.StateToneRole])
        self.message.emit(str(result or ("Cookie 有效" if ok else "Cookie 无效")))

    def _index(self, platform: str) -> int:
        target = str(platform or "").strip().casefold()
        return next(
            (index for index, row in enumerate(self._rows) if row["platform"] == target),
            -1,
        )

    @staticmethod
    def _state_presentation(state: object) -> tuple[str, str]:
        if not isinstance(state, dict):
            return "未检查", "muted"
        if bool(state.get("ok")):
            return "Cookie 有效", "success"
        return "Cookie 无效", "danger"
