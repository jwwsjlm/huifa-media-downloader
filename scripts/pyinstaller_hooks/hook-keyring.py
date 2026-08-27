"""Windows-only keyring hook for Huifa release builds.

The upstream PyInstaller hook intentionally collects every keyring backend
and package metadata so a generic application can discover plugins on Linux,
macOS and Windows. Huifa configures WinVaultKeyring explicitly, so production
Windows builds need only the maintained Credential Manager backend and its
ctypes adapter.
"""

hiddenimports = [
    "keyring.backends.Windows",
    "win32ctypes.pywin32.pywintypes",
    "win32ctypes.pywin32.win32cred",
]

