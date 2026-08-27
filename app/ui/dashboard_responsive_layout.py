from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QWidget


@dataclass(slots=True)
class DashboardResponsiveControls:
    input_layout: QGridLayout
    url: QWidget
    paste_button: QWidget
    add_button: QWidget
    smart_layout: QGridLayout
    smart_bar: QWidget
    smart_badge: QWidget
    smart_summary: QWidget
    content_label: QWidget
    content_menu: QWidget
    quality_label: QWidget
    quality_menu: QWidget
    format_label: QWidget
    container: QWidget
    filter_layout: QGridLayout
    tasks_label: QWidget
    search_box: QWidget
    sort_label: QWidget
    sort_box: QWidget
    filter_box: QWidget
    action_layout: QGridLayout
    download_dir_hint: QWidget
    open_download_dir_button: QWidget
    action_separator: QWidget
    pause_all_button: QWidget
    resume_all_button: QWidget
    log_button: QWidget
    cleanup_button: QWidget


class DashboardResponsiveLayoutController:
    """Own the dashboard toolbar breakpoints and grid placement rules."""

    NARROW_WIDTH = 1040
    COMPACT_WIDTH = 620

    def __init__(self, controls: DashboardResponsiveControls) -> None:
        self.controls = controls

    @staticmethod
    def _clear_grid(layout: QGridLayout) -> None:
        while layout.count():
            layout.takeAt(0)
        for column in range(12):
            layout.setColumnStretch(column, 0)

    def apply(self, width: int) -> None:
        controls = self.controls
        narrow = max(0, int(width)) < self.NARROW_WIDTH
        compact = max(0, int(width)) < self.COMPACT_WIDTH

        self._clear_grid(controls.input_layout)
        if compact:
            controls.input_layout.addWidget(controls.url, 0, 0, 1, 3)
            controls.input_layout.addWidget(controls.paste_button, 1, 1)
            controls.input_layout.addWidget(controls.add_button, 1, 2)
        else:
            controls.input_layout.addWidget(controls.url, 0, 0)
            controls.input_layout.addWidget(controls.paste_button, 0, 1)
            controls.input_layout.addWidget(controls.add_button, 0, 2)
        controls.input_layout.setColumnStretch(0, 1)

        self._clear_grid(controls.smart_layout)
        if compact:
            controls.smart_layout.addWidget(controls.smart_badge, 0, 0)
            controls.smart_layout.addWidget(controls.smart_summary, 0, 1, 1, 3)
            controls.smart_layout.addWidget(controls.content_label, 1, 0)
            controls.smart_layout.addWidget(controls.content_menu, 1, 1, 1, 2)
            controls.smart_layout.addWidget(controls.quality_label, 2, 0)
            controls.smart_layout.addWidget(controls.quality_menu, 2, 1, 1, 2)
            controls.smart_layout.addWidget(controls.format_label, 3, 0)
            controls.smart_layout.addWidget(controls.container, 3, 1, 1, 2)
            controls.smart_layout.setColumnStretch(1, 1)
        elif narrow:
            controls.smart_layout.addWidget(controls.smart_badge, 0, 0)
            controls.smart_layout.addWidget(controls.smart_summary, 0, 1, 1, 6)
            controls.smart_layout.addWidget(controls.content_label, 1, 0)
            controls.smart_layout.addWidget(
                controls.content_menu, 1, 1, Qt.AlignLeft
            )
            controls.smart_layout.addWidget(controls.quality_label, 1, 2)
            controls.smart_layout.addWidget(
                controls.quality_menu, 1, 3, Qt.AlignLeft
            )
            controls.smart_layout.addWidget(controls.format_label, 1, 4)
            controls.smart_layout.addWidget(
                controls.container, 1, 5, Qt.AlignLeft
            )
            controls.smart_layout.setColumnStretch(6, 1)
        else:
            controls.smart_layout.addWidget(controls.smart_badge, 0, 0)
            controls.smart_layout.addWidget(controls.smart_summary, 0, 1)
            controls.smart_layout.addWidget(controls.content_label, 0, 2)
            controls.smart_layout.addWidget(
                controls.content_menu, 0, 3, Qt.AlignLeft
            )
            controls.smart_layout.addWidget(controls.quality_label, 0, 4)
            controls.smart_layout.addWidget(
                controls.quality_menu, 0, 5, Qt.AlignLeft
            )
            controls.smart_layout.addWidget(controls.format_label, 0, 6)
            controls.smart_layout.addWidget(
                controls.container, 0, 7, Qt.AlignLeft
            )
            controls.smart_layout.setColumnStretch(1, 1)

        controls.smart_layout.activate()
        controls.smart_bar.setMinimumHeight(
            max(0, controls.smart_layout.minimumSize().height())
        )

        self._clear_grid(controls.filter_layout)
        controls.filter_layout.addWidget(controls.tasks_label, 0, 0)
        controls.filter_layout.addWidget(controls.search_box, 0, 1)
        controls.filter_layout.addWidget(controls.sort_label, 0, 2)
        controls.filter_layout.addWidget(controls.sort_box, 0, 3)
        controls.filter_layout.addWidget(controls.filter_box, 0, 4)
        controls.filter_layout.setColumnStretch(1, 1)

        self._clear_grid(controls.action_layout)
        controls.action_layout.addWidget(controls.download_dir_hint, 0, 0)
        controls.action_layout.addWidget(controls.open_download_dir_button, 0, 1)
        controls.action_layout.addWidget(controls.action_separator, 0, 2)
        controls.action_layout.addWidget(controls.pause_all_button, 0, 3)
        controls.action_layout.addWidget(controls.resume_all_button, 0, 4)
        controls.action_layout.addWidget(controls.log_button, 0, 5)
        controls.action_layout.addWidget(controls.cleanup_button, 0, 6)
        controls.action_layout.setColumnStretch(0, 1)
        controls.action_separator.show()
