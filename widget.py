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
import urllib.error
import urllib.request

import webview
from PIL import Image, ImageDraw
import pystray

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PROJECT_DIR, "config.json")
APPEARANCE_PATH = os.path.join(PROJECT_DIR, "appearance.json")
CACHE_DIR = os.path.join(PROJECT_DIR, "cache")
APPEARANCE_SNAPSHOT_PATH = os.path.join(CACHE_DIR, "appearance_snapshot.json")

WINDOW_TITLE = "Balances"
PAGE = "dashboard.html"

# Not config: WINDOW_WIDTH is only the size before the page's first layout
# pass corrects it (dashboard.js fitWindowToContent immediately resizes to
# the real, scale-aware width -- keep in sync with dashboard.html's #shell
# { width: ... }, the actual design-width source of truth). max_height is a
# safety ceiling (so a lot of accounts can't grow the window past a sane
# size), not a tuned preference -- the actual height always comes from the
# page measuring its own content. extra_spacing in appearance.json is what
# controls breathing room, not these.
WINDOW_WIDTH = 325
MAX_HEIGHT = 700

# This process's serve.py child. Module-level so the tray's 'Restart' can
# shut it down before spawning the replacement widget -- see restart_now.
SERVER = None


PYTHONW = sys.executable.replace("python.exe", "pythonw.exe")


def server_is_up(port):
    """Whether anything is answering on the port. An HTTP error status still
    counts -- it means a server replied, which is the question being asked."""
    try:
        urllib.request.urlopen(f"http://localhost:{port}/appearance.json", timeout=1)
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def start_server(port, attempts=6):
    """Spawns serve.py and waits until it actually answers, retrying if it
    exits on startup.

    This replaces a flat one-second sleep, which assumed the spawn always
    succeeds. It doesn't: serve.py sets allow_reuse_address = False on
    purpose, so a previous instance's listening socket sitting in TIME_WAIT
    makes the bind fail outright rather than quietly stacking two servers on
    one port. The tray's Restart hits that window regularly -- it kills its
    own server and immediately launches the replacement widget, which is
    well inside the kernel's TIME_WAIT. The old failure mode was silent and
    confusing: serve.py died, the sleep expired anyway, and the window came
    up pointed at a port with nothing behind it, so the widget simply
    disappeared with no error anywhere.

    Backs off a little further each round, since TIME_WAIT is a wait-it-out
    problem rather than one that retrying faster solves."""
    proc = None
    for attempt in range(attempts):
        proc = subprocess.Popen([PYTHONW, "serve.py"], cwd=PROJECT_DIR)
        deadline = time.time() + 3
        while time.time() < deadline:
            if proc.poll() is not None:
                break  # exited -- almost certainly the port; wait and retry
            if server_is_up(port):
                return proc
            time.sleep(0.15)
        else:
            # Still running but not answering yet. Hand it over anyway: the
            # page retries its own fetches, and killing a live server here
            # would only put us back in the TIME_WAIT hole.
            return proc
        time.sleep(0.5 * (attempt + 1))
    print(f"serve.py would not start after {attempts} attempts (port {port} busy?)")
    return proc


def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def load_appearance():
    with open(APPEARANCE_PATH, "r") as f:
        return json.load(f)


def load_appearance_snapshot():
    """appearance.json as of the end of the last reload, so refresh_now()
    can tell whether the native window actually needs touching (corner
    radius, position) instead of reapplying it unconditionally."""
    if not os.path.exists(APPEARANCE_SNAPSHOT_PATH):
        return None
    with open(APPEARANCE_SNAPSHOT_PATH, "r") as f:
        return json.load(f)


def save_appearance_snapshot(appearance):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(APPEARANCE_SNAPSHOT_PATH, "w") as f:
        json.dump(appearance, f, indent=2)


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


def window_rect(hwnd):
    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect


