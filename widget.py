"""
Launches the pinned balance dashboard as a genuinely frameless window using
pywebview (Windows' built-in WebView2 engine) instead of a Chrome --app
window -- Chrome paints its own minimal titlebar on --app windows
regardless of native window styles, so it can't be made truly frameless.

Hosts the HTTP server on a daemon thread, opens the frameless webview
window pinned to the configured position/size, exposes a resize API the
page calls to self-fit its height, runs the interval Plaid fetch, and
(optionally) reparents the window onto the desktop's own WorkerW layer so
it survives Win+D.

Not the entry point -- desktopcards.py is. The server and the fetch both
live in this process now rather than as separate ones, so there's a single
process to launch, a single one to kill, and nothing that can outlive the
widget holding its port.

That WorkerW layer doesn't receive normal mouse input (the same reason
Rainmeter/Wallpaper Engine skins need a system-wide hook to be clickable),
so there's no way to make something inside the page itself clickable while
pinned. Manual refresh/quit instead live in a system tray icon, which is
a normal interactive window regardless of where the main widget is parented.
"""

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

import fetch_data
import logo_cache
import platform_win as native
import serve
from paths import (
    DATA_PATH,
    DETACHED,
    FROZEN,
    clean_child_env,
    APPEARANCE_PATH,
    APPEARANCE_SNAPSHOT_PATH,
    ASSET_DIR,
    CACHE_DIR,
    CONFIG_PATH,
)

# The folder the sibling scripts are spawned from. That's the asset side, not
# the data side: it's where serve.py and widget.py themselves live.
PROJECT_DIR = ASSET_DIR

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

def force_exit_soon(delay=2.5):
    """A watchdog that ends this process if a clean shutdown doesn't.

    window.destroy() is supposed to end webview.start() and let main()
    return, and from source it does. Frozen it reliably leaves the
    process alive with its window handle still registered -- pywebview's
    WinForms/pythonnet loop doesn't fully unwind -- and every such
    leftover pins a ~35MB onefile unpack directory that its bootloader
    can't clean up. Three restarts, three orphans.

    So the clean path gets a head start and this is the backstop. Nothing
    is lost by it: config.json and data.json are written synchronously
    and complete long before here, the tray is already stopped, and the
    replacement process waits out the port on its own. os._exit rather
    than sys.exit because the point is to skip an interpreter shutdown
    that is demonstrably capable of hanging."""
    def bail():
        print("[exit] clean shutdown did not finish; exiting.")
        os._exit(0)

    t = threading.Timer(delay, bail)
    t.daemon = True
    t.start()


def spawn_self(*args):
    """Starts another copy of this app, with optional subcommand flags.

    Frozen, sys.executable *is* the app and takes no script argument.
    From source it's the interpreter and needs one -- and specifically
    needs pythonw.exe, or the new process gets a console window this one
    didn't have."""
    if FROZEN:
        cmd = [sys.executable, *args]
        # NOT PROJECT_DIR. Frozen, that's ASSET_DIR, which is this
        # process's own _MEIPASS unpack directory -- and a process's
        # current directory is locked against deletion for as long as it
        # lives. Starting the replacement there means our bootloader can
        # never remove its temp folder when we exit, so it lingers
        # forever trying: one orphaned process and one undeletable ~35MB
        # directory per restart. The exe's own folder is stable and
        # outlives everybody.
        cwd = os.path.dirname(sys.executable)
    else:
        pythonw = sys.executable.replace("python.exe", "pythonw.exe")
        cmd = [pythonw, "desktopcards.py", *args]
        cwd = PROJECT_DIR  # where desktopcards.py actually is
    return subprocess.Popen(
        cmd, cwd=cwd, env=clean_child_env(), **DETACHED
    )


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
            native.window_rect(native.hwnd_of(self._window))
            if self._translucent
            else None
        )
        self._window.resize(width, height)
        native.round_corners(self._window, self._corner_radius)
        if stale is not None:
            native.erase_vacated_area(self._window, stale)


