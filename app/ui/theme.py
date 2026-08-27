from __future__ import annotations

from typing import Final


THEME_SYSTEM: Final = "system"
THEME_LIGHT: Final = "light"
THEME_DARK: Final = "dark"
THEME_CHOICES: Final = (THEME_SYSTEM, THEME_LIGHT, THEME_DARK)


def normalize_theme(value: str | None) -> str:
    value = str(value or "").strip().casefold()
    aliases = {
        "跟随系统": THEME_SYSTEM,
        "系统": THEME_SYSTEM,
        "system": THEME_SYSTEM,
        "浅色": THEME_LIGHT,
        "light": THEME_LIGHT,
        "明亮": THEME_LIGHT,
        "深色": THEME_DARK,
        "dark": THEME_DARK,
        "暗色": THEME_DARK,
    }
    return aliases.get(value, THEME_SYSTEM)


def resolve_theme(value: str | None, system_is_dark: bool = False) -> str:
    normalized = normalize_theme(value)
    if normalized == THEME_SYSTEM:
        return THEME_DARK if system_is_dark else THEME_LIGHT
    return normalized


_DARK_REPLACEMENTS: Final = {
    "#ffffff": "#20262f",
    "#fbfdff": "#252c36",
    "#fcfdff": "#252c36",
    "#fbfcfe": "#1c222b",
    "#f6f8fb": "#181e26",
    "#f8fafc": "#20262f",
    "#f4f8ff": "#1d2b3d",
    "#d8e7fb": "#314d70",
    "#dfecff": "#243d5d",
    "#e3e8ef": "#36404d",
    "#d9dee7": "#3d4857",
    "#e0e5ec": "#38424f",
    "#edf7ff": "#213b55",
    "#e8f3ff": "#263f5c",
    "#eaf3ff": "#263a52",
    "#edf9f2": "#1e392b",
    "#dff4e8": "#244b36",
    "#e9eef5": "#303946",
    "#f1f4f8": "#2d3540",
    "#e9f8ef": "#203e2d",
    "#fff0ee": "#452c2d",
    "#fff7e7": "#443924",
    "#edf1f5": "#323b47",
    "#354052": "#e9eef5",
    "#172033": "#f0f3f8",
    "#253247": "#e5ebf3",
    "#344054": "#edf2f7",
    "#53657d": "#b2c4db",
    "#66758a": "#aab6c6",
    "#6f7b8c": "#a7b2c1",
    "#7b8798": "#aab4c2",
    "#8090a6": "#aeb9c8",
    "#87909f": "#a6b0bf",
    "#8b96a6": "#a4afbe",
    "#98a2b3": "#a3afbf",
    "#9aa3b2": "#9ba6b5",
    "#4d5968": "#c0cad8",
    "#225ea8": "#82b9ff",
    "#2b8cff": "#4a9eff",
    "#2f7bdc": "#7db7ff",
    "#1f63ba": "#8bc1ff",
    "#126b42": "#79d7a5",
    "#0f5b38": "#9de2bd",
    "#18a957": "#27c77a",
    "#128945": "#36d889",
    "#39b86a": "#45c98a",
    "#138a4b": "#5edb91",
    "#20a35a": "#63df96",
    "#b26a00": "#e2ad63",
    "#d48716": "#e8b86e",
    "#c2413a": "#ff8f8a",
    "#d64444": "#ff8f8a",
    "#7c62d9": "#aa99ef",
    "#7b5bc7": "#bd9cff",
    "#151b24": "#11161d",
    "#2f3b4b": "#475465",
}