def erase_vacated_area(window, old_rect):
    """Repaints the desktop over the area the window used to cover.

    Only needed when the window is transparent. An opaque window's own
    background brush erases whatever it was painting over, and when it
    shrinks, Windows invalidates the uncovered region of the parent for us
    -- but a transparent window has no brush to erase with, and because
    this one is reparented onto WorkerW (the desktop's own paint layer),
    the thing that would have to redraw underneath is the desktop, which
    never notices it should. The result is the previous frame's text left
    stranded outside the window's new, shorter bounds: the widget renders
    once tall while data is loading, shrinks to fit, and the headings from
    the taller layout stay on screen as a ghost above it.

    SetWindowRgn doesn't help -- clipping the window stops *it* painting
    there, which is exactly how the stale pixels end up orphaned rather
    than overwritten.

    The old rect is in screen coordinates and RedrawWindow wants the
    parent's client coordinates, which are the same thing for a full-screen
    desktop window at the origin, but converted properly anyway rather than
    relying on that."""
    user32 = ctypes.windll.user32
    user32.GetParent.restype = ctypes.c_void_p
    user32.GetParent.argtypes = [ctypes.c_void_p]

    hwnd = ctypes.c_void_p(window.native.Handle.ToInt64())
    parent = user32.GetParent(hwnd)
    if not parent:
        return  # never pinned (survive_show_desktop off) -- DWM handles it

    origin = ctypes.wintypes.POINT(old_rect.left, old_rect.top)
    user32.ScreenToClient(ctypes.c_void_p(parent), ctypes.byref(origin))
    stale = ctypes.wintypes.RECT(
        origin.x,
        origin.y,
        origin.x + (old_rect.right - old_rect.left),
        origin.y + (old_rect.bottom - old_rect.top),
    )

    RDW_INVALIDATE, RDW_ERASE = 0x0001, 0x0004
    RDW_ALLCHILDREN, RDW_UPDATENOW = 0x0080, 0x0100
    user32.RedrawWindow(
        ctypes.c_void_p(parent),
        ctypes.byref(stale),
        None,
        RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN | RDW_UPDATENOW,
    )


class Api:
    def __init__(self, window, max_height, corner_radius, translucent=False):
        self._window = window
        self._max_height = max_height
        self._corner_radius = corner_radius
        self._translucent = translucent

    def resize_window(self, width, height):
        # Width has no cap (only height does, as a safety ceiling) -- a
        # scale above 1 is expected to grow the window as wide as it needs.
        width = max(1, int(width))
        height = max(1, min(int(height), self._max_height))
        # Captured before the resize, because afterwards there's no record of
        # how much of the desktop this window was covering.
        stale = (
            window_rect(ctypes.c_void_p(self._window.native.Handle.ToInt64()))
            if self._translucent
            else None
        )
        self._window.resize(width, height)
        apply_rounded_corners(self._window, self._corner_radius)
        if stale is not None:
            erase_vacated_area(self._window, stale)


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
    accent color appearance.json has."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, size - 4, size - 4), fill=accent)
    return img


