# Electron desktop

The new desktop runs Chromium in a sandboxed renderer and keeps the existing Python download, storage, cover and publishing services in a separate child process. Python owns Qt's event loop and SQLite. A fixed command/argument allowlist and bounded JSON protocol connect the two processes. Native dialogs and notifications belong to Electron; local image previews use backend-issued opaque tokens.

From the repository root, install the Python requirements into `.venv` as before. Then:

```powershell
cd desktop
npm ci
npm start
```

Set `HUIFA_PYTHON` to an absolute Python executable if the virtual environment lives elsewhere. Development and acceptance can set `HUIFA_DATA_ROOT` to an absolute temporary data directory. This affects all settings, database, cookie-vault and log helpers, not just the runtime database. `HUIFA_APPLICATION_ROOT` identifies the shared distribution directory; packaged frontend and `HuifaPythonBackend.exe` live beside one another in Velopack `current`.

To run checks:

```powershell
.venv/Scripts/python.exe -m unittest tests.test_electron_host -v
cd desktop
npm run build
npm test
```

An isolated smoke launch sets `HUIFA_PACKAGED_SMOKE_OUTPUT` to a JSON output path and `HUIFA_DATA_ROOT` to a temporary folder whose `config.ini` disables `auto_check_updates` and `desktop_notifications`. The real Electron renderer and Python backend start, report health and exit through the persistence/shutdown path.

The release builder currently exposes the new distribution as `-Electron` while acceptance is underway:

```powershell
./scripts/build_velopack_release.ps1 -Version <version> -Electron -RunSmokeChecks
```

It builds the Python backend with PyInstaller, stages Electron and the backend beside each other, and uses the existing Velopack package ID, portable/installer pipeline, runtime tools and persistent storage layout. Nothing is uploaded by this command.