def build_tray_image(accent):
    """The tray icon: logo.png, falling back to a drawn circle.

    The circle was the original and had one real advantage -- it was
    generated from appearance.json's accent, so the tray dot always matched
    the widget. The logo doesn't track the accent, which is the cost of
    having the tray icon, the taskbar icon and the Start-menu shortcut all
    be recognisably the same mark.

    The resize to tray size is cached in cache/ by logo_cache, rebuilt
    only when logo.png is newer than the cached copy.

    The fallback is not dead code: ASSET_DIR points inside the unpacked
    bundle under a frozen build, and a spec file that forgets to include
    logo.png would otherwise leave the app with no tray icon at all -- and
    the tray is the only control surface this widget has, since the
    desktop layer it's pinned to receives no mouse input."""
    img = logo_cache.tray_image()
    if img is not None:
        return img

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
        native.round_corners(window, appearance.get("corner_radius", 0))
        window.move(win.get("x", 100), win.get("y", 100))
    # Written every time regardless -- otherwise the snapshot only ever
    # reflects whichever reload last found a change, not "the state as of
    # the most recent reload" like its name implies.
    save_appearance_snapshot(appearance)


def explain_webview_failure(exc):
    """Turns a WebView2 load failure into something actionable.

    Two different causes produce almost the same message, and the fix
    differs:

      * An incomplete unpack. A onefile exe extracts itself to a temp
        directory on every launch, and if that's interrupted -- killed
        mid-launch, overwritten while running, a temp cleaner, antivirus
        quarantining a DLL -- the interop assemblies are missing from an
        otherwise working app. Relaunching fixes it, because the next run
        extracts again.
      * A genuinely absent WebView2 runtime. It's a built-in Edge
        component on Windows 11, so this is rare, but it's the one case
        that needs a download.
    """
    message = str(exc)
    if "WebView2" not in message and "Core.dll" not in message:
        return
    print(
        "\n[webview] Could not load the WebView2 interop layer.\n"
        f"[webview] {message}\n"
        "[webview] Try launching DesktopCards again -- an interrupted\n"
        "[webview]   unpack is the usual cause and a fresh start fixes it.\n"
        "[webview] If it keeps happening, install the WebView2 runtime:\n"
        "[webview]   https://developer.microsoft.com/microsoft-edge/webview2/\n"
    )


def data_age_seconds():
    """How long ago data.json was last written, or None if it never was."""
    try:
        return time.time() - os.path.getmtime(DATA_PATH)
    except OSError:
        return None


def fetch_loop(interval_minutes, window):
    """Re-pulls from Plaid on an interval, for the life of the widget.

    This replaces the Windows Task Scheduler job that setup_schedule.py
    used to register. The job worked, but it was a separate artifact
    registered with the OS, pointing at a script path, outliving any
    particular install and firing into a widget that might not be running
    -- writing a data.json nobody was reading. The widget is already a
    long-lived process that knows the interval, so the schedule belongs
    here, where it starts and stops with the thing that displays it.

    The trade: balances only refresh while the widget runs. The catch-up
    below is what makes that acceptable -- if data.json is already older
    than one interval, because the machine was off or the widget was
    closed, this fetches immediately on launch instead of waiting out a
    full interval while showing stale numbers."""
    interval = max(1, int(interval_minutes or 90)) * 60

    age = data_age_seconds()
    wait = 0 if age is None or age >= interval else interval - age

    while True:
        time.sleep(wait)
        wait = interval
        try:
            fetch_data.main()
        except Exception as e:
            # A failed fetch is normal and temporary (no network, Plaid
            # rate limit, an item needing re-auth). Never fatal: the loop
            # has to survive to try again at the next interval.
            print(f"[fetch] scheduled refresh failed: {e}")
            continue
        try:
            window.evaluate_js("reloadAll()")
        except Exception as e:
            print(f"[fetch] refreshed, but the page would not reload: {e}")


def restart_now(icon, window):
    """Tray 'Restart': a full process relaunch, for the handful of
    settings 'Reload' deliberately doesn't touch (survive_show_desktop,
    or anything else that's only ever read once at startup).

    There's no server to shut down first any more -- it's a daemon thread
    inside this process, so it stops when the process does. That removes
    the hazard this function used to have to work around: a serve.py
    subprocess outliving the widget that spawned it, holding the port as
    an orphan and surviving every later restart.

    The replacement still has to wait out TIME_WAIT on the port, but that
    waiting now happens where it can be seen and retried, in
    serve.build_server, rather than being assumed away by a sleep."""
    icon.stop()
    # Released before spawning, not after: the replacement checks the lock
    # on startup, and this process is still alive at that moment.
    native.release_single_instance()
    spawn_self()
    window.destroy()
    force_exit_soon()


