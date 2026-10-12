"""
Reads the current Windows accent color from the registry and records it in
appearance.json. serve.py reads appearance.json live on every request, so
the dashboard picks up the new color on its next load -- no separate sync
step needed. Re-run this any time you change your Windows accent color
(Settings > Personalization > Colors).
"""

import json
import os
import sys

from paths import APPEARANCE_PATH


def main():
    if sys.platform != "win32":
        sys.exit("accent_color.py only works on Windows.")

    # The registry read itself lives in platform_win.py, with the rest of the
    # OS-specific calls, so a port has one file to replace rather than two.
    import platform_win

    accent = platform_win.system_accent()

    appearance = {}
    if os.path.exists(APPEARANCE_PATH):
        with open(APPEARANCE_PATH, "r") as f:
            appearance = json.load(f)
    appearance["accent"] = accent
    appearance.setdefault("scale", 1.0)
    appearance.setdefault("extra_spacing", 0)

    with open(APPEARANCE_PATH, "w") as f:
        json.dump(appearance, f, indent=2)

    print(f"Accent color {accent} -- saved to appearance.json")


if __name__ == "__main__":
    main()
