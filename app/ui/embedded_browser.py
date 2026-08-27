from __future__ import annotations

"""Cookie viewer retained after consolidating login onto SAU Chromium.

The application no longer embeds QtWebEngine. Interactive sign-in is owned by
the bundled social-auto-upload/Playwright runtime, while this
module provides only the safe, read-only Cookie inspection UI.
"""

from PySide6.QtCore import QDateTime
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from app.core.browser_cookies import BrowserCookie, CookieVault, deduplicate_cookies
from app.ui.i18n import format_text as ui_format, text as ui_text


class CookieViewerDialog(QDialog):
    """Read-only, domain-grouped view of one encrypted Cookie profile."""

    def __init__(self, cookies_provider, *, profile_id: str = "", parent=None):
        super().__init__(parent)
        self.cookies_provider = cookies_provider
        self.profile_id = str(profile_id or "default")
        self.setWindowTitle(ui_text("Cookie Viewer - Embedded Browser"))
        self.resize(980, 600)
        self.setMinimumSize(760, 460)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        title = QLabel(ui_text("Current Browser Cookies"))
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        self.summary = QLabel()
        self.summary.setObjectName("mutedText")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        controls = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setClearButtonEnabled(True)
        self.search.setPlaceholderText(ui_text("Search domain, cookie name, or path"))
        self.show_values = QCheckBox(ui_text("Show cookie values (sensitive)"))
        self.show_values.setToolTip(ui_text(
            "Cookie values may be equivalent to login credentials. They are hidden by default and never written to logs or diagnostics.",
        ))
        refresh = QPushButton(ui_text("Refresh"))
        controls.addWidget(self.search, 1)
        controls.addWidget(self.show_values)
        controls.addWidget(refresh)
        layout.addLayout(controls)

        self.sensitive_note = QLabel(ui_text(
            "Cookie values are hidden. Show them only when no one else can see the screen.",
        ))
        self.sensitive_note.setObjectName("mutedText")
        self.sensitive_note.setWordWrap(True)
        layout.addWidget(self.sensitive_note)

        self.tree = QTreeWidget()
        self.tree.setObjectName("cookieTree")
        self.tree.setHeaderLabels([
            ui_text("Domain / Cookie Name"), ui_text("Value"),
            ui_text("Path"), ui_text("Expires"), "Secure", "HttpOnly", "SameSite",
        ])
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        for column in range(2, 7):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        layout.addWidget(self.tree, 1)

        actions = QHBoxLayout()
        actions.addStretch(1)
        close = QPushButton(ui_text("Close"))
        actions.addWidget(close)
        layout.addLayout(actions)

        self.search.textChanged.connect(self.refresh)
        self.show_values.toggled.connect(self.refresh)
        self.show_values.toggled.connect(self._update_sensitive_note)
        refresh.clicked.connect(self.refresh)
        close.clicked.connect(self.accept)
        self.refresh()

    def _update_sensitive_note(self, visible: bool) -> None:
        if visible:
            self.sensitive_note.setText(ui_text(
                "Cookie values are visible. Do not capture, paste into chats, or include them in diagnostics.",
            ))
            self.sensitive_note.setStyleSheet("color: #d48716; font-weight: 600;")
        else:
            self.sensitive_note.setText(ui_text(
                "Cookie values are hidden. Show them only when no one else can see the screen.",
            ))
            self.sensitive_note.setStyleSheet("")

    @staticmethod
    def _expiry_text(cookie: BrowserCookie) -> str:
        if cookie.expires <= 0:
            return ui_text("Session")
        timestamp = QDateTime.fromSecsSinceEpoch(cookie.expires).toLocalTime()
        value = timestamp.toString("yyyy-MM-dd HH:mm")
        if cookie.expires <= QDateTime.currentSecsSinceEpoch():
            return ui_format("Expired · {time}", time=value)
        return value

    def refresh(self, *_args) -> None:
        cookies = deduplicate_cookies(self.cookies_provider() or [])
        query = self.search.text().strip().casefold()
        if query:
            cookies = [
                cookie for cookie in cookies
                if query in cookie.domain.casefold()
                or query in cookie.name.casefold()
                or query in cookie.path.casefold()
            ]
        grouped: dict[str, list[BrowserCookie]] = {}
        for cookie in cookies:
            grouped.setdefault(cookie.domain, []).append(cookie)

        self.tree.clear()
        show_values = self.show_values.isChecked()
        for domain in sorted(grouped, key=lambda value: value.lstrip(".").casefold()):
            domain_cookies = sorted(
                grouped[domain], key=lambda item: (item.name.casefold(), item.path)
            )
            group = QTreeWidgetItem([
                domain, ui_format("{count} items", count=len(domain_cookies)),
            ])
            group.setToolTip(0, ui_format(
                "Domain: {domain}\nCookies: {count}",
                domain=domain, count=len(domain_cookies),
            ))
            font = group.font(0)
            font.setBold(True)
            group.setFont(0, font)
            self.tree.addTopLevelItem(group)
            for cookie in domain_cookies:
                item = QTreeWidgetItem([
                    cookie.name,
                    cookie.value if show_values else "••••••••",
                    cookie.path,
                    self._expiry_text(cookie),
                    ui_text("Yes") if cookie.secure else ui_text("No"),
                    ui_text("Yes") if cookie.http_only else ui_text("No"),
                    cookie.same_site,
                ])
                item.setToolTip(0, f"{cookie.domain}{cookie.path}\n{cookie.name}")
                if not show_values:
                    item.setToolTip(1, ui_text(
                        "Value hidden. Enable Show cookie values (sensitive) to view it.",
                    ))
                group.addChild(item)
            group.setExpanded(True)

        if not grouped:
            self.tree.addTopLevelItem(QTreeWidgetItem([
                ui_text("No matching cookies")
                if query else ui_text("The browser has no cookies yet"),
            ]))
        self.summary.setText(ui_format(
            "Profile: {profile} · {cookies} cookies · {domains} domains · Encrypted persistently with Windows DPAPI and restored after restart.",
            profile=self.profile_id, cookies=len(cookies), domains=len(grouped),
        ))


def open_vault_cookie_viewer(profile_id: str, parent=None) -> int:
    """Open the encrypted profile without exposing values to callers."""
    vault = CookieVault()
    return CookieViewerDialog(
        lambda: vault.load(profile_id),
        profile_id=profile_id,
        parent=parent,
    ).exec()