def make_tray_icon(port, window, appearance):
    image = build_tray_image(appearance.get("accent", "#0078d4"))

    def on_reload(icon, item):
        threading.Thread(target=refresh_now, args=(port, window), daemon=True).start()

    def on_restart(icon, item):
        restart_now(icon, window)

    def on_modify_accounts(icon, item):
        """Opens the setup page's management view in a browser.

        A separate process on purpose: it's a Flask app on its own port,
        and nothing about it should be able to take the widget down with
        it. The widget keeps running throughout -- the Link flow is on
        8008/8009 while the dashboard is on 5000, so there's no conflict,
        and newly linked banks appear on the next refresh.

        This is the everyday route for adding a bank. The setup exe can
        do it too, but nobody should have to go find a file in Downloads
        to add a card."""
        spawn_self("--setup")

    def on_quit(icon, item):
        icon.stop()
        window.destroy()
        # Same backstop as Restart, for the same reason -- a Quit that
        # leaves the process alive is worse, since nothing replaces it and
        # the single-instance lock then blocks the next launch.
        force_exit_soon()

    menu = pystray.Menu(
        pystray.MenuItem("Reload", on_reload),
        pystray.MenuItem("Modify Accounts", on_modify_accounts),
        pystray.MenuItem("Restart", on_restart),
        pystray.MenuItem("Quit", on_quit),
    )
    return pystray.Icon("DesktopCards", image, "DesktopCards", menu)


def main():
    # Before anything else, and before the port is touched. The app is
    # launched from a Start-menu shortcut, a Startup shortcut and setup's
    # Launch button, so a second instance is an ordinary event rather than
    # a mistake -- it should step aside quietly, not race for the port and
    # die with a traceback in a popup.
    if not native.claim_single_instance():
        print("Already running -- leaving the existing widget alone.")
        return

    # Keeps the shortcut icon in step with logo.png. The Start-menu and
    # Startup shortcuts point at this cached .ico, and nothing else
    # rebuilds it -- so without this, deleting the cache leaves them
    # pointing at a missing file and showing a generic icon. It also means
    # replacing logo.png updates those shortcuts on the next launch, which
    # is as close to live as a shell icon gets. A cache hit after the
    # first run, so it costs a hash of one PNG.
    logo_cache.icon_file()

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

    # Bound before the window exists, deliberately: if the port can't be had
    # this raises here, where it's a reportable failure, instead of letting a
    # window come up pointed at nothing -- the old silent-disappearance bug.
    serve.serve_in_thread(port)

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

    window.events.shown += lambda: native.hide_from_taskbar(window)
    window.events.shown += lambda: native.round_corners(window, corner_radius)

    if win.get("survive_show_desktop", True):
        window.events.shown += lambda: native.pin_to_desktop(window)

    tray = make_tray_icon(port, window, appearance)
    threading.Thread(target=tray.run, daemon=True).start()
    window.events.closing += lambda: tray.stop()

    # Lets another process ask this widget to restart (POST /restart) --
    # setup's "Launch widget" button, when a widget is already running.
    # Registered here rather than earlier because it closes over the tray
    # and the window, which have to exist before it can be called.
    serve.restart_hook = lambda: restart_now(tray, window)

    # Started on 'shown' rather than here: the catch-up fetch can fire
    # immediately, and it calls evaluate_js when it finishes, which needs a
    # window that actually exists. Daemon, so it can't hold the process open.
    window.events.shown += lambda: threading.Thread(
        target=fetch_loop,
        args=(config.get("fetch_interval_minutes"), window),
        daemon=True,
    ).start()

    try:
        webview.start()
    except Exception as e:
        # pywebview resolves and loads its WebView2 interop assemblies here,
        # on the way to showing the window -- the one part of a frozen build
        # most likely to fail, and the one that fails worst. Unhandled, it
        # reaches the user as a bare traceback in a popup naming a DLL,
        # which says nothing about what to do. Catch it to say something
        # useful, then re-raise so the traceback still lands in the log.
        explain_webview_failure(e)
        raise
    # No server teardown needed -- it's a daemon thread, so returning from
    # here is all it takes.


if __name__ == "__main__":
    main()
