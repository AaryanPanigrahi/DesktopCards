"""
Launches the pinned balance dashboard as a genuinely frameless window using
pywebview (Windows' built-in WebView2 engine) instead of a Chrome --app
window -- Chrome paints its own minimal titlebar on --app windows
regardless of native window styles, so it can't be made truly frameless.

Starts serve.py as a background subprocess, opens the frameless webview
window pinned to the configured position/size, exposes a resize API the
page calls to self-fit its height, and (optionally) reparents the window
onto the desktop's own WorkerW layer so it survives Win+D.

That WorkerW layer doesn't receive normal mouse input (the same reason
Rainmeter/Wallpaper Engine skins need a system-wide hook to be clickable),
so there's no way to make something inside the page itself clickable while
pinned. Manual refresh/quit instead live in a system tray icon, which is
a normal interactive window regardless of where the main widget is parented.
"""

import ctypes
import ctypes.wintypes
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

import webview
from PIL import Image, ImageDraw
import pystray

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PROJECT_DIR, "config.json")
THEME_PATH = os.path.join(PROJECT_DIR, "theme.json")
CACHE_DIR = os.path.join(PROJECT_DIR, "cache")
THEME_SNAPSHOT_PATH = os.path.join(CACHE_DIR, "theme_snapshot.json")

WINDOW_TITLE = "Balances"
PAGE = "dashboard.html"

# Not config: WINDOW_WIDTH is only the size before the page's first layout
# pass corrects it (dashboard.js fitWindowToContent immediately resizes to
# the real, scale-aware width -- keep in sync with dashboard.html's #shell
# { width: ... }, the actual design-width source of truth). max_height is a
# safety ceiling (so a lot of accounts can't grow the window past a sane
# size), not a tuned preference -- the actual height always comes from the
# page measuring its own content. extra_spacing in theme.json is what
# controls breathing room, not these.
WINDOW_WIDTH = 400
MAX_HEIGHT = 700


def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def load_theme():
    with open(THEME_PATH, "r") as f:
        return json.load(f)


def load_theme_snapshot():
    """theme.json as of the end of the last reload, so refresh_now() can
    tell whether the native window actually needs touching (corner
    radius, position) instead of reapplying it unconditionally."""
    if not os.path.exists(THEME_SNAPSHOT_PATH):
        return None
    with open(THEME_SNAPSHOT_PATH, "r") as f:
        return json.load(f)


def save_theme_snapshot(theme):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(THEME_SNAPSHOT_PATH, "w") as f:
        json.dump(theme, f, indent=2)


def apply_rounded_corners(window, radius):
    """Clips the actual OS window to a rounded-rect region via
    SetWindowRgn -- the area outside it simply isn't painted, so whatever
    is behind the window (desktop or another window) shows through there.
    This is the real way to get a non-rectangular window shape on Win32;
    pywebview's transparent=True only makes WebView2's own background
    transparent against the Form's paint, not the window's actual shape,
    so it doesn't give rounded corners by itself.

    Reads the window's actual client rect in device pixels rather than
    trusting our own WINDOW_WIDTH/resize-height values -- those are CSS
    pixels, and at any display scaling above 100% the real window is
    physically larger, so a region built from the CSS numbers clipped off
    a real slice of the right/bottom edge."""
    if radius <= 0:
        return
    hwnd = ctypes.c_void_p(window.native.Handle.ToInt64())
    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(rect))
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    hrgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius, radius)
    ctypes.windll.user32.SetWindowRgn(hwnd, hrgn, True)


class Api:
    def __init__(self, window, max_height, corner_radius):
        self._window = window
        self._max_height = max_height
        self._corner_radius = corner_radius

    def resize_window(self, width, height):
        # Width has no cap (only height does, as a safety ceiling) -- a
        # scale above 1 is expected to grow the window as wide as it needs.
        width = max(1, int(width))
        height = max(1, min(int(height), self._max_height))
        self._window.resize(width, height)
        apply_rounded_corners(self._window, self._corner_radius)


