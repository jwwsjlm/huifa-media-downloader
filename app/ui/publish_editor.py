from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from app.adapters.sau_adapter import SAU_SUPPORTED_PLATFORMS, get_sau_platform_capability
from app.storage.models import MediaItem
from app.ui.i18n import format_text as ui_format
from app.ui.i18n import runtime_text
from app.ui.i18n import text as ui_text
from app.ui.media_presentation import platform_label


class PublishPage(QWidget):
    """Editor that converts one completed media item into publish tasks."""

    def __init__(
        self,
        window,
        media: MediaItem,
        preselected_platforms: tuple[str, ...] = (),
    ) -> None:
        super().__init__()
        self._initialize_state(window, media)
        layout = self._build_scroll_content()
        layout.addWidget(QLabel(ui_text("Media: ") + f"{media.title}"))
        layout.addLayout(self._build_metadata_form())
        layout.addWidget(QLabel(ui_text("Platforms")))
        self.platforms = self._build_platform_list(preselected_platforms)
        layout.addWidget(self.platforms)
        layout.addWidget(self._build_account_summary())
        self.platform_settings_group = self._build_platform_settings()
        layout.addWidget(self.platform_settings_group)
        self.submit_button = QPushButton(ui_text("Save and Add to Publish Queue"))
        self.submit_button.clicked.connect(self.submit)
        layout.addWidget(self.submit_button)
        self.update_platform_settings()

    def _initialize_state(self, window, media: MediaItem) -> None:
        self.window = window
        self.media = media

    def _build_scroll_content(self) -> QVBoxLayout:
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(18, 16, 18, 16)
        scroll.setWidget(content)
        outer_layout.addWidget(scroll)
        return layout

    def _build_metadata_form(self) -> QFormLayout:
        self.title = QLineEdit(self.media.title)
        self.description = QTextEdit(self.media.description)
        self.topics = QLineEdit(" ".join(f"#{tag}" for tag in self.media.tags))
        form = QFormLayout()
        form.addRow(ui_text("Title"), self.title)
        form.addRow(ui_text("Description"), self.description)
        form.addRow(ui_text("Topics"), self.topics)
        return form

    def _build_platform_list(
        self,
        preselected_platforms: tuple[str, ...],
    ) -> QListWidget:
        preselected = {
            platform
            for platform in preselected_platforms
            if platform in SAU_SUPPORTED_PLATFORMS
        }
        platforms = QListWidget()
        for name in SAU_SUPPORTED_PLATFORMS:
            item = QListWidgetItem(platform_label(name))
            item.setData(Qt.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if name in preselected else Qt.Unchecked)
            platforms.addItem(item)
        platforms.itemChanged.connect(lambda *_: self.update_platform_settings())
        return platforms

    def _build_account_summary(self) -> QGroupBox:
        account_summary = QGroupBox(ui_text("Account Status"))
        account_summary_layout = QHBoxLayout(account_summary)
        account_summary_layout.setContentsMargins(12, 10, 12, 10)
        self.account_summary_label = QLabel(ui_text(
            "Accounts are managed in Accounts; cookies are checked before publishing."
        ))
        self.account_summary_label.setObjectName("mutedText")
        self.account_summary_label.setWordWrap(True)
        account_summary_layout.addWidget(self.account_summary_label, 1)
        manage_accounts = QPushButton(ui_text("Manage Accounts"))
        manage_accounts.setObjectName("primaryButton")
        manage_accounts.clicked.connect(self._open_accounts)
        account_summary_layout.addWidget(manage_accounts)
        return account_summary

    def _build_platform_settings(self) -> QGroupBox:
        group = QGroupBox(ui_text("Platform Settings"))
        platform_form = QFormLayout(group)
        self.source_type = QComboBox()
        self.source_type.addItem(ui_text("Original"), "original")
        self.source_type.addItem(ui_text("Repost"), "repost")
        self.source_url = QLineEdit(self.media.source_url)
        self.source_url.setReadOnly(True)
        self.source_ip = QLineEdit(str(self.media.source_ip or ""))
        self.source_ip.setPlaceholderText(ui_text("Not recorded"))
        self.source_ip.setReadOnly(True)
        self.visibility = QComboBox()
        self.visibility.addItem(ui_text("Public"), "public")
        self.visibility.addItem(ui_text("Unlisted (link required)"), "unlisted")
        self.visibility.addItem(ui_text("Private"), "private")
        self.collection = QLineEdit()
        self.collection.setPlaceholderText(ui_text("Optional: collection or album name"))
        self.playlist = QLineEdit()
        self.playlist.setPlaceholderText(ui_text("Optional: YouTube playlist name or ID"))
        self.partition = QLineEdit()
        self.partition.setPlaceholderText(ui_text("Required for Bilibili: numeric partition ID (tid)"))
        self.schedule = QLineEdit()
        self.schedule.setPlaceholderText(ui_text("Optional: YYYY-MM-DD HH:MM"))
        self.thumbnail = QLineEdit(self.media.thumbnail_path)
        self.thumbnail.setPlaceholderText(ui_text("Uses the cover saved during download by default"))
        self.thumbnail_landscape = QLineEdit()
        self.thumbnail_landscape.setPlaceholderText(ui_text("Douyin/Channels: landscape cover"))
        self.thumbnail_portrait = QLineEdit()
        self.thumbnail_portrait.setPlaceholderText(ui_text("Douyin/Channels: portrait cover"))
        self.thumbnail_row = self._path_row(
            self.thumbnail,
            ui_text("Choose"),
            lambda: self.choose_cover(self.thumbnail),
        )
        self.thumbnail_landscape_row = self._path_row(
            self.thumbnail_landscape,
            ui_text("Choose"),
            lambda: self.choose_cover(self.thumbnail_landscape),
        )
        self.thumbnail_portrait_row = self._path_row(
            self.thumbnail_portrait,
            ui_text("Choose"),
            lambda: self.choose_cover(self.thumbnail_portrait),
        )
        platform_form.addRow(ui_text("Source Type"), self.source_type)
        platform_form.addRow(ui_text("Source URL"), self.source_url)
        platform_form.addRow(ui_text("Source IP"), self.source_ip)
        platform_form.addRow(ui_text("Visibility"), self.visibility)
        platform_form.addRow(ui_text("Collection / Album"), self.collection)
        platform_form.addRow(ui_text("Playlist"), self.playlist)
        platform_form.addRow(ui_text("Bilibili Partition"), self.partition)
        platform_form.addRow(ui_text("Schedule"), self.schedule)
        platform_form.addRow(ui_text("Cover", context="publish.cover"), self.thumbnail_row)
        platform_form.addRow(ui_text("Landscape Cover"), self.thumbnail_landscape_row)
        platform_form.addRow(ui_text("Portrait Cover"), self.thumbnail_portrait_row)
        group.setVisible(False)
        return group

    def _open_accounts(self) -> None:
        self.window.tabs.setCurrentWidget(self.window.account_hub)

    def selected_platforms(self) -> tuple[str, ...]:
        return tuple(
            str(self.platforms.item(index).data(Qt.UserRole) or "")
            for index in range(self.platforms.count())
            if self.platforms.item(index).checkState() == Qt.Checked
        )

    def update_platform_settings(self) -> None:
        selected = self.selected_platforms()
        self.platform_settings_group.setVisible(bool(selected))
        account_fields = self.window.account_hub.platform_account_fields
        if selected:
            summaries: list[str] = []
            for platform in selected:
                account = account_fields.get(platform)
                account_name = account.text().strip() if account is not None else ""
                display_account = account_name or ui_text("Not set")
                state = self.window.publish_service.account_state(platform, account_name)
                state_text = (
                    ui_text("Cookie valid")
                    if isinstance(state, dict) and state.get("ok")
                    else ui_text("Cookie not checked")
                )
                summaries.append(
                    f"{platform_label(platform)}: {display_account} ({state_text})"
                )
            self.account_summary_label.setText(
                ui_text("Current accounts (managed in Accounts): ")
                + ", ".join(summaries)
                + ui_text(". Cookies are checked before publishing.")
            )
            self.platform_settings_group.setTitle(
                ui_text("Platform Settings (")
                + ", ".join(platform_label(name) for name in selected)
                + ui_text(")")
            )
        else:
            self.account_summary_label.setText(ui_text(
                "Select at least one platform; accounts and cookies are managed in Accounts."
            ))
            self.platform_settings_group.setTitle(ui_text("Platform Settings"))

        capabilities = [get_sau_platform_capability(name) for name in selected]
        capabilities = [item for item in capabilities if item is not None]
        field_visibility = {
            self.visibility: any(item.supports_visibility for item in capabilities),
            self.collection: any(item.supports_collection for item in capabilities),
            self.playlist: any(item.supports_playlist for item in capabilities),
            self.partition: any(item.requires_tid for item in capabilities),
            self.schedule: any(item.supports_schedule for item in capabilities),
            self.thumbnail_row: bool(capabilities),
            self.thumbnail_landscape_row: any(
                item.supports_dual_thumbnail for item in capabilities
            ),
            self.thumbnail_portrait_row: any(
                item.supports_dual_thumbnail for item in capabilities
            ),
        }
        form = self.platform_settings_group.layout()
        for field, visible in field_visibility.items():
            field.setVisible(visible)
            label = form.labelForField(field) if isinstance(form, QFormLayout) else None
            if label is not None:
                label.setVisible(visible)

    def submit(self) -> None:
        submission = self._validated_submission()
        if submission is None:
            return
        platforms, accounts, partition, schedule = submission
        tags = [
            value.lstrip("#")
            for value in self.topics.text().split()
            if value.strip()
        ]
        platform_settings = {
            platform: self._platform_submission_settings(
                platform,
                accounts[platform],
                partition=partition,
                schedule=schedule,
            )
            for platform in platforms
        }
        metadata = {
            "title": self.title.text(),
            "description": self.description.toPlainText(),
            "tags": tags,
        }
        if not self._create_publish_tasks(platforms, metadata, platform_settings):
            return
        preference_error = self._save_account_preferences(accounts)
        self.submit_button.setEnabled(True)
        self._show_submission_result(preference_error)
        self.window.publish_ui.complete_editor(self)

    def _validated_submission(
        self,
    ) -> tuple[tuple[str, ...], dict[str, str], str, str] | None:
        platforms = self.selected_platforms()
        if not platforms:
            QMessageBox.warning(self, ui_text("Notice"), ui_text("Select at least one platform."))
            return None

        account_fields = self.window.account_hub.platform_account_fields
        missing_accounts = [
            platform_label(platform)
            for platform in platforms
            if account_fields.get(platform) is None
            or not account_fields[platform].text().strip()
        ]
        if missing_accounts:
            QMessageBox.warning(
                self,
                ui_text("Publishing Account Missing"),
                ui_text("Enter account names for:\n") + ", ".join(missing_accounts),
            )
            return None

        partition = self.partition.text().strip()
        if "bilibili" in platforms and (not partition.isdigit() or int(partition) <= 0):
            QMessageBox.warning(
                self,
                ui_text("Partition ID Missing"),
                ui_text("Bilibili requires a valid numeric partition ID (tid)."),
            )
            return None
        schedule = self.schedule.text().strip()
        schedule_supported = any(
            capability is not None and capability.supports_schedule
            for capability in (
                get_sau_platform_capability(platform)
                for platform in platforms
            )
        )
        if not schedule_supported:
            schedule = ""
        if schedule:
            try:
                datetime.strptime(schedule, "%Y-%m-%d %H:%M")
            except ValueError:
                QMessageBox.warning(
                    self,
                    ui_text("Invalid Schedule"),
                    ui_text("Enter a valid publishing time in YYYY-MM-DD HH:MM format."),
                )
                return None
        accounts = {
            platform: account_fields[platform].text().strip()
            for platform in platforms
        }
        return platforms, accounts, partition, schedule

    def _platform_submission_settings(
        self,
        platform: str,
        account: str,
        *,
        partition: str,
        schedule: str,
    ) -> dict[str, object]:
        capability = get_sau_platform_capability(platform)
        settings: dict[str, object] = {
            "source_type": self.source_type.currentData(),
            "source_url": self.source_url.text().strip(),
            "source_ip": str(self.media.source_ip or "").strip(),
            "account": account,
            "thumbnail": self.thumbnail.text().strip(),
        }
        if capability is None:
            return settings
        if capability.supports_visibility:
            settings["visibility"] = str(self.visibility.currentData() or "public")
        if capability.supports_collection:
            settings["collection"] = self.collection.text().strip()
        if capability.supports_playlist:
            settings["playlist"] = self.playlist.text().strip()
        if capability.requires_tid:
            settings["partition"] = partition
        if capability.supports_schedule:
            settings["schedule"] = schedule
        if capability.supports_dual_thumbnail:
            settings["thumbnail_landscape"] = self.thumbnail_landscape.text().strip()
            settings["thumbnail_portrait"] = self.thumbnail_portrait.text().strip()
        return settings

    def _create_publish_tasks(
        self,
        platforms: tuple[str, ...],
        metadata: dict[str, object],
        platform_settings: dict[str, dict[str, object]],
    ) -> bool:
        self.submit_button.setEnabled(False)
        try:
            self.window.publish_service.create_tasks(
                self.media,
                platforms,
                metadata,
                platform_settings,
            )
        except Exception as exc:
            self.submit_button.setEnabled(True)
            QMessageBox.warning(
                self,
                ui_text("Unable to Create Publish Tasks"),
                ui_format("Unable to create publish tasks:\n{error}", error=runtime_text(exc)),
            )
            return False
        return True

    def _save_account_preferences(self, accounts: dict[str, str]) -> str:
        try:
            self.window.app_settings.set_many({
                f"publish_account/{platform}": account
                for platform, account in accounts.items()
            })
        except Exception as exc:
            return runtime_text(exc)
        return ""

    def _show_submission_result(self, preference_error: str) -> None:
        created = int(getattr(self.window.publish_service, "last_created_count", 0) or 0)
        existing = int(getattr(self.window.publish_service, "last_existing_count", 0) or 0)
        message = ui_format("Created {count} publish task(s).", count=created)
        if existing:
            message += ui_format(
                "\nFound {count} duplicate task(s); kept the existing tasks to avoid duplicate publishing.",
                count=existing,
            )
        if preference_error:
            message += "\n" + ui_format(
                "Publish tasks were created, but the account preference could not be saved: {error}",
                error=preference_error,
            )
            QMessageBox.warning(self, ui_text("Done"), message)
        else:
            QMessageBox.information(self, ui_text("Done"), message)

    @staticmethod
    def _path_row(field: QLineEdit, label: str, callback: Callable[[], None]) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(field, 1)
        button = QPushButton(label)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return row

    def choose_cover(self, field: QLineEdit) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            ui_text("Choose Publishing Cover"),
            field.text() or self.media.thumbnail_path,
            ui_text("Images (*.jpg *.jpeg *.png *.webp);;All Files (*)"),
        )
        if path:
            field.setText(path)