def refresh_now(port, window):
    """Tray 'Reload': always re-pulls from Plaid (serve.py's /refresh
    re-reads config.json fresh and re-runs fetch_data.main() in-process --
    no skipping that, by design) and always tells the page to re-fetch
    data.json + appearance.json (reloadAll() -- cheap either way, since
    it's just a local fetch + a few CSS variable writes). The native-window
    side (corner radius clip, position) is the part worth gating: those are
    real OS calls, not just local JS, so they only run when
    appearance.json actually differs from the last reload's snapshot -- the
    common case (hit Reload just to see fresh balances, nothing about the
    appearance changed) skips them entirely instead of redoing a no-op
    every time.
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

    appearance = load_appearance()
    if appearance != load_appearance_snapshot():
        win = appearance.get("window", {})
        apply_rounded_corners(window, appearance.get("corner_radius", 0))
        window.move(win.get("x", 100), win.get("y", 100))
    # Written every time regardless -- otherwise the snapshot only ever
    # reflects whichever reload last found a change, not "the state as of
    # the most recent reload" like its name implies.
    save_appearance_snapshot(appearance)


def restart_now(icon, window):
    """Tray 'Restart': a full process relaunch, for the handful of
    settings 'Reload' deliberately doesn't touch (survive_show_desktop,
    or anything else that's only ever read once at startup).

    Our own serve.py is terminated here, before the replacement widget is
    spawned, rather than being left to main()'s post-webview.start()
    cleanup. Spawning first raced the new process against this one for the
    port: serve.py sets allow_reuse_address = False (deliberately, so a
    second instance can't silently bind on top of a live one), so the new
    serve.py died on startup and the new widget silently kept talking to
    the old process -- which then outlived the widget that owned it as an
    orphan holding the port, surviving every later restart."""
    icon.stop()
    if SERVER is not None:
        SERVER.terminate()
        try:
            SERVER.wait(timeout=5)
        except subprocess.TimeoutExpired:
            SERVER.kill()
    subprocess.Popen([PYTHONW, "widget.py"], cwd=PROJECT_DIR)
    window.destroy()


def make_tray_icon(port, window, appearance):
    image = build_tray_image(appearance.get("accent", "#0078d4"))

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
    global SERVER
    config = load_config()
    appearance = load_appearance()
    save_appearance_snapshot(appearance)  # baseline for the first Reload's diff
    port = config.get("port", 5000)
    win = appearance.get("window", {})
    corner_radius = appearance.get("corner_radius", 0)
    # The window's own fill, shown for the moment before the page paints and
    # in the sliver between body's rounded box and the window's (clipped)
    # square one. pywebview's default is white, which flashes as a bright
    # frame on every launch and Restart under the dark theme. These two
    # values are deliberately the same as --surface-0 in each theme
    # (dashboard.html), so there's no visible seam where one ends.
    page_background = "#1d2128" if appearance.get("dark_theme") else "#f3f4f6"
    # appearance.json's "translucent_background". The page's own rgba colors
    # can only ever blend against whatever the *window* paints, so without
    # this flag a translucent card would just reveal page_background above
    # and nothing else -- the wallpaper is on the other side of the window,
    # not the other side of the card. transparent=True instead sets
    # WebView2's DefaultBackgroundColor to Color.Transparent (pywebview's
    # winforms backend), which makes the control composite with real
    # per-pixel alpha against what's behind it. Since the window is
    # reparented onto WorkerW, what's behind it is the desktop wallpaper.
    #
    # It also means background_color below is ignored: pywebview's backend
    # picks one or the other, never both. That's the intended outcome rather
    # than a loss -- page_background exists to stop a white frame flashing
    # before the page paints, and a window with no fill has nothing to flash.
    # An opacity of 1 counts as off, matching applyTranslucency in
    # dashboard.js -- and this is the half of that check worth having. A
    # transparent window is composited per-pixel against the desktop for
    # its whole lifetime, whether or not anything in the page is actually
    # see-through, so a fully opaque "translucent" widget would pay that
    # cost continuously to look exactly like the cheap one. The two files
    # have to agree, or the page would blend into a window that isn't
    # transparent (grey behind the cards) or vice versa.
    translucent = (
        bool(appearance.get("translucent_background"))
        and float(appearance.get("opacity", 1.0)) < 0.998
    )

    SERVER = start_server(port)

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
                           # events. Position is fixed via appearance.json's
                           # window.x/y instead.
        on_top=False,
        background_color=page_background,
        transparent=translucent,
    )
    api = Api(window, MAX_HEIGHT, corner_radius, translucent)
    window.expose(api.resize_window)

    window.events.shown += lambda: hide_from_taskbar(window)
    window.events.shown += lambda: apply_rounded_corners(window, corner_radius)

    if win.get("survive_show_desktop", True):
        window.events.shown += lambda: pin_to_desktop(window)

    tray = make_tray_icon(port, window, appearance)
    threading.Thread(target=tray.run, daemon=True).start()
    window.events.closing += lambda: tray.stop()

    webview.start()
    # Already torn down if we got here via the tray's 'Restart'.
    if SERVER.poll() is None:
        SERVER.terminate()


if __name__ == "__main__":
    main()