def hide_from_taskbar(window):
    """Hides the window's taskbar button -- WS_EX_TOOLWINDOW instead of
    WS_EX_APPWINDOW. Requires a hide/show cycle for the taskbar to actually
    drop the button; just changing the style bit isn't enough."""
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_APPWINDOW = 0x00040000
    SW_HIDE, SW_SHOW = 0, 5

    user32 = ctypes.windll.user32
    hwnd = ctypes.c_void_p(window.native.Handle.ToInt64())

    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style = (style & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW
    user32.ShowWindow(hwnd, SW_HIDE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    user32.ShowWindow(hwnd, SW_SHOW)


def find_workerw(user32):
    """Finds the window to reparent onto, behind the desktop icons. Usually
    that's a dedicated WorkerW sibling of whichever top-level window hosts
    SHELLDLL_DefView (Windows 10 1809+), but on builds where SHELLDLL_DefView
    is parented directly to Progman with no separate WorkerW at all (true on
    this machine), Progman itself is the right target."""
    progman = user32.FindWindowW("Progman", None)
    result = ctypes.c_ulong()
    user32.SendMessageTimeoutW(progman, 0x052C, 0, 0, 0, 1000, ctypes.byref(result))

    defview_host = None

    def enum_windows(hwnd_candidate, _lparam):
        nonlocal defview_host
        if user32.FindWindowExW(hwnd_candidate, None, "SHELLDLL_DefView", None):
            defview_host = hwnd_candidate
            return False  # found it, stop enumerating
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows(WNDENUMPROC(enum_windows), 0)

    if not defview_host:
        return None

    workerw = user32.FindWindowExW(None, defview_host, "WorkerW", None)
    return workerw if workerw else defview_host


def pin_to_desktop(window):
    """Reparents the window onto the WorkerW layer behind desktop icons so
    it survives Win+D, the same trick Rainmeter/Wallpaper Engine use. Some
    Windows builds don't have WorkerW ready on the first message -- retry
    a few times before giving up."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.restype = ctypes.c_void_p
    user32.FindWindowExW.restype = ctypes.c_void_p
    user32.FindWindowExW.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p
    ]
    user32.SetParent.restype = ctypes.c_void_p
    user32.SetParent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    # .ToInt64() matters: .ToInt32() truncates a 64-bit HWND on x64 Windows,
    # producing a garbage handle that SetParent fails on silently.
    hwnd = ctypes.c_void_p(window.native.Handle.ToInt64())

    for _ in range(5):
        workerw = find_workerw(user32)
        if workerw:
            user32.SetParent(hwnd, workerw)
            print(f"Pinned to desktop (behind icons, target 0x{workerw:X})")
            return
        time.sleep(0.4)

    print("Could not find WorkerW after 5 attempts -- Win+D persistence not applied")


def build_tray_image(accent):
    """A plain colored circle, drawn with PIL rather than bundling an icon
    file -- one less asset to ship, and it automatically matches whatever
    accent color theme.json has."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, size - 4, size - 4), fill=accent)
    return img


def refresh_now(port, window):
    """Tray 'Reload': always re-pulls from Plaid (serve.py's /refresh
    re-reads config.json fresh and re-runs fetch_data.main() in-process --
    no skipping that, by design) and always tells the page to re-fetch
    data.json + theme.json (reloadAll() -- cheap either way, since it's
    just a local fetch + a few CSS variable writes). The native-window
    side (corner radius clip, position) is the part worth gating: those
    are real OS calls, not just local JS, so they only run when theme.json
    actually differs from the last reload's snapshot -- the common case
    (hit Reload just to see fresh balances, nothing about the theme
    changed) skips them entirely instead of redoing a no-op every time.
    survive_show_desktop is deliberately never touched here at all --
    re-pinning (or un-pinning) onto the WorkerW layer is a bigger, more
    disruptive operation than this quick action should trigger; use
    'Restart' (a full process relaunch) for changes that need that."""
    try:
        req = urllib.request.Request(f"http://localhost:{port}/refresh", method="POST")
        urllib.request.urlopen(req, timeout=30)
    except Exception as e:
        print(f"Reload failed: {e}")
        return
    window.evaluate_js("reloadAll()")

    theme = load_theme()
    if theme != load_theme_snapshot():
        win = theme.get("window", {})
        apply_rounded_corners(window, theme.get("corner_radius", 0))
        window.move(win.get("x", 100), win.get("y", 100))
    # Written every time regardless -- otherwise the snapshot only ever
    # reflects whichever reload last found a change, not "the state as of
    # the most recent reload" like its name implies.
    save_theme_snapshot(theme)


def restart_now(icon, window):
    """Tray 'Restart': a full process relaunch, for the handful of
    settings 'Reload' deliberately doesn't touch (survive_show_desktop,
    or anything else that's only ever read once at startup). Spawns a
    fresh widget.py (which starts its own new serve.py), then tears this
    one down -- same shutdown path as a normal exit (webview.start()
    returns once the window is destroyed, then main() terminates this
    process's own serve.py)."""
    icon.stop()
    subprocess.Popen(
        [sys.executable.replace("python.exe", "pythonw.exe"), "widget.py"],
        cwd=PROJECT_DIR,
    )
    window.destroy()


def make_tray_icon(port, window, theme):
    image = build_tray_image(theme.get("accent", "#0078d4"))

    def on_reload(icon, item):
        threading.Thread(target=refresh_now, args=(port, window), daemon=True).start()

    def on_restart(icon, item):
        restart_now(icon, window)

    def on_quit(icon, item):
        icon.stop()
        window.destroy()

    menu = pystray.Menu(
        pystray.MenuItem("Reload", on_reload),
        pystray.MenuItem("Restart", on_restart),
        pystray.MenuItem("Quit", on_quit),
    )
    return pystray.Icon("DesktopCards", image, "DesktopCards", menu)


def main():
    config = load_config()
    theme = load_theme()
    save_theme_snapshot(theme)  # baseline for the first Reload's diff
    port = config.get("port", 5000)
    win = theme.get("window", {})
    corner_radius = theme.get("corner_radius", 0)

    server = subprocess.Popen(
        [sys.executable.replace("python.exe", "pythonw.exe"), "serve.py"],
        cwd=PROJECT_DIR,
    )
    time.sleep(1)

    window = webview.create_window(
        WINDOW_TITLE,
        url=f"http://localhost:{port}/{PAGE}",
        width=WINDOW_WIDTH,
        height=MAX_HEIGHT,
        x=win.get("x", 100),
        y=win.get("y", 100),
        frameless=True,
        easy_drag=False,  # no drag region -- it swallowed clicks meant for
                           # the page underneath, since drag-region hit-
                           # testing runs before the page's own pointer
                           # events. Position is fixed via theme.json's
                           # window.x/y instead.
        on_top=False,
    )
    api = Api(window, MAX_HEIGHT, corner_radius)
    window.expose(api.resize_window)

    window.events.shown += lambda: hide_from_taskbar(window)
    window.events.shown += lambda: apply_rounded_corners(window, corner_radius)

    if win.get("survive_show_desktop", True):
        window.events.shown += lambda: pin_to_desktop(window)

    tray = make_tray_icon(port, window, theme)
    threading.Thread(target=tray.run, daemon=True).start()
    window.events.closing += lambda: tray.stop()

    webview.start()
    server.terminate()


if __name__ == "__main__":
    main()
