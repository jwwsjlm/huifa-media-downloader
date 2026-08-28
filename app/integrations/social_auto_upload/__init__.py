"""In-process integration for the vendored social-auto-upload source."""

from .runtime import (
    SocialAutoUploadError,
    account_check,
    account_login,
    core_status,
    publish_video,
    resolve_chromium_executable,
    runtime_home,
    vendor_commit,
    vendor_root,
)
