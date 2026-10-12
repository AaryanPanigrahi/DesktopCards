"""
Builds DesktopCards.exe.

    python build.py

Regenerates the icon from logo.png, then runs PyInstaller against
DesktopCards.spec. The output is dist/DesktopCards.exe -- a single file
with Python and every dependency inside it, so it runs on a machine with
no Python installed.

Why a script rather than just the PyInstaller command: the icon has to be
rebuilt first or a changed logo.png silently doesn't take effect, and the
build is worth checking afterwards (PyInstaller exits 0 on plenty of
builds that don't actually run).
"""

import os
import shutil
import subprocess
import sys

import make_icon
from paths import ASSET_DIR

SPEC = os.path.join(ASSET_DIR, "DesktopCards.spec")
DIST = os.path.join(ASSET_DIR, "dist")
EXE = os.path.join(DIST, "DesktopCards.exe")
SETUP_EXE = os.path.join(DIST, "DesktopCards-Setup.exe")


def locked_exes():
    """Which of the build outputs a running process is holding open.

    PyInstaller deletes the old exe before writing the new one, and this
    script then copies it to the setup name, so either file being in use
    turns the build into a bare PermissionError from deep inside
    somebody else's code. Checking first means saying what's wrong.

    Renaming is the test: Windows refuses it for a file that's executing
    and it's harmless when nothing holds it. Both names matter -- they're
    the same binary, and the setup copy is just as likely to be the one
    left open."""
    locked = []
    for path in (EXE, SETUP_EXE):
        if not os.path.exists(path):
            continue
        probe = path + ".locktest"
        try:
            os.rename(path, probe)
            os.rename(probe, path)
        except OSError:
            locked.append(path)
    return locked


def main():
    busy = locked_exes()
    if busy:
        sys.exit(
            "These are still running and have to be closed first:\n  "
            + "\n  ".join(busy)
            + "\n\nTray icon -> Quit, or: Stop-Process -Name DesktopCards*\n"
            "The build replaces those files, and Windows won't allow that "
            "while they're executing."
        )

    print("1/3  icon")
    make_icon.main()

    print("\n2/3  PyInstaller")
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", SPEC, "--noconfirm", "--clean"],
        cwd=ASSET_DIR,
    )
    if result.returncode != 0:
        sys.exit(f"PyInstaller failed (exit {result.returncode}).")

    print("\n3/3  result")
    if not os.path.exists(EXE):
        sys.exit("PyInstaller reported success but produced no exe.")
    size_mb = os.path.getsize(EXE) / (1024 * 1024)
    print(f"{EXE}  ({size_mb:.1f} MB)")

    # The name is what picks the mode (see desktopcards.looks_like_setup_build),
    # so the setup copy is just this same binary under the other name.
    shutil.copy2(EXE, SETUP_EXE)
    print(f"{SETUP_EXE}  (same binary, name selects setup mode)")

    print(
        "\nNote: unsigned, so the first run trips SmartScreen "
        "(More info -> Run anyway)."
    )


if __name__ == "__main__":
    main()