_LIGHT_STYLESHEET: Final = """
QWidget { font-size: 13px; }
QLineEdit, QComboBox { min-height: 30px; border: 1px solid #d9dee7; border-radius: 6px; padding: 0 28px 0 8px; background: white; color: #253247; }
QLineEdit:disabled, QComboBox:disabled, QTextEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled { color: #98a2b3; background: #f2f4f7; border-color: #e1e5eb; }
QComboBox:hover { border-color: #a9c6e8; background: #fbfdff; }
QComboBox:focus { border: 1px solid #2b8cff; }
QComboBox::drop-down { subcontrol-origin: padding; subcontrol-position: top right; width: 26px; border: none; border-left: 1px solid #e3e8ef; border-top-right-radius: 6px; border-bottom-right-radius: 6px; background: transparent; }
QComboBox::drop-down:hover { background: #edf5ff; }
QComboBox::down-arrow { image: none; width: 0; height: 0; border-left: 5px solid transparent; border-right: 5px solid transparent; border-top: 6px solid #6f7b8c; }
QComboBox QAbstractItemView { border: 1px solid #cbd8e8; border-radius: 6px; padding: 4px; background: #ffffff; color: #253247; selection-background-color: #e8f3ff; selection-color: #145da0; outline: none; }
QComboBox QAbstractItemView::item { min-height: 28px; padding: 4px 8px; border-radius: 4px; }
QComboBox QAbstractItemView::item:hover { background: #f0f6ff; }
QPushButton { min-height: 30px; border: 1px solid #d9dee7; border-radius: 6px; padding: 0 12px; background: white; }
QPushButton:hover { background: #f0f5ff; }
QPushButton#primaryButton { color: white; background: #18a957; border: none; font-weight: 600; }
QPushButton#primaryButton:hover { background: #128945; }
QPushButton#pasteDownloadButton { color: #126b42; background: #edf9f2; border-color: #a9dcc0; font-weight: 600; }
QPushButton#pasteDownloadButton:hover { color: #0f5b38; background: #dff4e8; border-color: #75c79a; }
QPushButton#linkButton { color: #2f7bdc; background: transparent; border: none; padding: 0 6px; }
QPushButton#linkButton:hover { color: #1f63ba; background: #eaf3ff; }
QPushButton:disabled { color: #98a2b3; background: #f2f4f7; border-color: #e1e5eb; }
QPushButton#primaryButton:disabled { color: #f8fafc; background: #aeb8c4; border: none; }
QPushButton#downloadOptionMenuButton { text-align: center; padding: 0 28px 0 16px; }
QPushButton#downloadOptionMenuButton::menu-indicator { subcontrol-origin: padding; subcontrol-position: center right; right: 9px; }
QComboBox#quickDownloadCombo { padding: 0 28px 0 16px; }
QMenu { border: 1px solid #cbd8e8; border-radius: 7px; padding: 4px; background: #ffffff; color: #253247; }
QMenu::item { min-height: 25px; padding: 4px 20px 4px 20px; border-radius: 4px; }
QMenu::item:selected { background: #e8f3ff; color: #145da0; }
QMenu::item:checked { font-weight: 700; }
QMenu::separator { height: 1px; margin: 5px 7px; background: #e3e8ef; }
QGroupBox { border: 1px solid #e3e8ef; border-radius: 10px; margin-top: 8px; padding-top: 10px; background: #ffffff; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #354052; background: #ffffff; }
QLabel#pageTitle { font-size: 22px; font-weight: 700; color: #172033; }
QLabel#mutedText { color: #87909f; }
QLabel#emptyState { color: #7b8798; padding: 40px; background: #ffffff; border: 1px dashed #cdd8e5; border-radius: 12px; font-size: 14px; line-height: 1.5em; }
QFrame#smartModeBar { background: #f4f8ff; border: 1px solid #d8e7fb; border-radius: 9px; }
QLabel#smartModeBadge { color: #225ea8; background: #dfecff; border-radius: 7px; padding: 3px 8px; font-weight: 700; }
QLabel#smartModeSummary { color: #53657d; border: none; background: transparent; }
QFrame#taskMetricCard { background: #ffffff; border: 1px solid #e3e8ef; border-radius: 10px; }
QFrame#taskMetricCard:hover { background: #fbfdff; border-color: #b9c9dd; }
QFrame#taskMetricCard[active="true"] { background: #edf7ff; border: 1px solid #2b8cff; }
QLabel#taskMetricValue { color: #344054; font-size: 20px; font-weight: 700; border: none; background: transparent; }
QLabel#taskMetricCaption { color: #7b8798; font-size: 11px; border: none; background: transparent; }
QFrame#taskMetricCard[tone="active"] QLabel#taskMetricValue { color: #2f7bdc; }
QFrame#taskMetricCard[tone="queued"] QLabel#taskMetricValue { color: #7c62d9; }
QFrame#taskMetricCard[tone="paused"] QLabel#taskMetricValue { color: #b26a00; }
QFrame#taskMetricCard[tone="success"] QLabel#taskMetricValue { color: #138a4b; }
QFrame#taskMetricCard[tone="danger"] QLabel#taskMetricValue { color: #c2413a; }
QListWidget#taskList { background: #f6f8fb; border: none; }
QFrame#taskCard { background: white; border: 1px solid #e3e8ef; border-radius: 10px; }
QFrame#taskCard:hover { background: #fbfdff; border-color: #c5d3e3; }
QFrame#taskCard[selected="true"] { background: #edf7ff; border: 1px solid #2b8cff; }
QFrame#completedCard { background: white; border: 1px solid #e3e8ef; border-radius: 10px; }
QFrame#completedCard:hover { border: 1px solid #b9c9dd; background: #fcfdff; }
QFrame#completedCard[selected="true"] { background: #edf7ff; border: 1px solid #2b8cff; }
QLabel#completedThumbnail { background: #e9eef5; color: #8090a6; border-radius: 8px; font-weight: 600; }
QLabel#completedTitle { color: #172033; font-size: 15px; font-weight: 700; }
QLabel#completedPath { color: #6f7b8c; }
QLabel#completedPath[missing="true"] { color: #c2413a; }
QLabel#distributionStatus { color: #b26a00; font-weight: 600; }
QLabel#distributionStatus[state="complete"] { color: #138a4b; }
QLabel#distributionChip { color: #66758a; background: #f1f4f8; border-radius: 8px; padding: 2px 7px; font-size: 11px; }
QLabel#distributionChip[state="success"] { color: #138a4b; background: #e9f8ef; }
QLabel#distributionChip[state="active"] { color: #2f7bdc; background: #eaf3ff; }
QLabel#distributionChip[state="failed"] { color: #c2413a; background: #fff0ee; }
QLabel#distributionChip[state="notStarted"] { color: #8a6a1f; background: #fff7e7; }
QLabel#coverStudioPreview { background: #151b24; border: 1px solid #2f3b4b; border-radius: 12px; color: #a9b4c2; }
QCheckBox { spacing: 0; }
QLabel#taskThumbnail { background: #e9eef5; color: #8090a6; border-radius: 6px; font-weight: 700; }
QLabel#platformIcon { background: rgba(255,255,255,235); border: 1px solid rgba(210,220,232,220); border-radius: 8px; padding: 3px; }
QLabel#taskTitle { color: #172033; font-size: 14px; font-weight: 700; }
QLabel#taskStatus { color: #3f6fca; min-width: 54px; }
QLabel#taskStage { color: #2f7bdc; font-weight: 600; }
QLabel#taskQuality { color: #19734a; background: #e9f8f0; border: 1px solid #c8ead8; border-radius: 5px; padding: 1px 6px; font-weight: 600; }
QLabel#taskPipeline { color: #98a2b3; font-size: 11px; }
QProgressBar { border: none; background: #edf1f5; border-radius: 7px; text-align: center; color: #4d5968; }
QProgressBar::chunk { background: #39b86a; border-radius: 7px; }
QTreeWidget { border: 1px solid #e3e8ef; border-radius: 8px; background: #ffffff; alternate-background-color: #f8fafc; outline: none; }
QTreeWidget::item { min-height: 28px; padding: 4px 6px; color: #253247; }
QTreeWidget::item:selected { background: #e8f3ff; color: #172033; }
QTableView::item:selected { background: #e8f3ff; color: #172033; }
QHeaderView::section { background: #f6f8fb; color: #66758a; font-weight: 600; border: none; border-bottom: 1px solid #e3e8ef; padding: 7px 8px; }
QLabel#formatCover, QLabel#formatRowCover { background: #e9eef5; color: #8090a6; border-radius: 6px; }
QListWidget { border: 1px solid #d9dee7; border-radius: 6px; background: #fbfcfe; }
QListWidget::item { border-radius: 6px; }
QListWidget::item:selected { background: #e8f3ff; color: #172033; }
QListWidget#supportedSitesList { color: #253247; outline: none; }
QListWidget#supportedSitesList::item { color: #253247; }
QListWidget#supportedSitesList::item:hover:!selected { background: #f4f8ff; color: #253247; }
QListWidget#supportedSitesList::item:selected { background: #e8f3ff; color: #172033; }
QListWidget#taskList::item:selected { background: transparent; border: none; }
QListWidget#taskList::item:hover { background: transparent; }
QListWidget#formatList::item:selected { background: #e8f3ff; border: 1px solid #2b8cff; }
QListWidget#completedList { color: #1f2937; background: #f6f8fb; border: none; }
QListWidget#completedList::item { color: #1f2937; background: transparent; border: none; }
QListWidget#completedList::item:selected { background: transparent; color: #1f2937; border: none; }
QListWidget#completedList::item:hover { background: transparent; color: #1f2937; }
QTreeWidget#publishQueueTree { border: 1px solid #dfe6ef; border-radius: 10px; background: #ffffff; alternate-background-color: #f8fafc; outline: none; }
QTreeWidget#publishQueueTree::item { min-height: 34px; padding: 5px 7px; color: #253247; border-bottom: 1px solid #eef2f6; }
QTreeWidget#publishQueueTree::item:hover { background: #f4f8ff; }
QTreeWidget#publishQueueTree::item:selected { background: #e8f3ff; color: #145da0; }
QTreeWidget#publishQueueTree QHeaderView::section { background: #f4f7fb; color: #53657d; border: none; border-bottom: 1px solid #dfe6ef; padding: 8px 8px; font-weight: 700; }
QWidget#formatRow { border-bottom: 1px solid #e0e5ec; }
QTabWidget::pane { border: none; }
QTabBar::tab { min-height: 36px; min-width: 88px; padding: 8px 16px; margin-right: 2px; color: #66758a; background: #f3f6fa; border: 1px solid #e1e7ef; border-bottom: 2px solid transparent; }
QTabBar::tab:hover { background: #e8f3ff; color: #225ea8; }
QTabBar::tab:selected { color: #145da0; background: #e8f3ff; border-bottom: 2px solid #2b8cff; font-weight: 700; }
QWidget#mainNavigation { background: #fbfcfe; }
QFrame#mainSidebar { background: #eef3f8; border: none; border-right: 1px solid #dce5ef; }
QStackedWidget#mainNavigationStack { background: #fbfcfe; }
QWidget#navigationHeader { background: transparent; }
QLabel#navigationBrandIcon { background: transparent; }
QLabel#navigationBrand { color: #26374d; font-size: 15px; font-weight: 800; }
QFrame#navigationDivider { border: none; border-top: 1px solid #dce5ef; max-height: 1px; }
QToolButton#navigationCollapseButton { color: #607289; background: transparent; border: none; border-radius: 9px; padding: 5px; }
QToolButton#navigationCollapseButton:hover { background: #dde7f3; }
QToolButton[navigationItem="true"] { color: #53657d; background: transparent; border: none; border-radius: 11px; padding: 0 12px; text-align: left; font-weight: 600; }
QToolButton[navigationItem="true"]:hover:!checked { color: #225ea8; background: #ddeafb; }
QToolButton[navigationItem="true"]:checked { color: #ffffff; background: #2f7bdc; font-weight: 700; }
QFrame#mainSidebar[collapsed="true"] QToolButton[navigationItem="true"] { padding: 0; }
QLabel#globalDownloadSpeed { color: #53657d; font-weight: 600; padding: 0 10px; }
QLabel#taskSummaryStatus { color: #7b8798; padding: 0 8px; }
"""


