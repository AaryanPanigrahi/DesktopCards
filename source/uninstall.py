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

import datetime
import json
import glob
import os
import shutil
import sys
import time
import urllib.error
import urllib.request

from paths import (
    APPEARANCE_PATH,
    BACKUPS_DIR,
    CACHE_DIR,
    CONFIG_PATH,
    DATA_DIR,
    DATA_PATH,
    FROZEN,
    LOOPBACK,
    MERGED_LAYOUT_PATH,
    SECRETS_PATH,
    SETUP_PORT,
    USER_LOGO_PATH,
)

APP_NAME = "DesktopCards"
LOG_PATH = os.path.join(DATA_DIR, "desktopcards.log")

# What the tick box protects: everything a reinstall would otherwise make
# someone set up again. config.json carries the profiles and one access
# token per linked bank, secrets.json every client id and secret,
# appearance.json the look, the layout files the account order, and
# logo.png a replaced mark.
#
# secrets.json is the one that must not be forgotten here. Without it
# config.json still lists every linked bank but nothing can read them, so
# "keep my settings" would quietly cost someone their Plaid Items -- and
# on a Trial plan those do not come back.
#
# Deliberately not data.json or data.<profile>.json: those are the last
# fetch's balances, regenerated within a minute of the next launch, and
# stale balances are the one thing nobody wants kept.
#
# Resolved on each call rather than fixed at import, because the
# per-profile layout files are discovered on disk rather than known up
# front.
def keepable_paths():
    found = [
        CONFIG_PATH,
        SECRETS_PATH,
        APPEARANCE_PATH,
        MERGED_LAYOUT_PATH,
        USER_LOGO_PATH,
    ]
    found.extend(sorted(glob.glob(os.path.join(DATA_DIR, "layout.*.json"))))
    return list(dict.fromkeys(found))


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
    """The Start-menu entry, the Startup one and the desktop one. All
    three are files, and all three are optional -- only the ones that are
    actually there get removed."""
    n = native()
    gone = []
    for path in (n.start_menu_path(APP_NAME),
                 n.autostart_path(APP_NAME),
                 n.desktop_path(APP_NAME)):
        if os.path.exists(path) and n.remove_shortcut(path):
            gone.append(os.path.basename(path))
    print(f"[uninstall] shortcuts removed: {gone or 'none found'}")
    return gone


def remove_data(keep_settings, keep_backup):
    """Everything under DATA_DIR except the installed exes, which have
    to outlive this process by a few seconds.

    Returns the files that were kept, so the closing message can say so
    rather than guess."""
    kept = []
    for path in (DATA_PATH, LOG_PATH):
        _unlink(path)
    # The per-profile balance caches go with data.json, for the same
    # reason: they are a fetch away from being rebuilt, and keeping them
    # would only ever show someone numbers from before they uninstalled.
    for path in sorted(glob.glob(os.path.join(DATA_DIR, "data.*.json"))):
        _unlink(path)
    if os.path.isdir(CACHE_DIR):
        shutil.rmtree(CACHE_DIR, ignore_errors=True)
        print(f"[uninstall] removed {CACHE_DIR}")
    for path in keepable_paths():
        if keep_settings:
            if os.path.exists(path):
                kept.append(os.path.basename(path))
        else:
            _unlink(path)
    # Backups hold secrets and live access tokens, so leaving them behind
    # after someone declined one would be the opposite of what they asked
    # for. keep_backup is their answer, not an inference from
    # keep_settings: someone can want their settings gone and still want
    # a way back.
    if not keep_backup and os.path.isdir(BACKUPS_DIR):
        shutil.rmtree(BACKUPS_DIR, ignore_errors=True)
        print(f"[uninstall] removed {BACKUPS_DIR}")
    elif keep_backup and os.path.isdir(BACKUPS_DIR):
        kept.append("backups")
    return kept


def _take_backups():
    """One backup file per profile, taken before anything is removed.

    Best effort: a backup is a courtesy here, and failing to write one is
    not a reason to leave a half-uninstalled app behind."""
    try:
        import profiles

        config, secrets_store = profiles.read_store()
        taken = 0
        for pid, profile in profiles.profiles(config).items():
            creds = secrets_store.get(pid) or {}
            if not creds.get("client_id"):
                continue
            profiles.write_backup(
                {
                    "kind": "manual",
                    "taken": datetime.datetime.now().isoformat(timespec="seconds"),
                    "profile_id": pid,
                    "label": profiles.label({**profile, **creds}),
                    "profile": profile,
                    "credentials": creds,
                },
                "before-uninstall-" + profiles.label({**profile, **creds}),
            )
            taken += 1
        return taken
    except Exception as e:
        print(f"[uninstall] could not write a backup: {e}")
        return 0


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

    # Asked only when the data is actually going: with settings kept there
    # is nothing to restore from, because nothing was lost.
    keep_backup = True
    if not keep_settings:
        asked, ticked = n.confirm_with_checkbox(
            "Save a backup first?",
            "A backup keeps your Plaid credentials and your linked banks "
            "in one file, so reinstalling can pick them up without "
            "re-linking anything.\n\n"
            f"It is left in {BACKUPS_DIR}, and it holds your secret and "
            "your bank tokens – delete it when you are done with it.",
            "Keep a backup",
            checked=True,
        )
        # Both, so that No and an unticked box agree: either way the
        # answer was no.
        keep_backup = bool(asked and ticked)

    print(f"[uninstall] starting; keep_settings={keep_settings}, "
          f"keep_backup={keep_backup}")
    # Before the backup, not after: the widget fetches on a timer and
    # writes config.json back when it does, so a backup taken while it
    # was still running could be a snapshot of a file that changed a
    # second later -- and the copy left behind would be the stale one.
    stop_running_app(port)
    if keep_backup and not keep_settings:
        taken = _take_backups()
        print(f"[uninstall] backed up {taken} profile(s)")
    remove_shortcuts()
    n.unregister_uninstaller(APP_NAME)
    kept = remove_data(keep_settings, keep_backup)

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
    config.json is malformed.

    Every profile, not just the enabled ones -- they all stay linked at
    Plaid, which is the thing the prompt is reassuring about. Read
    without migrating: an uninstall has no business rewriting files it is
    about to delete. The flat key is the pre-profiles shape, still read
    here because an install that never launched after updating has it."""
    try:
        with open(CONFIG_PATH, "r") as f:
            config = json.load(f)
        total = len(config.get("items") or [])
        for profile in (config.get("profiles") or {}).values():
            total += len((profile or {}).get("items") or [])
        return total
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


if __name__ == "__main__":
    sys.exit(main())
