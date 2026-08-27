from __future__ import annotations

import html as html_lib

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGroupBox,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from app.core.version import APP_NAME, APP_VERSION
from app.ui.i18n import format_text as ui_format
from app.ui.i18n import text as ui_text


THIRD_PARTY_ACKNOWLEDGEMENTS = (
    (
        "social-auto-upload",
        "Vendored source library imported in-process for visible sign-in, Cookie validation, and multi-platform publishing.",
        "MIT",
        "https://github.com/dreammis/social-auto-upload",
    ),
    (
        "yt-dlp",
        "Media information extraction and downloading across supported websites.",
        "Unlicense",
        "https://github.com/yt-dlp/yt-dlp",
    ),
    (
        "yt-dlp/FFmpeg-Builds",
        "FFmpeg and FFprobe builds used for stream merging, probing, and transcoding.",
        "Upstream licenses",
        "https://github.com/yt-dlp/FFmpeg-Builds",
    ),
    (
        "yt-dlp-ejs",
        "JavaScript challenge support used by yt-dlp extractors.",
        "Upstream license",
        "https://github.com/yt-dlp/ejs",
    ),
    (
        "Deno",
        "Recommended JavaScript runtime used by yt-dlp when a site requires JavaScript execution.",
        "MIT",
        "https://github.com/denoland/deno",
    ),
    (
        "Playwright / Chromium",
        "The single app-local Chromium automation runtime shared by sign-in, Cookie checks, and publishing.",
        "Apache-2.0 / Chromium licenses",
        "https://github.com/microsoft/playwright-python",
    ),
    (
        "CPython",
        "Python runtime embedded in the packaged application; end users do not install Python separately.",
        "Python Software Foundation License",
        "https://github.com/python/cpython",
    ),
    (
        "PySide6 / Qt",
        "Desktop interface. QtWebEngine is intentionally excluded because login uses the bundled Playwright Chromium.",
        "LGPL-3.0 / Qt commercial terms",
        "https://github.com/pyside/pyside-setup",
    ),
)


class AboutPage(QWidget):
    """Application identity, integration roles, licenses, and acknowledgements."""

    supported_sites_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)
        scroll.setWidget(content)
        outer.addWidget(scroll)

        title = QLabel(ui_text('About'))
        title.setObjectName("pageTitle")
        layout.addWidget(title)

        identity = QGroupBox(APP_NAME)
        identity_layout = QVBoxLayout(identity)
        version = QLabel(ui_format('Version {version}', version=APP_VERSION))
        version.setStyleSheet("font-weight: 600;")
        identity_layout.addWidget(version)
        summary = QLabel(ui_text(
            'This application combines media downloading, one app-managed login and publishing browser, encrypted Cookie storage, and multi-platform publishing workflows.',
        ))
        summary.setWordWrap(True)
        identity_layout.addWidget(summary)
        browser_note = QLabel(ui_text(
            'The packaged application embeds social-auto-upload source, its Python dependencies and one app-local Chromium runtime. Interactive sign-in and publishing reuse it; the user does not install Python and the application does not call a browser installed by the user.',
        ))
        browser_note.setObjectName("mutedText")
        browser_note.setWordWrap(True)
        identity_layout.addWidget(browser_note)
        layout.addWidget(identity)

        supported_sites = QPushButton(ui_text('Supported Sites'))
        supported_sites.setToolTip(ui_text(
            'View the extractor list provided by the current yt-dlp version',
        ))
        supported_sites.clicked.connect(self.supported_sites_requested)
        supported_sites.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        layout.addWidget(supported_sites, 0, Qt.AlignLeft)

        credits = QGroupBox(ui_text('Acknowledgements and Third-Party Components'))
        credits_layout = QVBoxLayout(credits)
        intro = QLabel(ui_text(
            'Thanks to the following open-source projects and their contributors. Each component remains subject to its own license.',
        ))
        intro.setWordWrap(True)
        credits_layout.addWidget(intro)
        for name, purpose, license_name, url in THIRD_PARTY_ACKNOWLEDGEMENTS:
            item = QFrame()
            item.setObjectName("settingsCard")
            item_layout = QVBoxLayout(item)
            item_layout.setContentsMargins(12, 10, 12, 10)
            heading = QLabel(f"<b>{html_lib.escape(name)}</b> · {html_lib.escape(license_name)}")
            heading.setTextFormat(Qt.RichText)
            item_layout.addWidget(heading)
            role = QLabel(ui_text(purpose))
            role.setWordWrap(True)
            item_layout.addWidget(role)
            link = QLabel(f'<a href="{html_lib.escape(url)}">{html_lib.escape(url)}</a>')
            link.setTextFormat(Qt.RichText)
            link.setTextInteractionFlags(Qt.TextBrowserInteraction)
            link.setOpenExternalLinks(True)
            item_layout.addWidget(link)
            credits_layout.addWidget(item)
        layout.addWidget(credits)
        layout.addStretch(1)

