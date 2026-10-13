"""Opt-in NSIS compilation check; never installs or launches the output.

Run only when requested:
    python tests/check_nsis_installer.py "C:/Program Files (x86)/NSIS/makensis.exe"
"""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def check() -> None:
    compiler = sys.argv[1] if len(sys.argv) > 1 else shutil.which("makensis.exe")
    if not compiler:
        raise SystemExit("Pass the path to NSIS 3.12 makensis.exe.")
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="Huifa NSIS 路径 ") as temporary:
        folder = Path(temporary)
        # The compiler embeds this file without executing or interpreting it.
        msi = folder / "安装 payload.msi"
        msi.write_bytes(b"NSIS compile-only placeholder; not an installable MSI.")
        subprocess.run(
            [
                compiler, "/INPUTCHARSET", "UTF8",
                "/DAppVersion=0.0.0-check",
                "/DAppFileVersion=0.0.0.0",
                f"/DSourceMsi={msi}",
                f"/DOutputDir={folder}",
                f"/DAppIcon={root / 'assets' / 'huifa.ico'}",
                str(root / "build" / "HuifaMediaDownloader.nsi"),
            ],
            check=True,
        )
        with (folder / "HuifaMediaDownloader-Setup.exe").open("rb") as output:
            assert output.read(2) == b"MZ", "NSIS did not produce a Windows executable"
    print("NSIS compiled with Unicode/space paths. The installer was not run.")


if __name__ == "__main__":
    check()
