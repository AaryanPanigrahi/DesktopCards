"""
Where everything lives, in one place.

Running from source, there's no distinction worth making: the code, the
config and the cache all sit in the repo folder together. A frozen build
splits them hard, and the split is not optional:

  ASSET_DIR  read-only, ships with the app -- dashboard.html, dashboard.js,
             logo.png. Under PyInstaller these are extracted to a temp
             directory (sys._MEIPASS) that is DELETED when the process
             exits. Nothing writable can live here; anything written would
             be gone by the next launch.

  DATA_DIR   writable, per-user, survives upgrades and the exe being moved
             -- config.json (Plaid secret + access tokens), appearance.json,
             data.json, cache/.

So every module imports its paths from here instead of deriving them from
__file__, which would land in the temp directory under a frozen build and
make every launch look like a fresh install.

Running from source both point at the repo folder, which is deliberate:
an existing checkout with a live config.json keeps working with no
migration step and no copied files.
"""

import os
import subprocess
import sys

FROZEN = getattr(sys, "frozen", False)

# _MEIPASS only exists under a PyInstaller onefile build; onedir and source
# runs both fall back to this file's own folder.
ASSET_DIR = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))

if FROZEN:
    # LOCALAPPDATA rather than a folder beside the exe: the exe may well end
    # up somewhere unwritable (Program Files) or get moved, and neither
    # should cost someone their linked accounts.
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    DATA_DIR = os.path.join(base, "DesktopCards")
else:
    DATA_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
APPEARANCE_PATH = os.path.join(DATA_DIR, "appearance.json")
DATA_PATH = os.path.join(DATA_DIR, "data.json")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
LOGOS_DIR = os.path.join(CACHE_DIR, "logos")
CONFIG_SNAPSHOT_PATH = os.path.join(CACHE_DIR, "config_snapshot.json")
APPEARANCE_SNAPSHOT_PATH = os.path.join(CACHE_DIR, "appearance_snapshot.json")

# Bundled, read-only.
DASHBOARD_PATH = os.path.join(ASSET_DIR, "dashboard.html")
BUNDLED_LOGO_PATH = os.path.join(ASSET_DIR, "logo.png")

# A logo dropped in DATA_DIR wins over the bundled one. From source these
# are the same file, so editing logo.png in the repo just works. Frozen,
# the bundled copy is sealed inside the exe, and this is what makes the
# mark changeable without a rebuild: drop a logo.png next to config.json.
USER_LOGO_PATH = os.path.join(DATA_DIR, "logo.png")


def logo_path():
    """The logo to use, preferring a user-supplied one.

    Resolved on each call rather than at import, so replacing the file
    takes effect without restarting anything that re-reads it."""
    if USER_LOGO_PATH != BUNDLED_LOGO_PATH and os.path.exists(USER_LOGO_PATH):
        return USER_LOGO_PATH
    return BUNDLED_LOGO_PATH


# Popen keyword arguments for starting a process that must outlive this
# one. Without this the replacement inherits our standard handles and
# stays tied to our console/process group, and under a onefile build that
# keeps the old bootloader process alive after its own child has exited --
# an orphan holding a ~35MB unpack directory, one per restart.
#
# DETACHED_PROCESS gives it no console (it's a windowed app and shouldn't
# borrow ours), CREATE_NEW_PROCESS_GROUP keeps a Ctrl-C aimed at us from
# reaching it, and close_fds stops handle inheritance.
DETACHED = {
    "close_fds": True,
    "creationflags": (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    ),
}


def clean_child_env():
    """The environment to hand a spawned copy of a frozen app.

    Lives here, with FROZEN, because both widget.py and setup_app.py
    launch independent copies of the app and both need this.

    PyInstaller's onefile bootloader tells child processes where it
    unpacked itself, through environment variables (_PYI_* on current
    versions, _MEIPASS2 historically). That's right for a child that's
    part of the same run -- it saves re-extracting tens of megabytes --
    and badly wrong for a replacement process meant to outlive us: the
    child skips extraction, uses our temp directory, and then we exit and
    the bootloader deletes it. The new process is left running against a
    directory being erased, and the first thing to go missing is the
    WebView2 interop layer, so it dies with 'Cannot find
    Microsoft.Web.WebView2.Core.dll' -- a message that points nowhere
    near the actual cause.

    Stripping them makes the replacement unpack its own copy, which is
    what an independent instance needs. Returns None when not frozen, to
    inherit the environment unchanged."""
    if not FROZEN:
        return None
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("_PYI") or key.startswith("_MEIPASS"):
            del env[key]
    return env


def ensure_data_dir():
    """Create DATA_DIR if it isn't there yet. A no-op from source, where it's
    the repo folder, and the first thing a frozen first run needs."""
    os.makedirs(DATA_DIR, exist_ok=True)
    return DATA_DIR
