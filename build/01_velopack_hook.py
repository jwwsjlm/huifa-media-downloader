"""PyInstaller runtime hook for the separate Velopack onedir build."""

from app.core.application_updater import run_velopack_startup


run_velopack_startup()