def build_application_stylesheet(theme: str = THEME_LIGHT) -> str:
    """Build the shared application QSS for the resolved light/dark theme."""
    if resolve_theme(theme) != THEME_DARK:
        return _LIGHT_STYLESHEET
    stylesheet = _LIGHT_STYLESHEET
    for source, target in _DARK_REPLACEMENTS.items():
        stylesheet = stylesheet.replace(source, target)
    # A few legacy rules use the named color ``white`` rather than a hex
    # literal.  Keep it for light mode (especially primary-button text), then
    # override only the input/surface selectors that must become dark.
    return stylesheet + """
QMainWindow, QDialog { background: #181e26; color: #edf2f7; }
QTabWidget, QStackedWidget, QScrollArea, QScrollArea > QWidget > QWidget { background: #181e26; }
QLabel, QCheckBox, QRadioButton { color: #edf2f7; background: transparent; }
QGroupBox { background: #20262f; }
QGroupBox::title { background: #20262f; }
QLineEdit, QComboBox, QTextEdit, QSpinBox, QDoubleSpinBox { background: #20262f; color: #edf2f7; selection-background-color: #365878; selection-color: #ffffff; }
QLineEdit:disabled, QComboBox:disabled, QTextEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled { color: #667282; background: #252b34; border-color: #36404d; }
QComboBox:hover { border-color: #5a7899; background: #26313d; }
QComboBox:focus { border: 1px solid #5ba7ff; }
QComboBox::drop-down { border-left-color: #36404d; }
QComboBox::drop-down:hover { background: #263f5c; }
QComboBox::down-arrow { border-top-color: #aab6c6; }
QPushButton { background: #20262f; color: #edf2f7; }
QPushButton:hover { background: #2b3745; }
QPushButton:disabled { color: #667282; background: #252b34; border-color: #36404d; }
QPushButton#primaryButton:disabled { color: #aab6c6; background: #475465; border: none; }
QComboBox QAbstractItemView { border-color: #475465; background: #20262f; color: #edf2f7; selection-background-color: #365878; selection-color: #ffffff; }
QComboBox QAbstractItemView::item:hover { background: #2b3745; }
QScrollArea, QScrollArea > QWidget > QWidget { background: #181e26; }
QFrame#taskCard, QFrame#completedCard { background: #20262f; }
QFrame#taskCard[selected="true"], QFrame#completedCard[selected="true"] { background: #213b55; }
QLabel#taskQuality { color: #80d9aa; background: #183b2b; border-color: #2b6248; }
QTabBar::tab { color: #aab6c6; background: #20262f; border: 1px solid #36404d; padding: 8px 14px; }
QTabBar::tab:selected { color: #edf2f7; background: #263f5c; }
QTabBar::tab { min-height: 36px; min-width: 88px; padding: 8px 16px; margin-right: 2px; }
QTabBar::tab:hover { background: #2b3745; }
QTabBar::tab:selected { border-bottom: 2px solid #5ba7ff; font-weight: 700; }
QWidget#mainNavigation { background: #181e26; }
QFrame#mainSidebar { background: #202832; border-right: 1px solid #36404d; }
QStackedWidget#mainNavigationStack { background: #181e26; }
QLabel#navigationBrand { color: #edf2f7; }
QFrame#navigationDivider { border-top-color: #36404d; }
QToolButton#navigationCollapseButton { color: #aab6c6; background: transparent; }
QToolButton#navigationCollapseButton:hover { background: #2b3745; }
QToolButton[navigationItem="true"] { color: #aab6c6; background: transparent; }
QToolButton[navigationItem="true"]:hover:!checked { color: #edf2f7; background: #2b3745; }
QToolButton[navigationItem="true"]:checked { color: #ffffff; background: #357fc9; }
QLabel#globalDownloadSpeed { color: #aab6c6; font-weight: 600; padding: 0 10px; }
QLabel#taskSummaryStatus { color: #aab6c6; padding: 0 8px; }
QTreeWidget#publishQueueTree { border-color: #36404d; background: #20262f; alternate-background-color: #1d232b; }
QTreeWidget#publishQueueTree::item { color: #edf2f7; border-bottom-color: #2c3541; }
QTreeWidget#publishQueueTree::item:hover { background: #26313d; }
QTreeWidget#publishQueueTree::item:selected { background: #263f5c; color: #ffffff; }
QTreeWidget#publishQueueTree QHeaderView::section { background: #20262f; color: #aab6c6; border-bottom-color: #36404d; }
QToolTip { color: #edf2f7; background: #26313d; border: 1px solid #475465; }
QLabel#emptyState { color: #aab6c6; background: #20262f; border-color: #475465; }
"""
