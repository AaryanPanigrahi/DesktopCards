"""
Removes DesktopCards: shortcuts, the installed copy, and itself.

    DesktopCards.exe --uninstall      from anywhere
    Uninstall.exe                     the installed copy, by name

Reached three ways, all the same code: the file the install step leaves
beside the app, the Uninstall button in Settings > Apps, and the flag.

The order matters more than it looks. The widget is asked to quit before
anything is deleted, because it holds DesktopCards.exe open as a running
image and writes config.json on its way out -- removing files under a
live widget would leave the exe undeletable and let it rewrite a file
that was just removed. The uninstaller's own exe goes last and from
outside the process, since Windows will not delete a running one.

Nothing here needs administrator. Everything the app installs is
per-user: shortcuts in %APPDATA%, the app and its data under
%LOCALAPPDATA%, and the Add/Remove entry in HKCU.
"""

import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request

from paths import (
    APPEARANCE_PATH,
    CACHE_DIR,
    CONFIG_PATH,
    DATA_DIR,
    DATA_PATH,
    FROZEN,
    LOOPBACK,
    SETUP_PORT,
    USER_LOGO_PATH,
)

APP_NAME = "DesktopCards"
LOG_PATH = os.path.join(DATA_DIR, "desktopcards.log")

# What the tick box protects. config.json carries the Plaid credentials
# and one access token per linked bank, appearance.json the look, and
# logo.png a replaced mark -- between them, everything a reinstall would
# otherwise make someone set up again. Deliberately not data.json: it is
# the last fetch's balances, it is regenerated within a minute of the
# next launch, and stale balances are the one thing nobody wants kept.
KEEPABLE = (CONFIG_PATH, APPEARANCE_PATH, USER_LOGO_PATH)


def native():
    """platform_win, imported late for the same reason setup_app does
    it: this module stays importable on a machine it cannot run on."""
    import platform_win

    return platform_win


def installed_exe_path():
    return os.path.join(DATA_DIR, f"{APP_NAME}.exe")


def uninstall_exe_path():
    return os.path.join(DATA_DIR, "Uninstall.exe")


def dashboard_port():
    """The widget's port, from config.json, defaulting the way the rest
    of the app does. Read before anything is deleted."""
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f).get("port", 5000)
    except (OSError, ValueError):
        return 5000


def _post(url, timeout=5):
    req = urllib.request.Request(url, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return False
    except Exception:
        return False


def stop_running_app(port):
    """Asks the widget and any setup server to quit, then makes sure.

    Asked first, not killed first: the widget's own quit path stops the
    tray icon, releases the single-instance lock and lets its pending
    writes finish, where a kill leaves a stale lock behind that would
    block the next install.

    taskkill is the backstop, and only for a widget that is still
    answering after being asked. Both names are covered because the
    installed copy and the setup copy are the same binary under
    different names, and either can be the one holding the folder."""
    asked = []
    if _post(f"http://{LOOPBACK}:{port}/quit"):
        asked.append("widget")
    # Setup's server, which is a separate process on its own port and
    # has held this page open for whoever clicked uninstall from it.
    if _post(f"http://{LOOPBACK}:{SETUP_PORT}/api/quit"):
        asked.append("setup")
    if asked:
        print(f"[uninstall] asked {' and '.join(asked)} to quit")
        # Long enough for force_exit_soon's 2.5s backstop to fire, which
        # is the slowest of the widget's two exit paths.
        time.sleep(3.5)

    if not widget_answers(port):
        return
    print("[uninstall] still running; killing it")
    import subprocess

    for name in (f"{APP_NAME}.exe", f"{APP_NAME}-Setup.exe"):
        subprocess.run(
            ["taskkill", "/f", "/im", name],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    time.sleep(1)


def widget_answers(port):
    """Whether something is still serving the dashboard. Same test
    setup_app.widget_is_running uses, and for the same reason: the
    server is a thread inside the widget, so an answer means a live
    process."""
    try:
        urllib.request.urlopen(
            f"http://{LOOPBACK}:{port}/appearance.json", timeout=1
        )
        return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def remove_shortcuts():
    """The Start-menu entry and the Startup one. Both are files."""
    n = native()
    gone = []
    for path in (os.path.join(n.start_menu_dir(), f"{APP_NAME}.lnk"),
                 n.autostart_path(APP_NAME)):
        if os.path.exists(path) and n.remove_shortcut(path):
            gone.append(os.path.basename(path))
    print(f"[uninstall] shortcuts removed: {gone or 'none found'}")
    return gone


def remove_data(keep_settings):
    """Everything under DATA_DIR except the installed exes, which have
    to outlive this process by a few seconds.

    Returns the files that were kept, so the closing message can say so
    rather than guess."""
    kept = []
    for path in (DATA_PATH, LOG_PATH):
        _unlink(path)
    if os.path.isdir(CACHE_DIR):
        shutil.rmtree(CACHE_DIR, ignore_errors=True)
        print(f"[uninstall] removed {CACHE_DIR}")
    for path in KEEPABLE:
        if keep_settings:
            if os.path.exists(path):
                kept.append(os.path.basename(path))
        else:
            _unlink(path)
    return kept


def _unlink(path):
    try:
        os.remove(path)
        print(f"[uninstall] removed {path}")
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        print(f"[uninstall] could not remove {path}: {e}")
        return False


def main():
    if os.name != "nt":
        sys.exit("Uninstalling is Windows-only.")

    n = native()
    port = dashboard_port()
    banks = _linked_bank_count()

    # Said plainly and before anything happens, because this is the only
    # chance to say it: what goes, and what the tick box is protecting.
    yes, keep_settings = n.confirm_with_checkbox(
        "Uninstall DesktopCards?",
        "This closes the widget, removes it and its shortcuts, and takes "
        "DesktopCards out of Settings > Apps."
        + (f"\n\n{banks} linked bank{'' if banks == 1 else 's'} stay linked "
           "at Plaid either way – uninstalling never touches your bank."
           if banks else ""),
        "Keep my settings and linked accounts",
        checked=True,
    )
    if not yes:
        print("[uninstall] cancelled")
        return 0

    print(f"[uninstall] starting; keep_settings={keep_settings}")
    stop_running_app(port)
    remove_shortcuts()
    n.unregister_uninstaller(APP_NAME)
    kept = remove_data(keep_settings)

    # The two exes last, and from outside: this process *is* one of them
    # when run as the installed Uninstall.exe. DATA_DIR goes too, but
    # only through a plain rmdir -- it refuses a folder that still has
    # the kept settings in it, which is exactly the right answer.
    me = os.path.abspath(sys.executable if FROZEN else __file__)
    doomed = [p for p in (installed_exe_path(), uninstall_exe_path())
              if os.path.exists(p)]
    if FROZEN:
        n.delete_after_exit(doomed or [me], directories=[DATA_DIR])
    else:
        # From source there is nothing installed to delete, and deleting
        # the checkout would be absurd. Shortcuts, registry and data are
        # real and have already been dealt with.
        print("[uninstall] running from source; no installed files to remove")

    n.message_box(
        "DesktopCards has been removed."
        + (f"\n\nKept: {', '.join(kept)}\nin {DATA_DIR}" if kept else ""),
        "DesktopCards",
    )
    return 0


def _linked_bank_count():
    """How many banks the config knows about, for the prompt. Zero on
    anything unreadable: the dialog is not the place to explain that
    config.json is malformed."""
    try:
        with open(CONFIG_PATH, "r") as f:
            return len(json.load(f).get("items", []))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


if __name__ == "__main__":
    sys.exit(main())
