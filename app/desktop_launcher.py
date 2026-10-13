from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


def launch_desktop() -> int:
    """Launch the Electron workspace from the Python development entry point."""
    root = Path(__file__).resolve().parents[1]
    desktop = root / "desktop"
    node = shutil.which("node")
    if node is None:
        print("Node.js is required for the desktop development launcher. Install Node.js, then run npm ci in desktop.", file=sys.stderr)
        return 1
    if not (desktop / "node_modules" / "typescript" / "bin" / "tsc").is_file():
        print("Desktop dependencies are missing. Run npm ci in desktop, then launch again.", file=sys.stderr)
        return 1
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    environment = {**os.environ, "HUIFA_PYTHON": sys.executable}
    for command in ([node, "node_modules/typescript/bin/tsc", "-p", "tsconfig.json"], [node, "scripts/build-renderer.cjs"]):
        result = subprocess.run(command, cwd=desktop, env=environment, creationflags=flags)
        if result.returncode:
            return int(result.returncode)
    resolved = subprocess.run([node, "-p", "require('electron')"], cwd=desktop, env=environment, capture_output=True, text=True, encoding="utf-8", creationflags=flags)
    if resolved.returncode:
        print(resolved.stderr, file=sys.stderr)
        return int(resolved.returncode)
    executable = next((line.strip() for line in reversed(resolved.stdout.splitlines()) if Path(line.strip()).is_file()), "")
    if not executable:
        print("Electron executable is unavailable. Run npm ci in desktop.", file=sys.stderr)
        return 1
    return int(subprocess.run([executable, str(desktop), *sys.argv[1:]], cwd=root, env=environment, creationflags=flags).returncode)
