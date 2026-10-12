"""
Generates DesktopCards.ico from logo.png, for the exe's embedded icon.

A build step rather than a committed .ico, so logo.png stays the single
source of truth for the mark -- replace the PNG, rebuild, and the exe
icon follows.

PyInstaller's --icon needs a real .ico; it won't take a PNG. The actual
conversion lives in logo_cache.write_ico, which the running app also uses
for its shortcut icon, so there's one definition of what a DesktopCards
icon looks like rather than two that can drift.
"""

import os
import sys

from logo_cache import write_ico
from paths import ICON_PATH, logo_path


def main():
    source = logo_path()
    if not os.path.exists(source):
        sys.exit(f"{source} not found -- nothing to build an icon from.")

    write_ico(source, ICON_PATH)
    print(f"Wrote {ICON_PATH} from {source}")
    return ICON_PATH


if __name__ == "__main__":
    main()
