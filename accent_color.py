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

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
APPEARANCE_PATH = os.path.join(PROJECT_DIR, "appearance.json")


def read_accent():
    import winreg

    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM")
    value, _ = winreg.QueryValueEx(key, "ColorizationColor")
    # Stored as 0xAARRGGBB
    r = (value >> 16) & 0xFF
    g = (value >> 8) & 0xFF
    b = value & 0xFF
    return f"#{r:02x}{g:02x}{b:02x}"


def main():
    if sys.platform != "win32":
        sys.exit("accent_color.py only works on Windows.")

    accent = read_accent()

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
