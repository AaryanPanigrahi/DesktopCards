"""
Reads the current Windows accent color from the registry and records it in
theme.json. serve.py reads theme.json live on every request, so the
dashboard picks up the new color on its next load -- no separate sync step
needed. Re-run this any time you change your Windows accent color
(Settings > Personalization > Colors).
"""

import json
import os
import sys

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
THEME_PATH = os.path.join(PROJECT_DIR, "theme.json")


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

    theme = {}
    if os.path.exists(THEME_PATH):
        with open(THEME_PATH, "r") as f:
            theme = json.load(f)
    theme["accent"] = accent
    theme.setdefault("scale", 1.0)
    theme.setdefault("extra_spacing", 0)

    with open(THEME_PATH, "w") as f:
        json.dump(theme, f, indent=2)

    print(f"Accent color {accent} -- saved to theme.json")


if __name__ == "__main__":
    main()
