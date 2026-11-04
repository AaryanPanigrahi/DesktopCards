"""
First-run setup, and the place to change a DesktopCards install later.

Runs in the browser rather than a terminal. That isn't a style choice:
the packaged build is windowed, so it has no console to prompt in, and the
Plaid Link flow has to happen in a browser anyway because that's where
Plaid hosts it. Given one of the steps is already a web page, all of them
may as well be.

What it does, in order:

  1. seeds DATA_DIR (appearance.json from the example)
  2. takes Plaid credentials, validates them against the real API before
     writing anything
  3. runs Plaid Link so you log into your bank directly
  4. does one real fetch, so a broken setup fails here rather than
     silently producing an empty widget
  5. installs the app and its shortcuts
  6. offers to launch it

Run it again later and it skips to a management view instead: add a bank,
remove one, or redo the shortcuts. Adding accounts is also reachable from
the widget's own tray menu, which is the easier route once it's running.

Served on localhost only. Credentials go from the form straight into
config.json in DATA_DIR and are never echoed back to the page.
"""

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

from flask import Flask, jsonify, request

import net_guard
import plaid_link_setup as link
import profiles
from paths import (
    APPEARANCE_EXAMPLE_PATH,
    APPEARANCE_PATH,
    ASSET_DIR,
    CONFIG_EXAMPLE_PATH,
    CONFIG_SNAPSHOT_PATH,
    DATA_DIR,
    DATA_PATH,
    DETACHED,
    FROZEN,
    LOOPBACK,
    SETUP_PORT,  # 5000 is the dashboard, 8008 is standalone --link
    clean_child_env,
    ensure_data_dir,
    profile_data_path,
    profile_layout_path,
    profile_snapshot_path,
)

APP_NAME = "DesktopCards"

# How this process knows it still has a reader.
#
# It's a server behind a browser tab, and a tab can be closed, reloaded,
# navigated away from or killed with the whole browser -- none of which
# reach the process. Left alone it would sit there forever, and because
# every tray "Modify" click used to start another one, "forever" meant one
# orphan per click.
#
# Three signals, because no single one covers every way a tab can go:
#
#   HEARTBEAT   the page pings /api/alive on a timer while it's open
#   CLOSE       pagehide fires a beacon at /api/closing as the tab goes
#   IDLE        nothing has spoken to us at all for a long time
#
# CLOSE_GRACE is what makes a reload survive: an unload fires the same
# beacon a close does, so instead of exiting on it we arm a short timer,
# and the reloaded page's very first request disarms it. Long enough for a
# localhost round trip, far too short to notice.
#
# IDLE_GRACE is the backstop for a browser that was killed outright, where
# no beacon is ever sent. It's minutes rather than seconds because browsers
# throttle timers in background tabs hard -- Chrome can drop a hidden tab
# to one wake per minute, and a page the user merely switched away from
# must not look the same as one that's gone.
HEARTBEAT_SECONDS = 25
CLOSE_GRACE = 4.0
IDLE_GRACE = 300.0

shutdown_event = threading.Event()
app = Flask(__name__)

_last_seen = time.time()
_closing_at = None


@app.before_request
def only_this_machine():
    """Refuses anything that isn't this machine's own browser.

    Registered before note_activity on purpose: a refused request must not
    count as evidence that somebody still has the page open, or a hostile
    site could keep this server alive indefinitely. See net_guard."""
    reason = net_guard.rejection(
        SETUP_PORT,
        method=request.method,
        host=request.host,
        origin=request.headers.get("Origin"),
        fetch_site=request.headers.get("Sec-Fetch-Site"),
        fetch_mode=request.headers.get("Sec-Fetch-Mode"),
    )
    if reason:
        print(f"[setup] refused {request.method} {request.path}: {reason}")
        return jsonify(ok=False, error="Refused: not a local request."), 403


@app.before_request
def note_activity():
    """Any request at all means somebody still has this page open."""
    global _last_seen, _closing_at
    _last_seen = time.time()
    # Everything except the beacon itself cancels a pending shutdown. This
    # is what distinguishes a reload from a close: the reloaded page's
    # first request lands here inside CLOSE_GRACE and calls it off.
    if request.path != "/api/closing":
        _closing_at = None


# ----------------------------------------------------------------- helpers

def native():
    """platform_win, imported late so this module stays importable
    elsewhere (and so a future platform_mac swap has one place to go)."""
    import platform_win

    return platform_win


def installed_exe_path():
    return os.path.join(DATA_DIR, f"{APP_NAME}.exe")


def uninstall_exe_path():
    """The uninstaller, beside the app it removes.

    A third copy of the same binary, like the setup one: the filename is
    what selects the mode (see desktopcards.looks_like_uninstall_build),
    so there is nothing to build separately and nothing that can fall out
    of step with the app it is uninstalling."""
    return os.path.join(DATA_DIR, "Uninstall.exe")


def launch_target():
    """What a shortcut should point at, with what arguments, and its icon.

    Frozen, that's the installed copy of the exe with no arguments.
    From source there's nothing to install, so it's pythonw.exe plus the
    entry script -- which still gives a working Start-menu shortcut for a
    developer checkout.

    The icon is the cached .ico built from the current logo.png, which is
    what lets a replaced logo reach the Start-menu shortcut without
    rebuilding the exe. Falls back to the target's own embedded icon."""
    import logo_cache

    icon = logo_cache.icon_file()
    if FROZEN:
        return installed_exe_path(), "", icon
    pythonw = sys.executable.replace("python.exe", "pythonw.exe")
    return pythonw, "desktopcards.py", icon


def dashboard_port():
    return (read_config() or {}).get("port", 5000)


def widget_is_running():
    """Whether a widget is up, judged by its server answering.

    The server is a thread inside the widget process, so an answer on
    that port means a live widget -- and it's a cheap, cross-process
    check that needs no handle to anything."""
    try:
        urllib.request.urlopen(
            f"http://{LOOPBACK}:{dashboard_port()}/appearance.json", timeout=1
        )
        return True
    except urllib.error.HTTPError:
        return True  # something replied, which is the question
    except Exception:
        return False


def ask_widget_to_restart():
    """Asks the running widget to relaunch itself. True if it accepted."""
    req = urllib.request.Request(
        f"http://{LOOPBACK}:{dashboard_port()}/restart", method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r).get("ok", False)
    except Exception as e:
        print(f"[setup] restart request failed: {e}")
        return False


def read_config():
    """The raw config.json, migrating a pre-profiles install first.

    Still the file as it sits on disk, so the callers that only want a
    shared setting (port, fetch_interval_minutes) are unchanged. Anything
    wanting credentials asks profiles.active() instead -- those live in
    secrets.json now and are never in this dict."""
    config = profiles.ensure_migrated()
    return config or None


def seed_data_dir():
    """Puts the files the app needs into DATA_DIR if they aren't there.

    A frozen first run has no repo folder to copy an example out of, so
    the examples ship inside the bundle. Never overwrites: this runs again
    on every setup, and clobbering someone's tuned appearance.json would
    be a nasty surprise."""
    ensure_data_dir()
    if not os.path.exists(APPEARANCE_PATH):
        example = APPEARANCE_EXAMPLE_PATH
        if os.path.exists(example):
            shutil.copy2(example, APPEARANCE_PATH)
            print(f"seeded {APPEARANCE_PATH}")


def config_skeleton(client_id, secret, environment):
    """The credentials to verify, plus the shared defaults from the
    shipped example so port and fetch_interval_minutes come along rather
    than being re-invented here.

    Not a config.json any more: profiles own the credentials, and
    api_credentials takes only the shared keys off this.
    """
    example = CONFIG_EXAMPLE_PATH
    config = {}
    if os.path.exists(example):
        try:
            with open(example, "r") as f:
                config = json.load(f)
        except (OSError, ValueError):
            config = {}
    config.update({
        "client_id": client_id.strip(),
        "secret": secret.strip(),
        "environment": environment,
    })
    config.setdefault("port", 5000)
    config.setdefault("fetch_interval_minutes", 90)
    # Deliberately no items/items_created: those belong to a profile now,
    # and leaving them here would have the example's placeholder profile
    # look like real linked banks.
    config.pop("profiles", None)
    config.pop("_comment", None)
    return config


# Plaid's Trial plan allows this many Production Items over the lifetime of
# a team. Not enforced here -- a paid plan lifts it and we have no way to
# tell which plan a client_id is on -- so this only drives what the page
# tells the user about how much of a free tier they have spent.
TRIAL_ITEM_CAP = 10


def items_created(profile):
    """How many Plaid Items this profile has ever created.

    Delegates to profiles.items_created so the counter has one
    implementation: fetch_data and the link flow read it too, and two
    copies of the len(items) fallback would eventually disagree."""
    return profiles.items_created(profile)


def item_summary(config):
    return [
        {
            "item_id": it.get("item_id"),
            "institution_name": it.get("institution_name", "Unknown"),
        }
        for it in (config or {}).get("items", [])
    ]


# The sections the dashboard draws, in the order it draws them. Keyed by
# Plaid account type, same as dashboard.js's SECTION_TYPES, because the
# type is what decides which section an account lands in.
SECTION_TYPES = ("depository", "credit")
SECTION_FALLBACK_TITLES = {"depository": "Checking & Savings", "credit": "Credit Cards"}

# "other" is everything the dashboard doesn't draw as its own section --
# loans, investments and anything Plaid adds later. It can be hidden but
# not ordered or retitled, so it carries a hidden list and nothing else,
# and api_layout still refuses it.
OTHER_SECTION = "other"
HIDEABLE_SECTIONS = SECTION_TYPES + (OTHER_SECTION,)


def clear_fetched_data():
    """Forget everything the last fetch produced.

    Written empty rather than deleted: the dashboard and this page both
    read data.json and treat a missing file the same as a failed read, so
    an explicit empty list is the difference between "no accounts" and
    "something went wrong". The snapshot goes entirely -- it only exists
    to diff against the next run, and a diff against banks that are no
    longer linked would suppress the logo fetch for whatever gets linked
    next."""
    try:
        ensure_data_dir()
        with open(DATA_PATH, "w") as f:
            json.dump(
                {
                    "last_updated": datetime.datetime.now().isoformat(),
                    "accounts": [],
                    "errors": [],
                },
                f,
                indent=2,
            )
    except OSError as e:
        print(f"[setup] could not clear {DATA_PATH}: {e}", file=sys.stderr)
    try:
        os.remove(CONFIG_SNAPSHOT_PATH)
    except FileNotFoundError:
        pass
    except OSError as e:
        print(f"[setup] could not clear snapshot: {e}", file=sys.stderr)


def read_data():
    """data.json, or an empty dict before the first fetch has run."""
    try:
        with open(DATA_PATH, "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def order_key(account, namespaced=True):
    """The "Bank Name,last4" string the layout order uses.

    Must match dashboard.js's orderKey exactly, or a hand-written order
    silently matches nothing and every card falls through to the appended
    tail. Mask-less accounts fall back to name, same as there.

    The profile id is always prefixed, however many profiles are
    enabled: the same bank linked in two profiles produces the same
    "Bank,last4", so a plain key would move or hide both cards. Keeping
    the shape constant also stops the keys changing when a profile is
    switched on or off, which used to discard a hand-sorted order. Pass
    namespaced=False only for the un-prefixed form, which nothing writes
    any more and only the widget still reads, as a fallback."""
    mask = account.get("mask")
    institution = account.get("institution_name", "Unknown")
    if mask:
        key = f"{institution},{mask}"
    else:
        key = f"{institution} {account.get('name', '')}"
    if namespaced and account.get("profile"):
        return profiles.layout_key(account["profile"], key)
    return key


def balance_digits(account):
    """Digits before the decimal point, matching dashboard.js's intDigits.

    Has to agree with it, since this is what decides whether the Layout
    tab's badge says a card breaks onto its own row and the widget is the
    thing that actually does the breaking."""
    try:
        value = abs(float(account.get("balance") or 0))
    except (TypeError, ValueError):
        return 1
    return len(str(int(value)))


def account_summary():
    """The linked accounts, grouped the way the dashboard groups them.

    From data.json rather than config.json: config.json knows which banks
    are linked, but only a fetch knows which individual checking/savings
    and credit accounts came back from them.

    No balances. This page opens in an ordinary browser tab, and a tab
    showing real account balances reads as a banking site -- which this
    isn't, and shouldn't look like. The identity of an account (bank, type,
    last four) is all this view needs to let someone order and hide
    things; the numbers belong on the widget."""
    accounts = read_data().get("accounts", [])
    layout = read_appearance().get("layout", {}) or {}

    def entry(account, section):
        hidden = set((layout.get(section) or {}).get("hidden", []) or [])
        key = order_key(account)
        return {
            "institution_name": account.get("institution_name", "Unknown"),
            "name": account.get("name", ""),
            "mask": account.get("mask"),
            "key": key,
            "hidden": key in hidden,
            # Set by fetch_data.py only when more than one profile is
            # enabled, which is exactly when two accounts in this list can
            # otherwise be indistinguishable: the same bank linked in two
            # profiles reads as one bank twice.
            "profile_label": account.get("profile_label"),
            # How many digits the balance has, never the balance. The Layout
            # tab needs it to say whether a card breaks onto its own row at
            # the current threshold, and it has to be a count rather than a
            # computed verdict so the badges can update live as that
            # threshold is dragged around. A digit count is not a balance and
            # is never rendered as money.
            "digits": balance_digits(account),
        }

    groups = [
        {
            "type": section,
            "accounts": [entry(a, section) for a in accounts
                         if a.get("type") == section],
        }
        for section in SECTION_TYPES
    ]
    other = [a for a in accounts if a.get("type") not in SECTION_TYPES]
    if other:
        # Hideable but not orderable: the dashboard draws these under a
        # plain "Other" heading in fetch order, so there's nothing to sort
        # or retitle, but switching off a mortgage you never wanted on the
        # widget is the whole point of this list.
        groups.append({
            "type": OTHER_SECTION,
            "accounts": [entry(a, OTHER_SECTION) for a in other],
        })
    return groups


def default_appearance():
    """appearance.example.json, which is already the shipped default.

    Reusing it rather than restating the defaults in Python keeps "Restore
    to Default" honest: there's one file that defines what a fresh install
    looks like, and it's the one a fresh install actually gets seeded
    from."""
    try:
        with open(APPEARANCE_EXAMPLE_PATH, "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def read_appearance():
    try:
        with open(APPEARANCE_PATH, "r") as f:
            appearance = json.load(f)
    except (OSError, ValueError):
        appearance = default_appearance()
    # Layout belongs to a profile, appearance does not: the order key is
    # "Bank,last4", which collides the moment the same bank is linked in
    # two profiles, while colours and fonts are how the widget looks
    # whatever is enabled. So it lives in layout.<profile>.json and is
    # grafted on here, then split back out by write_appearance(). That
    # keeps every call site that reads appearance["layout"] working
    # unchanged, which is most of this file.
    # One shared file with profile-prefixed keys, whatever is enabled.
    # The call sites below see the shape they saw when this lived in
    # appearance.json; only the keys inside the order and hidden lists
    # carry a prefix now.
    if profiles.profiles():
        # Merged, not replaced: the section titles and the row-break
        # thresholds never moved out of appearance.json, so handing back
        # the saved block alone reset the titles to their defaults the
        # first time a profile existed.
        appearance["layout"] = profiles.layout_with_settings(
            appearance.get("layout", {}), profiles.shared_layout()
        )
    return appearance


def sync_layout_order(appearance):
    """Rewrite each section's order list with the accounts that exist.

    The shipped example ships placeholder keys ("Chase,0000"), which match
    nothing on a real dashboard -- so someone opening appearance.json to
    tweak the order found a list that named none of their banks and no way
    to learn the right strings. Filling it with the real keys makes the
    file self-documenting: the order you see is the order being used.

    Keys already present keep their position, so a hand-sorted order
    survives; new accounts are appended and departed ones dropped.

    A key belonging to a profile that is not currently enabled is kept
    where it is rather than dropped. It names a card that exists and is
    simply not being drawn, and discarding it is what used to rebuild the
    whole order the first time a second profile was switched on."""
    layout = appearance.setdefault("layout", {})
    accounts = read_data().get("accounts", [])
    drawing = {prof["id"] for prof in profiles.enabled()}

    def belongs_to_a_drawing_profile(key):
        pid = (key or "").split(":", 1)[0]
        return pid in drawing

    for section in SECTION_TYPES:
        block = layout.setdefault(section, {})
        block.setdefault("title", SECTION_FALLBACK_TITLES[section])
        keys = [order_key(a) for a in accounts if a.get("type") == section]
        kept = [
            k for k in block.get("order", [])
            if k in keys or not belongs_to_a_drawing_profile(k)
        ]
        block["order"] = kept + [k for k in keys if k not in kept]
        # Deliberately NOT pruned to what's present, unlike order. A bank
        # that errors on one fetch drops out of data.json, and pruning here
        # would quietly unhide an account someone had switched off -- the
        # worse of the two failures. Stale keys cost nothing but a line.
        block.setdefault("hidden", [])
    # "other" gets a hidden list and nothing else -- no title or order,
    # because the dashboard draws it under a fixed heading in fetch order
    # and api_layout refuses the section. Created unconditionally so the
    # key is there to write into the first time something is hidden.
    layout.setdefault(OTHER_SECTION, {}).setdefault("hidden", [])
    return appearance


def write_appearance(appearance):
    """The other half of the seam in read_appearance(): layout goes to the
    profile's own file, everything else to appearance.json.

    With no profile yet -- first run, before any credentials -- layout
    stays in appearance.json, because there is nothing to key it by. It
    is adopted into the shared file the first time a profile exists."""
    ensure_data_dir()
    payload = dict(appearance)
    layout = payload.pop("layout", None) if "layout" in payload else None
    if layout is not None:
        if profiles.profiles():
            profiles.write_merged_layout(_merge_saved_order(layout))
        else:
            # First run, before any credentials: nothing to key a layout
            # file by, so it stays in appearance.json until there is.
            payload["layout"] = layout
    # Through profiles._write_json for the temp-file-and-replace that
    # every other file in this app gets. A plain open() here truncated
    # appearance.json if anything went wrong mid-write.
    profiles._write_json(APPEARANCE_PATH, payload)


def _merge_saved_order(layout):
    """A saved layout folded into the one on disk.

    The page only lists accounts from enabled profiles, so what comes
    back names a subset of the order. Writing it as-is would delete every
    entry belonging to a profile that is switched off. Each of those is
    anchored to the entry in front of it and travels with it, so a card
    moved across a switched-off one leaves it at the bottom of the
    move."""
    on_disk = profiles.read_merged_layout()
    accounts = read_data().get("accounts", [])
    out = {}
    for section, block in (layout or {}).items():
        # "_comment" and anything else that is not a section block.
        if not isinstance(block, dict):
            out[section] = block
            continue
        block = dict(block)
        drawable = [order_key(a) for a in accounts if a.get("type") == section]
        # The file on disk gets the same treatment: it is hand-editable,
        # and a non-section value there is not this function's business.
        previous = on_disk.get(section)
        previous = dict(previous) if isinstance(previous, dict) else {}
        block["order"] = profiles.merge_order(
            previous.get("order") or [], block.get("order") or [], drawable
        )
        # Hidden is a union, not a replacement, for the same reason: the
        # page cannot un-hide what it was never shown.
        was_hidden = [
            k for k in (previous.get("hidden") or [])
            if k not in drawable
        ]
        block["hidden"] = list(
            dict.fromkeys(list(block.get("hidden") or []) + was_hidden)
        )
        out[section] = block
    return out


# ------------------------------------------------------------------ routes

# Anchored and deliberately narrow: this value goes straight into an HTML
# attribute, and appearance.json is a hand-edited file.
_HEX_COLOR = re.compile(r"\A#[0-9a-fA-F]{3,8}\Z")

# appearance.json's "shadow_ui" block, which this page reads for its own
# controls the way the widget reads "shadow" for its cards. Deliberately
# absent from APPEARANCE_FIELDS: the manage view styles itself from this,
# and a control that restyles the control you are using is a toy rather
# than a setting -- so it is edited in the file and takes effect on reload.
SHADOW_UI_DEFAULTS = {
    "enabled": True,
    "color": None,
    "offset": {"tabs": 3, "inputs": 3, "checkboxes": 2, "rows": 3, "buttons": 3},
}

# Each group of controls carries its own distance, because they are not the
# same size: 3px under a full-width text box is a hairline, and the same
# 3px under a 1.05rem checkbox is a third of the control. Named per group
# rather than per element, so a new control joins a group instead of
# needing a key of its own.
_UI_SHADOW_PARTS = {
    "tabs": "--ui-shadow-tab",
    "inputs": "--ui-shadow-input",
    "checkboxes": "--ui-shadow-check",
    "rows": "--ui-shadow-row",
    "buttons": "--ui-shadow-btn",
}

# Far enough to read as a drawn edge, not far enough to collide with the
# next control. A hand-edited file gets no validation elsewhere, so the
# clamp is the validation.
_UI_SHADOW_MAX = 10


def shadow_ui_css():
    """The inline custom properties the page's shadows read, or "".

    Off, or every distance 0, returns nothing at all rather than a set of
    0px values: the stylesheet already defaults each var to 0, and every
    shadow rule is inert at that length, so absent and off are the same
    page.

    "offset" takes either a block of per-group distances or one number for
    all of them, so the shorter form keeps working and is still the easy
    thing to write when the whole page wants dialling up or down."""
    block = read_appearance().get("shadow_ui")
    if not isinstance(block, dict):
        block = {}
    block = {**SHADOW_UI_DEFAULTS, **block}
    if not block.get("enabled"):
        return ""
    offset = block.get("offset")
    if not isinstance(offset, dict):
        offset = {part: offset for part in _UI_SHADOW_PARTS}
    props = []
    for part, var in _UI_SHADOW_PARTS.items():
        raw = offset.get(part, SHADOW_UI_DEFAULTS["offset"][part])
        try:
            px = min(_UI_SHADOW_MAX, max(0, int(raw)))
        except (TypeError, ValueError):
            continue
        if px:
            props.append(f"{var}:{px}px")
    if not props:
        return ""
    color = str(block.get("color") or "")
    # Anything else falls through to the stylesheet's per-theme default,
    # which is the only safe answer for a value that lands in an attribute.
    if _HEX_COLOR.match(color):
        props.append(f"--ui-shadow-color:{color}")
    return ";".join(props)


@app.route("/")
def index():
    """The page, with the theme already in the markup.

    Server-side rather than left to the script, because anything the script
    does happens after the first paint: the page came up in the light
    palette and flipped to dark a moment later, a visible flash on every
    open. It's the same attribute applyPageTheme sets, so nothing
    downstream needs to know which of the two put it there.

    The accent is an inline custom property, which outranks any selector,
    so it holds in both palettes the way the widget's own accent does."""
    appearance = read_appearance()
    theme = "dark" if appearance.get("dark_theme") else "light"
    accent = str(appearance.get("accent") or "")
    props = [f"--accent:{accent}"] if _HEX_COLOR.match(accent) else []
    props += [p for p in (shadow_ui_css(),) if p]
    style = f' style="{";".join(props)}"' if props else ""
    return PAGE.replace(
        '<html lang="en">',
        f'<html lang="en" data-theme="{theme}"{style}>',
        1,
    )


@app.route("/api/state")
def api_state():
    """What the page needs to decide which view to show.

    Deliberately carries no client_id or secret -- the page never needs
    them back, and not sending them means they can't leak into a browser
    cache or a screenshot."""
    config = profiles.read_config()
    secrets_store = profiles.read_secrets()
    active = profiles.active(config, secrets_store)
    _, _, _icon = launch_target()
    return jsonify(
        configured=bool(active),
        environment=(active or {}).get("environment"),
        items=item_summary(active),
        items_created=items_created(active),
        # What this profile's Plaid team has actually spent, which is not
        # the same number once two production profiles share a client id:
        # the Trial cap belongs to the team, so their counts add up. Sent
        # alongside rather than instead of items_created so the page can
        # show "4 here, 6 on this team" when they differ.
        trial_items_used=(
            profiles.trial_items_used(config, secrets_store, active["client_id"])
            if active else 0
        ),
        trial_item_cap=TRIAL_ITEM_CAP,
        # The whole profile list comes down with the state, so the page
        # renders the Advanced section without a second round trip.
        profiles=[
            profile_view(pid, config, secrets_store)
            for pid in profiles.profiles(config)
        ],
        backups=profiles.list_backups(),
        frozen=FROZEN,
        data_dir=DATA_DIR,
        installed=os.path.exists(installed_exe_path()) if FROZEN else True,
        autostart=os.path.exists(native().autostart_path(APP_NAME)),
        start_menu=os.path.exists(native().start_menu_path(APP_NAME)),
        desktop=os.path.exists(native().desktop_path(APP_NAME)),
        shortcut_dirs={
            "startup": native().startup_dir(),
            "start_menu": native().start_menu_dir(),
            "desktop": native().desktop_dir(),
        },
        running=widget_is_running(),
        groups=account_summary(),
        # Served rather than hardcoded in the page, so the interval and the
        # deadline it has to beat can't drift apart.
        heartbeat_seconds=HEARTBEAT_SECONDS,
        # So this page can wear the same theme and accent as the widget it
        # configures. Safe to send: appearance.json holds no secrets, which
        # is the whole reason it's a separate file from config.json.
        **{k: read_appearance().get(k) for k in ("dark_theme", "accent")},
    )


@app.route("/api/appearance")
def api_appearance():
    """appearance.json as the editor should show it, plus the defaults.

    The order lists are synced and written back here rather than only on
    save: opening this view is the moment someone is looking for the real
    "Bank Name,last4" keys, and leaving the shipped placeholders on disk
    until they happen to press Save is what made the layout untweakable in
    the first place. Idempotent, so reopening the tab rewrites nothing."""
    appearance = read_appearance()
    before = json.dumps(appearance, sort_keys=True)
    sync_layout_order(appearance)
    if json.dumps(appearance, sort_keys=True) != before:
        write_appearance(appearance)
    return jsonify(
        ok=True,
        appearance=appearance,
        defaults=sync_layout_order(default_appearance()),
        path=APPEARANCE_PATH,
    )


@app.route("/api/visibility", methods=["POST"])
def api_visibility():
    """Switch one account on or off on the widget.

    Its own endpoint rather than a field in the appearance editor: this is
    a per-account toggle in the Accounts tab, and making it post the whole
    appearance object would mean that tab had to load and hold the entire
    file just to tick a box."""
    body = request.get_json(silent=True) or {}
    key = body.get("key")
    section = body.get("type")
    if not key or section not in HIDEABLE_SECTIONS:
        return jsonify(ok=False, error="Need an account key and a known section."), 400

    appearance = read_appearance()
    sync_layout_order(appearance)
    block = appearance["layout"][section]
    hidden = [k for k in block.get("hidden", []) if k != key]
    if not body.get("visible"):
        hidden.append(key)
    block["hidden"] = hidden
    write_appearance(appearance)
    print(f"{'showing' if body.get('visible') else 'hiding'} {key}")
    return jsonify(ok=True, hidden=hidden)


@app.route("/api/appearance", methods=["POST"])
def api_appearance_save():
    body = request.get_json(silent=True) or {}
    appearance = body.get("appearance")
    if not isinstance(appearance, dict):
        return jsonify(ok=False, error="Expected an appearance object."), 400
    # Clamped here as well as in dashboard.js: the page is one way in, but
    # the floor exists because a 0 leaves an invisible widget pinned to the
    # desktop layer with nothing clickable to undo it, and that's worth
    # enforcing wherever the value is written.
    try:
        opacity = float(appearance.get("opacity", 1))
    except (TypeError, ValueError):
        opacity = 1.0
    appearance["opacity"] = min(1.0, max(0.15, opacity))
    # The Layout tab owns layout; this editor has no fields for it. Taking
    # the on-disk copy rather than the posted one means a reorder or a
    # hidden toggle made while the Appearance tab sat open can't be
    # clobbered by a stale object being saved back over it.
    appearance["layout"] = read_appearance().get("layout", {})
    # Same reasoning for the file-only shadow block: the editor has no field
    # for it, so the object posted back carries whatever was on disk when
    # the tab loaded, and that would overwrite an edit made since.
    # Same for the two other file-only blocks. "colors" is the widget's
    # palette and this page has no field for it either; without this a
    # save would write back whatever copy the tab loaded with, which is
    # how a hand-edited colour would silently revert.
    for file_only in ("shadow_ui", "colors"):
        on_disk = read_appearance().get(file_only)
        if on_disk is None:
            appearance.pop(file_only, None)
        else:
            appearance[file_only] = on_disk
    sync_layout_order(appearance)
    write_appearance(appearance)
    print(f"wrote {APPEARANCE_PATH}")
    return jsonify(ok=True, appearance=appearance)


# What the Layout tab is allowed to set, by name, so a patch can't write
# arbitrary keys into the layout block.
LAYOUT_FIELDS = ("title", "order", "new_row_digit_threshold",
                 "new_row_name_length_threshold")


@app.route("/api/layout", methods=["POST"])
def api_layout():
    """Patch one section's layout: its heading, row rules, or card order.

    One endpoint for the whole tab rather than one per control, and a patch
    rather than a whole-file write, so a drop that reorders cards can't
    also carry a stale copy of everything else in appearance.json."""
    body = request.get_json(silent=True) or {}
    section = body.get("type")
    patch = body.get("patch")
    if section not in SECTION_TYPES or not isinstance(patch, dict):
        return jsonify(ok=False, error="Need a known section and a patch object."), 400

    unknown = set(patch) - set(LAYOUT_FIELDS)
    if unknown:
        return jsonify(ok=False, error=f"Not layout fields: {sorted(unknown)}"), 400

    appearance = read_appearance()
    sync_layout_order(appearance)
    block = appearance["layout"][section]

    if "order" in patch:
        order = patch["order"]
        if not isinstance(order, list) or not all(isinstance(k, str) for k in order):
            return jsonify(ok=False, error="order must be a list of keys."), 400
        # Reordering can only permute what the page was actually shown,
        # which is the accounts of the enabled profiles. Anything else is
        # a stale page posting keys for accounts that have since gone.
        # Compared against those rather than the stored order, because the
        # stored order also holds entries for switched-off profiles that
        # the page never saw and cannot send back; write_appearance folds
        # those back in.
        drawable = [
            order_key(a) for a in read_data().get("accounts", [])
            if a.get("type") == section
        ]
        if set(order) != set(drawable):
            return jsonify(
                ok=False,
                error="That order doesn't match the accounts on file. "
                      "Reload the page and try again.",
            ), 409
        block["order"] = profiles.merge_order(
            block.get("order", []), order, drawable)

    for field in ("title", "new_row_digit_threshold", "new_row_name_length_threshold"):
        if field not in patch:
            continue
        if field == "title":
            block["title"] = str(patch["title"])[:60]
        else:
            try:
                # Floors chosen so a threshold can't be set to something that
                # makes every card break out (or none ever could).
                block[field] = max(1, int(patch[field]))
            except (TypeError, ValueError):
                return jsonify(ok=False, error=f"{field} must be a number."), 400

    write_appearance(appearance)
    print(f"patched layout.{section}: {sorted(patch)}")
    return jsonify(ok=True, layout=appearance["layout"])


@app.route("/api/credentials", methods=["POST"])
def api_credentials():
    body = request.get_json(silent=True) or {}
    client_id = (body.get("client_id") or "").strip()
    secret = (body.get("secret") or "").strip()
    environment = (body.get("environment") or "production").strip()

    # Blank means "keep what's saved". The Advanced form never receives the
    # stored credentials back (they'd end up in a browser cache), so
    # requiring both would make switching environment alone impossible
    # without retyping a secret from the Plaid dashboard. First-run setup
    # has nothing saved to fall back on, so it still fails as before.
    saved = profiles.active() or {}
    client_id = client_id or (saved.get("client_id") or "")
    secret = secret or (saved.get("secret") or "")

    if not client_id or not secret:
        return jsonify(ok=False, error="Both the client ID and the secret are required."), 400

    candidate = config_skeleton(client_id, secret, environment)

    # Validate against the real API before writing anything. Creating a
    # link token is the cheapest call that actually proves the pair works,
    # and getting this wrong is the single most likely setup mistake --
    # far better to fail here, with the fields still on screen, than to
    # save bad credentials and fail later looking like a broken app.
    try:
        link.new_link_token(link.build_client(candidate))
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    except Exception as e:
        return jsonify(
            ok=False,
            error=f"Plaid rejected those credentials ({_plaid_message(e)}). "
                  f"Check the client ID, the secret, and that the secret "
                  f"matches the {environment} environment.",
        ), 400

    seed_data_dir()
    config = profiles.ensure_migrated()
    secrets_store = profiles.read_secrets()

    # Which profile these credentials belong to. The same one when the
    # client_id and environment both match, because that is a rotated
    # secret and the linked banks must survive it -- an access token is
    # tied to the client_id, not the secret, so it keeps working.
    #
    # Any other pair is a different Plaid team or a different
    # environment, which is a different profile rather than a destructive
    # edit to this one. That is what removes the old warning: nothing has
    # to be unlinked to change credentials any more, because the old
    # profile and its banks simply stay where they are.
    target = None
    for pid, profile in profiles.profiles(config).items():
        creds = secrets_store.get(pid) or {}
        if (
            creds.get("client_id") == client_id
            and profile.get("environment") == environment
        ):
            target = pid
            break

    if target is None:
        target = profiles.new_id(config)
        config.setdefault("profiles", {})[target] = {
            "environment": environment,
            "nickname": None,
            "enabled": True,
            "items": [],
            "items_created": 0,
        }
    else:
        config["profiles"][target]["enabled"] = True

    # This endpoint is the first-run wizard and nothing else -- the manage
    # view's profile list has its own endpoints -- so the profile it just
    # saved is the one being set up and has to end up enabled. Saving step
    # 1 twice (correcting a typo in the environment, say) would otherwise
    # leave the wizard linking banks into the first attempt.
    #
    # Switching the others off is free here: a first run has nothing
    # linked yet, and it keeps the sandbox-runs-alone rule true no matter
    # which way step 1 is filled in.
    for other_id, other in profiles.profiles(config).items():
        if other_id != target:
            other["enabled"] = False

    # Shared settings came from the example via config_skeleton. Seeded
    # here rather than written wholesale, so a first run still gets a
    # port and an interval while an existing install keeps the ones it
    # has.
    for key in ("port", "fetch_interval_minutes"):
        if key in candidate:
            config.setdefault(key, candidate[key])
    config.setdefault("retired_profile_ids", [])

    secrets_store[target] = {"client_id": client_id, "secret": secret}
    # secrets.json first, for the reason given in profiles.migrate(): a
    # config naming a profile whose secret never landed is recoverable by
    # saving again, while the reverse leaves a secret for a profile that
    # does not exist.
    profiles.write_secrets(secrets_store)
    profiles.write_config(config)
    print(f"wrote credentials for profile {target} ({environment})")
    return jsonify(ok=True)


def _plaid_message(exc):
    """The useful sentence out of a Plaid ApiException, which otherwise
    stringifies as a wall of headers and a JSON body."""
    body = getattr(exc, "body", None)
    if body:
        try:
            parsed = json.loads(body)
            return parsed.get("error_message") or parsed.get("error_code") or str(exc)
        except (ValueError, TypeError):
            pass
    return str(exc).splitlines()[0][:200]


# --------------------------------------------------------------- profiles

def profile_view(pid, config, secrets_store):
    """One profile as the page needs it.

    Carries the last 4 of the client ID and never the client ID itself or
    the secret, for the same reason api_state does not: the page has no
    use for them, and not sending them means they cannot end up in a
    browser cache or a screenshot."""
    profile = profiles.profiles(config).get(pid) or {}
    creds = secrets_store.get(pid) or {}
    client_id = creds.get("client_id") or ""
    with_creds = {**profile, "client_id": client_id}
    sandbox = profile.get("environment") == "sandbox"
    return {
        "id": pid,
        "label": profiles.label(with_creds),
        "auto_label": profiles.auto_label(with_creds),
        "nickname": profile.get("nickname"),
        "environment": profile.get("environment"),
        "client_id_tail": profiles.client_id_tail(client_id),
        "has_credentials": bool(client_id and creds.get("secret")),
        "enabled": bool(profile.get("enabled")),
        "items": item_summary(profile),
        "items_created": items_created(profile),
        # The team's spend, which is what the cap actually applies to.
        # Zero for sandbox: those Items are free and must never look like
        # they have eaten into a Trial allowance.
        "trial_items_used": (
            0 if sandbox
            else profiles.trial_items_used(config, secrets_store, client_id)
        ),
    }


def profiles_payload():
    config, secrets_store = profiles.read_store()
    return {
        "profiles": [
            profile_view(pid, config, secrets_store)
            for pid in profiles.profiles(config)
        ],
        "trial_item_cap": TRIAL_ITEM_CAP,
        "trial_items_used": _active_trial_items_used(config, secrets_store),
        "backups": profiles.list_backups(),
    }


def _active_trial_items_used(config, secrets_store):
    """Team-wide Items spent for the active profile's client ID, or 0.

    The same number api_state sends, pulled out so the profiles
    payload can refresh it after an unlink without a full page load.
    """
    active = profiles.active(config, secrets_store)
    if not active or not active.get("client_id"):
        return 0
    return profiles.trial_items_used(config, secrets_store, active["client_id"])


def verify_credentials(client_id, secret, environment):
    """Returns an error message, or None when Plaid accepts the pair.

    Creating a link token is the cheapest call that proves credentials
    work, and it is the same check first-run setup makes."""
    try:
        link.new_link_token(
            link.build_client({
                "client_id": client_id,
                "secret": secret,
                "environment": environment,
            })
        )
    except ValueError as e:
        return str(e)
    except Exception as e:
        return (
            f"Plaid rejected those credentials ({_plaid_message(e)}). Check "
            f"the client ID, the secret, and that the secret matches the "
            f"{environment} environment."
        )
    return None


# How long to wait for a running fetch before giving up on a change that
# would alter what gets fetched. Long enough for a real run of a few
# banks, short enough that the page is not left hanging.
FETCH_WAIT_SECONDS = 45


def wait_for_fetch():
    """Block while a fetch is in flight.

    Enabling or disabling a profile changes what the next write to
    data.json should contain, and a fetch that started before the change
    would finish afterwards and write the old set's numbers over the new
    ones. Waiting is the simplest correct answer: the alternative is
    cancelling work that is already paid for in Plaid calls."""
    import fetch_data

    deadline = time.time() + FETCH_WAIT_SECONDS
    while fetch_data.fetch_in_progress():
        if time.time() > deadline:
            return False
        time.sleep(0.5)
    return True


def refresh_in_background():
    """Restart the widget onto the new profile set, then refetch.

    Backgrounded because the page should not sit waiting on Plaid: the
    switch itself is already done and saved by the time this starts."""
    def work():
        ask_widget_to_restart()
        try:
            import fetch_data

            fetch_data.main()
        except (Exception, SystemExit) as e:
            print(f"[setup] refresh after profile change failed: {e}")

    threading.Thread(target=work, daemon=True).start()


def backup_profile(config, secrets_store, pid, kind):
    """A restore point for one profile, taken before anything
    destructive.

    Includes the secret and every access token on purpose: nothing here
    ever calls /item/remove, so the Item outlives its local record and a
    saved token still works. That is what makes a restore able to recover
    a bank without spending another Plaid Item."""
    profile = profiles.profiles(config).get(pid) or {}
    creds = secrets_store.get(pid) or {}
    label = profiles.label({**profile, "client_id": creds.get("client_id")})
    return profiles.write_backup(
        {
            "kind": kind,
            "taken": datetime.datetime.now().isoformat(timespec="seconds"),
            "profile_id": pid,
            "label": label,
            "profile": profile,
            "credentials": creds,
        },
        f"{kind}-{label}",
    )


@app.route("/api/profiles")
def api_profiles():
    return jsonify(ok=True, **profiles_payload())


@app.route("/api/profiles", methods=["POST"])
def api_profile_add():
    body = request.get_json(silent=True) or {}
    client_id = (body.get("client_id") or "").strip()
    secret = (body.get("secret") or "").strip()
    environment = (body.get("environment") or "production").strip()
    nickname = (body.get("nickname") or "").strip() or None

    if not client_id or not secret:
        return jsonify(ok=False, error="Both the client ID and the secret are required."), 400
    if environment not in profiles.ENVIRONMENTS:
        return jsonify(ok=False, error=f"Unknown environment {environment!r}."), 400

    config, secrets_store = profiles.read_store()
    for pid, profile in profiles.profiles(config).items():
        creds = secrets_store.get(pid) or {}
        if (
            creds.get("client_id") == client_id
            and profile.get("environment") == environment
        ):
            return jsonify(
                ok=False,
                error=f"That client ID already has a {environment} profile "
                      f"({profiles.label({**profile, 'client_id': client_id})}).",
            ), 400

    error = verify_credentials(client_id, secret, environment)
    if error:
        return jsonify(ok=False, error=error), 400

    seed_data_dir()
    pid = profiles.new_id(config)
    config.setdefault("profiles", {})[pid] = {
        "environment": environment,
        "nickname": nickname,
        # Added switched off. Enabling changes what the widget draws, and
        # that should be a separate deliberate click rather than a side
        # effect of saving a secret.
        "enabled": False,
        "items": [],
        "items_created": 0,
    }
    config.setdefault("retired_profile_ids", [])
    secrets_store[pid] = {"client_id": client_id, "secret": secret}
    profiles.write_secrets(secrets_store)
    profiles.write_config(config)
    print(f"[setup] added profile {pid} ({environment})")
    return jsonify(ok=True, id=pid, **profiles_payload())


@app.route("/api/profiles/<pid>/credentials", methods=["POST"])
def api_profile_credentials(pid):
    """Edit a profile in place. Never unlinks its banks.

    An access token is tied to the client ID, not the secret, so a
    rotated secret keeps working against the same banks. Changing the
    client ID to a different team is refused rather than silently
    orphaning them -- that is a new profile, which is what Add is for."""
    body = request.get_json(silent=True) or {}
    config, secrets_store = profiles.read_store()
    profile = profiles.profiles(config).get(pid)
    if not profile:
        return jsonify(ok=False, error="No such profile."), 404

    creds = secrets_store.get(pid) or {}
    client_id = (body.get("client_id") or "").strip() or (creds.get("client_id") or "")
    secret = (body.get("secret") or "").strip() or (creds.get("secret") or "")
    if not client_id or not secret:
        return jsonify(ok=False, error="Both the client ID and the secret are required."), 400

    if (
        profile.get("items")
        and creds.get("client_id")
        and client_id != creds.get("client_id")
    ):
        return jsonify(
            ok=False,
            error="That profile has linked banks, and their access tokens "
                  "belong to the current client ID. Add a profile for the "
                  "other client ID instead.",
        ), 400

    error = verify_credentials(client_id, secret, profile.get("environment"))
    if error:
        return jsonify(ok=False, error=error), 400

    if "nickname" in body:
        profile["nickname"] = (body.get("nickname") or "").strip() or None
    secrets_store[pid] = {"client_id": client_id, "secret": secret}
    profiles.write_secrets(secrets_store)
    profiles.write_config(config)
    return jsonify(ok=True, **profiles_payload())


@app.route("/api/profiles/<pid>/enabled", methods=["POST"])
def api_profile_enabled(pid):
    body = request.get_json(silent=True) or {}
    want = bool(body.get("enabled"))
    config, secrets_store = profiles.read_store()
    profile = profiles.profiles(config).get(pid)
    if not profile:
        return jsonify(ok=False, error="No such profile."), 404

    if want:
        creds = secrets_store.get(pid) or {}
        if not creds.get("client_id") or not creds.get("secret"):
            return jsonify(
                ok=False,
                error="That profile has no saved credentials yet.",
            ), 400
        # Verified before enabling, so the widget never restarts into a
        # profile that cannot fetch. force=true is the page coming back
        # after asking whether to enable it anyway.
        if not body.get("force"):
            error = verify_credentials(
                creds["client_id"], creds["secret"], profile.get("environment")
            )
            if error:
                return jsonify(ok=False, error=error, confirmable=True), 400

    if not wait_for_fetch():
        return jsonify(
            ok=False,
            error="A refresh is still running. Try again in a moment.",
        ), 409

    profile["enabled"] = want
    disabled = []
    if want:
        # Sandbox runs alone. Invented balances summing into a real total
        # is a mistake that is hard to notice afterwards, so the rule
        # enforces itself here rather than being reported as an error
        # after the fact.
        sandbox = profile.get("environment") == "sandbox"
        for other_id, other in profiles.profiles(config).items():
            if other_id == pid or not other.get("enabled"):
                continue
            other_sandbox = other.get("environment") == "sandbox"
            if sandbox or other_sandbox:
                other["enabled"] = False
                disabled.append(other_id)

    profiles.write_config(config)
    refresh_in_background()
    return jsonify(ok=True, disabled=disabled, **profiles_payload())


@app.route("/api/profiles/<pid>/unlink", methods=["POST"])
def api_profile_unlink(pid):
    """Drop a profile's linked banks, leaving the profile itself.

    Local only: nothing is sent to Plaid, so this costs no Items and
    frees none either. On a production profile the Items it spent stay
    spent, which is why the page says so before asking."""
    config, secrets_store = profiles.read_store()
    profile = profiles.profiles(config).get(pid)
    if not profile:
        return jsonify(ok=False, error="No such profile."), 404

    backup = backup_profile(config, secrets_store, pid, "before-unlink")
    before = len(profile.get("items") or [])
    profile["items"] = []
    profiles.write_config(config)
    clear_profile_data(pid)
    # The merged data.json is what the widget actually draws, and
    # clear_profile_data only drops this profile's own cache. Without
    # this the unlinked banks stay on the desktop until the next
    # scheduled fetch, which can be an hour and a half away.
    clear_fetched_data()
    if profile.get("enabled"):
        refresh_in_background()
    print(f"[setup] unlinked {before} banks from profile {pid}; backup at {backup}")
    return jsonify(ok=True, **profiles_payload())


@app.route("/api/profiles/<pid>/reset", methods=["POST"])
def api_profile_reset(pid):
    """Sandbox only: back to a clean slate for testing.

    Clears the test link count as well as the banks, which production
    deliberately cannot do -- there the count records Items that are
    genuinely spent, and zeroing it would be lying about the allowance.
    Sandbox Items are free, so there is nothing to lie about."""
    config, secrets_store = profiles.read_store()
    profile = profiles.profiles(config).get(pid)
    if not profile:
        return jsonify(ok=False, error="No such profile."), 404
    if profile.get("environment") != "sandbox":
        return jsonify(
            ok=False,
            error="Only a sandbox profile can be reset. Its Items are free; "
                  "production Items stay spent whatever this app forgets.",
        ), 400

    backup = backup_profile(config, secrets_store, pid, "before-reset")
    profile["items"] = []
    profile["items_created"] = 0
    profiles.write_config(config)
    clear_profile_data(pid)
    clear_fetched_data()
    if profile.get("enabled"):
        refresh_in_background()
    print(f"[setup] reset sandbox profile {pid}; backup at {backup}")
    return jsonify(ok=True, **profiles_payload())


@app.route("/api/profiles/<pid>/delete", methods=["POST"])
def api_profile_delete(pid):
    config, secrets_store = profiles.read_store()
    profile = profiles.profiles(config).get(pid)
    if not profile:
        return jsonify(ok=False, error="No such profile."), 404

    backup = backup_profile(config, secrets_store, pid, "before-delete")
    was_enabled = bool(profile.get("enabled"))

    del config["profiles"][pid]
    # Retired before the files go, so a crash in between cannot leave the
    # id reusable while its data is already gone.
    profiles.retire_id(config, pid)
    secrets_store.pop(pid, None)
    # Config first here, unlike migrate() and restore: this is the
    # opposite direction. Dropping the secret first would leave a profile
    # still listed in config.json with its credentials already gone -- a
    # zombie that shows in the list, never resolves, and can only be
    # fixed by typing the secret back in. An orphaned secret, the other
    # way round, is harmless and is cleaned up by the next write.
    profiles.write_config(config)
    profiles.write_secrets(secrets_store)
    clear_profile_data(pid)
    # The shared layout keeps the keys of profiles that are switched off,
    # on purpose. A deleted one is not switched off: its id is retired, so
    # its keys can never match an account again and nothing would ever
    # clear them.
    profiles.drop_layout_keys(pid)
    for path in (profile_layout_path(pid), profile_snapshot_path(pid)):
        try:
            os.remove(path)
        except OSError:
            pass
    print(f"[setup] deleted profile {pid}; backup at {backup}")
    if was_enabled:
        # Cleared, not just refreshed: with no enabled profile left the
        # fetch exits before writing data.json, deliberately, so a
        # refresh on its own would leave the deleted profile's balances
        # on the widget with nothing left to explain them.
        clear_fetched_data()
        refresh_in_background()
    return jsonify(ok=True, **profiles_payload())


# ----------------------------------------------------------------- backups
# A backup holds one profile's credentials and access tokens, which is
# what lets a restore bring a bank back without spending a Plaid Item.
# Automatic ones are taken before unlink, reset and delete; manual ones
# are taken here and never pruned.

@app.route("/api/backups")
def api_backups():
    return jsonify(ok=True, backups=profiles.list_backups())


@app.route("/api/backups", methods=["POST"])
def api_backup_create():
    """A manual backup: one profile, or every profile when none is named.

    One file per profile either way, so a restore never has to pick a
    profile out of a bundle."""
    data = request.get_json(silent=True) or {}
    pid = (data.get("profile_id") or "").strip()
    config, secrets_store = profiles.read_store()
    all_profiles = profiles.profiles(config)

    if pid:
        if pid not in all_profiles:
            return jsonify(ok=False, error="No such profile."), 404
        targets = [pid]
    else:
        targets = list(all_profiles)
    if not targets:
        return jsonify(ok=False, error="There is nothing to back up yet."), 400

    for target in targets:
        path = backup_profile(config, secrets_store, target, "manual")
        print(f"[setup] manual backup of profile {target} at {path}")
    return jsonify(ok=True, saved=len(targets), **profiles_payload())


@app.route("/api/backups/<name>/restore", methods=["POST"])
def api_backup_restore(name):
    data = request.get_json(silent=True) or {}
    payload = profiles.read_backup(name)
    if payload is None:
        return jsonify(ok=False, error="That backup is missing or unreadable."), 404

    profile, creds = profiles.backup_profile_payload(payload)
    if not creds.get("client_id") or not creds.get("secret"):
        return jsonify(
            ok=False,
            error="That backup has no saved credentials, so there is nothing to restore.",
        ), 400

    # Checked before anything is written, so a restore that cannot work
    # does not leave a dead profile in the list. Refusable rather than
    # fatal: the access tokens in the backup are still good even when the
    # secret has been rotated since, and restoring is how you get them
    # back before fixing the secret.
    if not data.get("force"):
        problem = verify_credentials(
            creds["client_id"], creds["secret"],
            profile.get("environment") or "production",
        )
        if problem:
            return jsonify(ok=False, error=problem, confirmable=True), 400

    before = set(profiles.profiles())
    pid = profiles.restore_backup(payload)
    merged = pid in before
    print(f"[setup] restored backup {name} into profile {pid}"
          if merged else f"[setup] restored backup {name} as profile {pid}")
    if merged:
        # Its banks went back into the profile that is already there, so
        # the widget is a fetch behind.
        clear_fetched_data()
        refresh_in_background()
    return jsonify(ok=True, profile_id=pid, merged=merged, **profiles_payload())


@app.route("/api/backups/<name>/delete", methods=["POST"])
def api_backup_delete(name):
    if not profiles.delete_backup(name):
        return jsonify(ok=False, error="That backup is missing already."), 404
    print(f"[setup] deleted backup {name}")
    return jsonify(ok=True, backups=profiles.list_backups())


def clear_profile_data(pid):
    """Forget one profile's cached balances.

    Separate from clear_fetched_data(), which blanks the merged data.json
    the widget draws. This only removes the per-profile cache, so the
    accounts of other profiles are untouched."""
    try:
        os.remove(profile_data_path(pid))
    except OSError:
        pass


def link_target(profile_id):
    """The profile a link should attach to.

    Named explicitly by the page when more than one profile is enabled,
    because guessing would quietly put a work bank in the personal
    profile. Falls back to the active one, which is what the page relies
    on when there is only one and it therefore asks nothing."""
    if profile_id:
        resolved = profiles.resolve(profile_id)
        if resolved:
            return resolved
        return None
    return profiles.active()


@app.route("/api/link_token", methods=["POST"])
def api_link_token():
    body = request.get_json(silent=True) or {}
    config = link_target(body.get("profile_id"))
    if not config:
        return jsonify(ok=False, error="No credentials saved yet."), 400
    try:
        return jsonify(ok=True, link_token=link.new_link_token(
            link.build_client(config), config.get("environment")
        ))
    except Exception as e:
        return jsonify(ok=False, error=_plaid_message(e)), 400


@app.route("/api/exchange", methods=["POST"])
def api_exchange():
    body = request.get_json(silent=True) or {}
    config = link_target(body.get("profile_id"))
    if not config:
        return jsonify(ok=False, error="No credentials saved yet."), 400
    try:
        item = link.add_item(
            link.build_client(config),
            config,
            body["public_token"],
            body.get("institution_name"),
            body.get("institution_id"),
        )
    except Exception as e:
        return jsonify(ok=False, error=_plaid_message(e)), 400
    return jsonify(ok=True, institution_name=item["institution_name"])


@app.route("/api/remove", methods=["POST"])
def api_remove():
    body = request.get_json(silent=True) or {}
    item_id = body.get("item_id")
    config, secrets_store = profiles.read_store()

    # Found by searching rather than taken from the active profile: with
    # several enabled, the bank being removed may well belong to another
    # one, and removing it from whichever profile happens to be active
    # would delete the wrong token.
    owner = None
    for pid, profile in profiles.profiles(config).items():
        if any(it.get("item_id") == item_id for it in profile.get("items") or []):
            owner = pid
            break
    if owner is None:
        return jsonify(ok=False, error="No linked bank with that id."), 404

    profile = config["profiles"][owner]
    before = len(profile.get("items") or [])
    profile["items"] = [
        it for it in profile.get("items") or [] if it.get("item_id") != item_id
    ]
    profiles.write_config(config)
    config = profiles.resolve(owner) or {"items": profile["items"]}
    print(f"removed item {item_id} from {owner} ({before} -> {len(profile['items'])})")
    # The access token is gone from config.json, but Plaid still holds the
    # Item until it's explicitly removed there. Not done automatically:
    # it's irreversible and someone re-adding the same bank in a minute
    # shouldn't be punished for it.
    # items_created deliberately does not come down with this: the Trial
    # allowance it tracks is spent, and re-linking this bank would spend
    # another Item rather than reclaim this one.
    return jsonify(
        ok=True,
        items=item_summary(config),
        items_created=items_created(config),
        **profiles_payload(),
    )


@app.route("/api/verify", methods=["POST"])
def api_verify():
    """One real fetch, so setup fails loudly here if anything is wrong.

    This is the step worth insisting on. Everything before it can succeed
    and still leave someone looking at an empty widget -- credentials that
    validate but lack the right products, a bank that linked but returns
    nothing. Finding that out now, with the setup page still open, is the
    difference between a fixable message and a mystery."""
    import fetch_data

    try:
        fetch_data.main()
    except (Exception, SystemExit) as e:
        return jsonify(ok=False, error=str(e)), 400

    try:
        with open(os.path.join(DATA_DIR, "data.json"), "r") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return jsonify(ok=False, error=f"Fetch ran but data.json is unreadable: {e}"), 400

    return jsonify(
        ok=True,
        accounts=len(data.get("accounts", [])),
        errors=[e.get("error_message", str(e)) for e in data.get("errors", [])],
    )


def install_binary():
    """Puts the widget-named copy of this exe in DATA_DIR.

    A copy, not a generated file: a program that writes a new .exe is
    doing what droppers do, and antivirus treats it accordingly. It's the
    same binary -- only the filename differs, and the filename is what
    selects widget mode over setup mode.

    Returns (path, replaced). A no-op when not frozen, where there's no
    single file to install.

    Windows locks a running exe, so this fails with WinError 32 whenever
    the widget is already running -- which is the normal case for someone
    who opened setup from the tray to add a bank. That isn't an error
    worth stopping for: the binary is demonstrably already installed, so
    keep the existing one and carry on to the shortcuts, which is what
    the caller actually came for. Only a missing target makes it fatal."""
    if not FROZEN:
        return None, False
    target = installed_exe_path()
    if os.path.abspath(sys.executable) == os.path.abspath(target):
        return target, False
    try:
        shutil.copy2(sys.executable, target)
        return target, True
    except OSError:
        if os.path.exists(target):
            print(f"[install] {target} is in use; keeping the installed copy")
            return target, False
        raise


def install_uninstaller():
    """Puts Uninstall.exe next to the app and lists it in Settings.

    Returns the path, or None when there is nothing to install -- from
    source there is no single file to copy, and the uninstaller is then
    reached with --uninstall instead.

    Failures here are reported but never fatal: an app that refused to
    finish installing because its uninstaller could not be written would
    be a worse outcome than one you remove from the Modify page."""
    if not FROZEN:
        return None
    target = uninstall_exe_path()
    try:
        if os.path.abspath(sys.executable) != os.path.abspath(target):
            shutil.copy2(sys.executable, target)
    except OSError as e:
        # In use means a previous uninstall is mid-flight, in which case
        # the copy already there is the one we would have written.
        if not os.path.exists(target):
            print(f"[install] could not write {target}: {e}")
            return None
        print(f"[install] {target} is in use; keeping the existing copy")

    import logo_cache

    n = native()
    size_kb = None
    try:
        size_kb = int(os.path.getsize(target) / 1024) * 2  # app + uninstaller
    except OSError:
        pass
    n.register_uninstaller(
        target,
        name=APP_NAME,
        display_name=APP_NAME,
        icon=logo_cache.icon_file() or target,
        install_location=DATA_DIR,
        size_kb=size_kb,
    )
    return target


@app.route("/api/install", methods=["POST"])
def api_install():
    body = request.get_json(silent=True) or {}
    autostart = bool(body.get("autostart"))
    # Absent is not the same as false. The wizard's install posts only
    # autostart, and it must not delete a shortcut it never asked about --
    # so a missing key leaves that shortcut alone, except for the
    # Start-menu one, which an install has always created.
    want_start_menu = body.get("start_menu", True)
    want_desktop = body.get("desktop", None)
    n = native()
    result = {"app_path": None, "start_menu": None, "desktop": None,
              "startup": None, "uninstaller": None}

    seed_data_dir()

    try:
        result["app_path"], result["replaced"] = install_binary()
    except OSError as e:
        return jsonify(
            ok=False, error=f"Could not install to {installed_exe_path()}: {e}"
        ), 400

    exe, args, icon = launch_target()
    # Frozen, the exe is self-contained and its own folder is the right
    # place to run from. From source the target is pythonw.exe, which
    # lives in the Python install -- the working directory has to be the
    # project, or the "desktopcards.py" argument resolves to nothing.
    working_dir = os.path.dirname(exe) if FROZEN else ASSET_DIR

    def shortcut(path, wanted):
        """Writes the shortcut, removes it, or leaves it as it is."""
        if wanted is None:
            return path if os.path.exists(path) else None
        if not wanted:
            n.remove_shortcut(path)
            return None
        return n.create_shortcut(
            path,
            target=exe,
            arguments=args,
            icon=icon,
            working_dir=working_dir,
            description="DesktopCards balance widget",
        )

    result["start_menu"] = shortcut(n.start_menu_path(APP_NAME), want_start_menu)
    result["desktop"] = shortcut(n.desktop_path(APP_NAME), want_desktop)
    result["startup"] = n.install_autostart(
        autostart, exe, arguments=args, icon=icon, name=APP_NAME,
        working_dir=working_dir,
    )
    # Last, and after the shortcuts: what it is registered to remove has
    # to exist before it is advertised as the way to remove it.
    result["uninstaller"] = install_uninstaller()
    print(f"installed: {result}")
    return jsonify(ok=True, **result)


@app.route("/api/launch", methods=["POST"])
def api_launch():
    # Already running? Restart it rather than spawning a second copy.
    # The single-instance lock would make a second launch exit silently,
    # which is the wrong response to someone pressing a button -- and a
    # restart is usually what they want anyway, since the reason to press
    # it after changing something is to pick that change up.
    if widget_is_running():
        if ask_widget_to_restart():
            return jsonify(ok=True, action="restarted")
        return jsonify(
            ok=False,
            error="A widget is running but wouldn't restart. Quit it from "
                  "the tray icon and try again.",
        ), 409

    exe, args, _ = launch_target()

    # Install on demand rather than refusing. Someone who clicked Launch
    # wants the widget running, and the only thing standing in the way is
    # a file copy we were going to do anyway. Launching the *current* exe
    # instead isn't an option: this process is the setup-named copy, so it
    # would just reopen setup.
    if FROZEN and not os.path.exists(exe):
        try:
            install_binary()
        except OSError as e:
            return jsonify(ok=False, error=f"Could not install to {exe}: {e}"), 400
        n = native()
        n.create_shortcut(
            os.path.join(n.start_menu_dir(), f"{APP_NAME}.lnk"),
            target=exe, arguments="", icon=None,
            working_dir=os.path.dirname(exe),
            description="DesktopCards balance widget",
        )

    if not os.path.exists(exe):
        return jsonify(ok=False, error=f"{exe} is missing."), 400

    cwd = os.path.dirname(exe) if FROZEN else ASSET_DIR
    # clean_child_env matters here for the same reason it does in
    # widget.spawn_self: setup exits once it's done, and a widget that
    # inherited setup's unpack directory would have it deleted underneath
    # it moments later.
    subprocess.Popen(
        [exe] + ([args] if args else []), cwd=cwd,
        env=clean_child_env(), **DETACHED,
    )
    return jsonify(ok=True, action="launched")


@app.route("/api/quit", methods=["POST"])
def api_quit():
    shutdown_event.set()
    return jsonify(ok=True)


@app.route("/api/alive", methods=["POST"])
def api_alive():
    """The page's heartbeat. note_activity has already done the work."""
    return jsonify(ok=True)


@app.route("/api/closing", methods=["POST"])
def api_closing():
    """The tab is going away. Arms a shutdown rather than performing one.

    A reload fires this exactly like a close does, and the two are
    indistinguishable from here -- so the difference is drawn in time: if
    another request arrives within CLOSE_GRACE it was a reload and
    note_activity cancels this, and if nothing comes the tab really is
    gone."""
    global _closing_at
    _closing_at = time.time() + CLOSE_GRACE
    return jsonify(ok=True)


def watch_for_shutdown():
    """Exits once nothing is reading this page any more.

    Polls rather than blocking on the event alone, since two of the three
    shutdown reasons are deadlines rather than events."""
    while not shutdown_event.wait(timeout=1.0):
        now = time.time()
        if _closing_at is not None and now >= _closing_at:
            print("[setup] the page was closed; shutting down")
            os._exit(0)
        if now - _last_seen > IDLE_GRACE:
            print(f"[setup] nothing has contacted us in {IDLE_GRACE:.0f}s; shutting down")
            os._exit(0)
    # Long enough for the response to the request that asked for this to
    # reach the browser. os._exit is immediate and takes the socket with
    # it, so without the pause the reply to /api/quit was cut mid-write:
    # the page's fetch rejected with a network error and the one button
    # whose entire job is to stop the server looked broken, even though
    # the server had in fact stopped.
    time.sleep(0.4)
    # Flask's development server has no clean programmatic stop, and this
    # is a single-purpose process whose job is finished -- nothing here
    # owns state worth unwinding.
    os._exit(0)


def main():
    threading.Thread(target=watch_for_shutdown, daemon=True).start()
    url = f"http://{LOOPBACK}:{SETUP_PORT}"
    print(f"Setup is at {url}")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    # localhost only. This form carries a Plaid secret; it has no business
    # being reachable from the network.
    app.run(host=LOOPBACK, port=SETUP_PORT)


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DesktopCards Setup</title>
<script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
<style>
  /* Follows the widget's own dark_theme flag, applied as one attribute on
     <html> exactly the way dashboard.html does it -- every color below
     comes from these vars, so neither theme needs a second copy of any
     rule. Dark is the base rather than the override because that's the
     shipped default (appearance.example.json), so the fallback when there
     is no appearance.json yet matches what a first run will actually get.
     color-scheme is here for the selects and the color picker, which a
     stylesheet can't reach into. (Checkboxes used to need it too; they're
     drawn by the rules further down now.) */
  :root {
    --bg: #15181e; --card: #1d2128; --card-2: #23272f;
    --text: #e8eaed; --dim: #9aa0a6; --faint: #6b7280;
    --accent: #4c8dff; --ok: #34c759; --bad: #ff6b6b; --line: #2d323b;
    --hair: rgba(255,255,255,.04);
    /* The surface button tier, kept as its own pair of tokens rather than
       reusing --card-2/--line: a surface button sits *on* a .card, so it
       needs a fill that reads as raised against it and a hover a step
       further again, which is one more level than the card tokens have. */
    --btn-bg: #262b34; --btn-bg-hover: #2f3540; --btn-line: #363d49;
    /* The section grid: the space between boxes, and the granularity
       their heights are rounded up to. */
    --gap: 16px; --row-unit: 4px;
    --tick: url("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='3.5' stroke-linecap='round' stroke-linejoin='round'><path d='M20 6 9 17l-5-5'/></svg>");
    --dash: url("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='black' stroke-width='3.5' stroke-linecap='round'><path d='M6 12h12'/></svg>");
    /* The hard offset shadow these controls draw, borrowed from the
       widget's own card shadow (appearance.json "shadow") so the two
       surfaces cast the same kind of light. One distance per group of
       controls, since they are nothing like the same size. Inert at 0:
       every rule below spends its distance twice and a 0 0 shadow with no
       blur paints nothing, so the whole feature is off until index() sends
       one -- which it only does when appearance.json's file-only
       "shadow_ui" block asks for it. No blur anywhere: this is a drawn
       edge, and a blurred copy of it would read as a different control. */
    --ui-shadow-tab: 0px;
    --ui-shadow-input: 0px;
    --ui-shadow-check: 0px;
    --ui-shadow-row: 0px;
    --ui-shadow-btn: 0px;
    --ui-shadow-color: #0a0c11;
    color-scheme: dark;
  }
  :root[data-theme="light"] {
    --bg: #f4f5f7; --card: #ffffff; --card-2: #eceff3;
    --text: #1b2230; --dim: #596070; --faint: #878f9d;
    --accent: #0b66d0; --ok: #1a8f43; --bad: #c62828; --line: #dde1e8;
    --hair: rgba(0,0,0,.05);
    --btn-bg: #f0f2f6; --btn-bg-hover: #e4e8ef; --btn-line: #d2d8e1;
    /* Near-black on white is a hole, not a shadow, so the light palette
       takes the same slate the cards' borders are cut from. */
    --ui-shadow-color: #b9c0cb;
    color-scheme: light;
  }
  * { box-sizing: border-box; }
  /* Picking a control up drops its shadow: the distance closes to nothing,
     so the thing you are touching settles onto the page while everything
     around it stays raised. One keyframe serves every group because it
     animates --ui-shadow-self, which each rule below sets to its own
     group's distance -- a custom property resolves against the element
     being animated, not against where the keyframes were written. */
  @keyframes ui-shadow-settle {
    from { box-shadow: var(--ui-shadow-self) var(--ui-shadow-self) var(--ui-shadow-color); }
    to   { box-shadow: 0 0 var(--ui-shadow-color); }
  }
  body {
    margin: 0; padding: 2.5rem 1rem 4rem; background: var(--bg); color: var(--text);
    font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
    display: flex; justify-content: center;
  }
  /* Setup runs in one narrow column -- a five-step wizard is a sequence,
     and a 1000px-wide instruction is harder to read, not easier. The
     manage view is the opposite: three tabs of dense, unordered settings
     that were stacked into one 620px strip with a third of the window
     empty on either side. main.manage is set by loadState once it knows
     which of the two the page is. */
  main { width: 100%; max-width: 620px; }
  main.manage { max-width: 1060px; padding-bottom: 4.5rem; }

  /* For blocks that stay single-column on a wide page: a label/control row
     stretched to 1060px leaves its two halves at opposite ends of the
     window with nothing in between. */
  .narrow { max-width: 560px; }

  /* ---- a tab's sections, each in its own box ----
     Borrowed from DesktopTasks' settings page. A tab used to be one card
     with its sections ruled off inside it, which made a long strip where
     every section had to be as wide as the widest one. Now each section
     is its own box in a two-column grid, so "Accounts on the widget" can
     sit beside "Linked banks" rather than under it.

     The row bins are what stop a short box being stretched to the height
     of whatever is next to it: settings.js-style, buildBins() measures
     each box and rounds it up to a whole number of --row-unit rows, then
     `dense` backfills the holes. Binning needs JS, so it is a class the
     script adds -- without it this is an ordinary two-column grid where
     each row is as tall as its tallest box. A worse layout, not a broken
     one. */
  .grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    grid-auto-flow: row dense;
    gap: var(--gap);
    /* start, so a box is its own content's height: that is the state the
       measuring happens in, and a stretched box would measure as tall as
       the tallest one beside it. */
    align-items: start;
  }
  /* Binned, stretch instead, so the rounding shows as nothing at all
     rather than as a strip of page under each box. */
  .grid.binned { grid-auto-rows: var(--row-unit); align-items: stretch; }
  .grid.binned > .card { grid-row: span var(--span, 1); }
  .card.wide { grid-column: span 2; }

  .card {
    background: var(--card); border: 1px solid var(--line); border-radius: 12px;
    padding: 1.1rem 1.25rem;
    /* The grid's gap spaces them, and a margin would be measured into the
       height and inflate the row span. */
    margin-bottom: 0;
    min-width: 0;
  }
  .card > h3.grp:first-child { margin-top: 0; }
  .card > :last-child { margin-bottom: 0; }

  /* A tab is now a frame around the grid rather than a card itself. */
  section.panel {
    background: none; border: 0; padding: 0; margin-bottom: 0;
  }
  section.panel > .hint { margin-left: 0; }

  /* ---- the bottom ribbon ----
     Every button a tab has, latched to the foot of the window instead of
     to the bottom of whichever box it happened to belong to. Which ones
     show is the tab's business: a button that cannot do anything on this
     tab is better not drawn at all than drawn disabled.

     The message line lives here too, left of the buttons, because the
     result of pressing one belongs beside it -- and with the buttons out
     of the boxes there is nowhere above for it to go. One line per tab
     rather than one shared: each tab's script already writes to its own,
     and only the active tab's is drawn. */
  .savebar {
    position: fixed; left: 0; right: 0; bottom: 0; z-index: 30;
    background: var(--card); border-top: 1px solid var(--line);
    padding: .75rem 1rem;
  }
  .savebar[hidden] { display: none; }
  .savebar .inner {
    max-width: 1060px; margin: 0 auto;
    display: flex; align-items: center; gap: .6rem; flex-wrap: wrap;
  }
  /* Takes the slack, which is what keeps the buttons at the right end
     once there is something to say. */
  .savebar .msg { margin: 0 auto 0 0; flex: 1; min-width: 0; text-align: left; }
  .savebar .msg[hidden] { display: none; }
  .savebar .acts {
    display: none; gap: .6rem; align-items: center; flex-wrap: wrap;
    margin: 0; padding: 0; border-top: 0;
  }
  .savebar .acts.on { display: flex; }
  .savebar button { margin-top: 0; }
  /* Opens upward: the bar is at the foot of the window, so the default
     drop below it would be off screen. */
  .savebar .menu { top: auto; bottom: calc(100% + .35rem); }

  /* Below this a column is too narrow for a .fld's label and its control
     side by side, which is the shape of nearly every row in these forms. */
  @media (max-width: 900px) {
    .grid { grid-template-columns: minmax(0, 1fr); }
    .card.wide { grid-column: span 1; }
  }
  h1 { font-size: 1.5rem; margin: 0 0 .25rem; letter-spacing: -.01em; }
  .sub { color: var(--dim); margin: 0 0 2rem; }
  section {
    background: var(--card); border: 1px solid var(--line); border-radius: 12px;
    padding: 1.25rem 1.4rem; margin-bottom: 1rem;
  }
  section[hidden] { display: none; }
  section.done { opacity: .62; }
  h2 { font-size: 1.02rem; margin: 0 0 .2rem; display: flex; align-items: center; gap: .55rem; }
  .num {
    flex: 0 0 1.4rem; height: 1.4rem; border-radius: 999px; background: var(--card-2);
    color: var(--dim); font-size: .78rem; display: grid; place-items: center;
  }
  section.active .num { background: var(--accent); color: #fff; }
  section.done .num { background: var(--ok); color: #fff; }
  .hint { color: var(--dim); font-size: .88rem; margin: .1rem 0 1rem 1.95rem; }
  .body { margin-left: 1.95rem; }
  label { display: block; font-size: .82rem; color: var(--dim); margin: .75rem 0 .3rem; }
  /* One shape for every box you type into, number boxes included: no
     fill, a 2px edge and a 10px radius. The reference draws its edge
     purely in layered shadows, which assume one fixed dark backdrop --
     these boxes sit on three (page, card, table row) and have to survive
     the light theme, so the border carries the shape instead and focus
     moves its color rather than stacking another shadow on top. */
  input[type=text], input[type=password], input[type=number], select {
    width: 100%; padding: .55rem .7rem; border-radius: 10px; color: var(--text);
    background: transparent; border: 2px solid var(--line); font: inherit;
  }
  input[type=text]:hover:not(:disabled), input[type=password]:hover:not(:disabled),
  input[type=number]:hover:not(:disabled), select:hover:not(:disabled) {
    border-color: var(--btn-line);
  }
  input:focus, select:focus { outline: none; border-color: var(--accent); }
  /* The closed box stays transparent so it can sit on any of the three
     backdrops, but the popup list is a separate native surface: it takes
     the select's own background, which transparent resolves to white in
     both themes, leaving near-white option text readable only under the
     hover highlight. The options carry a real card fill of their own. */
  select option { background: var(--card); color: var(--text); }
  /* Every box is raised while it is waiting for you, and the one you click
     into drops to the page -- the same move the layout rows make, so being
     edited looks the same wherever it happens. */
  input[type=text], input[type=password], input[type=number], select {
    --ui-shadow-self: var(--ui-shadow-input);
    box-shadow: var(--ui-shadow-self) var(--ui-shadow-self) var(--ui-shadow-color);
  }
  input[type=text]:focus, input[type=password]:focus,
  input[type=number]:focus, select:focus {
    animation: ui-shadow-settle .45s ease-out forwards;
  }
  input[type=text]:disabled, input[type=password]:disabled,
  input[type=number]:disabled, select:disabled { box-shadow: none; }
  @media (prefers-reduced-motion: reduce) {
    input[type=text]:focus, input[type=password]:focus,
    input[type=number]:focus, select:focus { animation: none; box-shadow: none; }
  }
  /* Every number box on the page, not just the ones in a form row: a
     right-aligned figure ends against its unit ("8" "px") and a column of
     them lines up on the ones digit instead of the leading one. */
  input[type=number] { text-align: right; font-variant-numeric: tabular-nums; }

  /* ---- buttons ------------------------------------------------------
     Chakra's button variants, as plain CSS: "surface" is the bare
     element, so every button on the page is one unless it says
     otherwise, and .solid is the single primary action in a group (the
     one that writes something, or moves the setup forward). The old
     split was the other way round -- solid by default with a .ghost
     opt-out -- which made four solid blue buttons in a row normal, and
     left nothing for the one that mattered to stand out with. */
  button {
    /* 80% of the sizes this page shipped with (.62/1.1rem at the body's
       15px): a settings page is almost all controls, and at full size a
       row of them read as the loudest thing on it. */
    margin-top: 1rem; padding: .5rem .88rem; border-radius: 7px;
    font: inherit; font-size: .86rem; font-weight: 550; cursor: pointer;
    /* inline-flex, not the default inline-block: the loading spinner is a
       ::before box that has to sit on the label's line and share a gap
       with it, and baseline alignment of an inline-block won't do that. */
    display: inline-flex; align-items: center; justify-content: center; gap: .45rem;
    background: var(--btn-bg); color: var(--text); border: 1px solid var(--btn-line);
    transition: background .12s ease, border-color .12s ease, box-shadow .32s ease;
  }
  /* The only shadow on the page that arrives rather than leaves: a button
     is raised by being pointed at, and the distance grows from nothing to
     full in 0.32s, holds while the pointer is on it, then runs back down
     when it leaves. Quicker than the 0.45s the rows take to settle: a
     hover is answering you, and anything you can outrun with the pointer
     feels like lag rather than motion. A transition
     rather than a keyframe because it has to be interruptible -- a
     keyframe would snap back to 0 the moment the pointer left, and the
     ask is for it to travel in both directions. .tab is excluded: tabs are
     buttons only for the keyboard's sake, and they cast on selection. */
  button:hover:not(:disabled):not(.tab) {
    background: var(--btn-bg-hover);
    box-shadow: var(--ui-shadow-btn) var(--ui-shadow-btn) var(--ui-shadow-color);
  }
  @media (prefers-reduced-motion: reduce) {
    button { transition: none; }
  }
  button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  button.solid { background: var(--accent); color: #fff; border-color: transparent; }
  /* Brightness rather than a second accent token: the accent is the user's
     Windows accent color at runtime, so there's no fixed darker shade of it
     to name -- and this works whichever end of the range theirs is at. */
  button.solid:hover:not(:disabled) { background: var(--accent); filter: brightness(1.1); }
  button.danger { color: var(--bad); padding: .24rem .5rem; margin: 0; font-size: .72rem; }
  /* :not(.loading) -- a loading button is disabled so it can't be pressed
     twice, but it's mid-task rather than unavailable, and fading it makes
     its own spinner hard to see. */
  button:disabled:not(.loading) { opacity: .5; cursor: default; }

  /* Loading state: the spinner is the button's own ::before, and the
     label swap is loading()'s job in the script below. */
  button.loading { cursor: default; }
  button.loading::before {
    content: ''; flex: 0 0 auto; width: .85em; height: .85em;
    border: 2px solid currentColor; border-top-color: transparent;
    border-radius: 50%; animation: btn-spin .55s linear infinite;
  }
  @keyframes btn-spin { to { transform: rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) {
    button.loading::before { animation-duration: 1.6s; }
  }

  /* ---- split button + its menu ----
     Two buttons sharing a border, so the chevron reads as part of the Save
     button rather than a second control next to it. */
  .btn-group { display: inline-flex; vertical-align: middle; }
  .btn-group > button { margin-top: 0; }
  .btn-group > button:first-child { border-top-right-radius: 0; border-bottom-right-radius: 0; }
  .btn-group > button + button {
    border-top-left-radius: 0; border-bottom-left-radius: 0;
    margin-left: -1px; /* the shared edge, drawn once */
    padding-left: .44rem; padding-right: .44rem;
  }
  /* A solid group has no borders to hide the seam, so it gets drawn. */
  .btn-group > button.solid + button.solid { box-shadow: inset 1px 0 0 rgba(0,0,0,.18); }
  .btn-group svg { width: 1em; height: 1em; display: block; }

  .menu-root { position: relative; display: inline-flex; }
  .menu {
    position: absolute; top: calc(100% + .35rem); right: 0; z-index: 20;
    min-width: 11.5rem; padding: .25rem;
    background: var(--card); border: 1px solid var(--line); border-radius: 10px;
    box-shadow: 0 12px 30px rgba(0,0,0,.32);
  }
  .menu[hidden] { display: none; }
  .menu button {
    margin: 0; width: 100%; justify-content: flex-start;
    background: transparent; border-color: transparent; border-radius: 6px;
    padding: .38rem .5rem; font-weight: 500;
  }
  /* A menu item is not a button sitting on the page, it is a row inside
     one that already casts its own shadow -- so the hover shadow every
     other button gets is switched back off here. */
  .menu button:hover:not(:disabled) { background: var(--btn-bg-hover); box-shadow: none; }

  /* ---- checkboxes ----
     Chakra's Checkbox.Control: a square that takes the accent and a white
     tick when checked, in place of the OS one. Still a real
     <input type=checkbox> underneath -- appearance: none only replaces how
     it's painted, so the label association, keyboard behaviour and the
     .checked reads the script already does all go on working untouched.
     The tick is a mask rather than a border trick so it stays a tick at
     any zoom, and takes its color from the background below it. */
  input[type=checkbox] {
    appearance: none; -webkit-appearance: none;
    /* The shadow is painted outside the box, into whatever sits next to
       it, and the label text is both closer and drawn later -- which is
       what was clipping the corner off it. The reserved strip is the room
       it needs; position lifts the box and its shadow above the
       surrounding text rather than leaving the two to share pixels. */
    position: relative;
    margin: 0 var(--ui-shadow-check) var(--ui-shadow-check) 0;
    flex: 0 0 auto; width: 1.05rem; height: 1.05rem;
    display: inline-grid; place-content: center;
    background: var(--btn-bg); border: 1px solid var(--btn-line);
    border-radius: 4px; cursor: pointer;
    transition: background .12s ease, border-color .12s ease;
  }
  input[type=checkbox]::before {
    content: ''; width: .72rem; height: .72rem; background: #fff;
    /* The tick, as a mask: stroke is a named color so the data URI needs
       no escaped # inside this stylesheet. */
    -webkit-mask: var(--tick) center / contain no-repeat;
    mask: var(--tick) center / contain no-repeat;
    transform: scale(.5); opacity: 0;
    transition: transform .12s ease, opacity .12s ease;
  }
  input[type=checkbox]:checked {
    background: var(--accent); border-color: var(--accent);
  }
  input[type=checkbox]:checked::before { transform: scale(1); opacity: 1; }
  /* Partially selected: a dash, which is the state a select-all box is in
     when some but not all of its rows are ticked. The <input> tracks this
     itself via .indeterminate -- nothing here has to model it. */
  input[type=checkbox]:indeterminate { background: var(--accent); border-color: var(--accent); }
  input[type=checkbox]:indeterminate::before {
    -webkit-mask: var(--dash) center / contain no-repeat;
    mask: var(--dash) center / contain no-repeat;
    transform: scale(1); opacity: 1;
  }
  /* Ticked or focused, either one: the shadow marks a box that is set, and
     follows the keyboard as well so tabbing through a list of them reads
     the same way as clicking. */
  input[type=checkbox]:checked, input[type=checkbox]:indeterminate,
  input[type=checkbox]:focus-visible {
    box-shadow: var(--ui-shadow-check) var(--ui-shadow-check) var(--ui-shadow-color);
  }
  input[type=checkbox]:hover:not(:disabled) { border-color: var(--accent); }
  input[type=checkbox]:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  input[type=checkbox]:disabled { opacity: .45; cursor: default; }
  @media (prefers-reduced-motion: reduce) {
    input[type=checkbox]::before { transition: none; }
  }

  /* ---- switch (Chakra's Switch.Root) ----
     What the Appearance tab's booleans all are: the widget either does
     this or it doesn't, applied the moment it's saved. A switch says that;
     a checkbox says "include this in a set", which is what the account
     list and the two opt-ins elsewhere on the page actually are -- so
     those stay checkboxes and only the appearance form opts in here.
     Reuses the checkbox's ::before as the thumb rather than adding a
     wrapper element, which is what keeps these real <input>s. */
  input[type=checkbox].switch {
    width: 2.1rem; height: 1.2rem; border-radius: 999px;
    position: relative; place-content: stretch;
  }
  input[type=checkbox].switch::before {
    -webkit-mask: none; mask: none; /* thumb, not tick */
    position: absolute; top: 50%; left: 2px;
    width: .82rem; height: .82rem; border-radius: 50%;
    background: var(--dim); opacity: 1;
    transform: translateY(-50%);
    transition: left .15s ease, background .15s ease;
  }
  input[type=checkbox].switch:checked::before {
    left: calc(100% - .82rem - 3px);
    background: #fff; opacity: 1; transform: translateY(-50%);
  }
  @media (prefers-reduced-motion: reduce) {
    input[type=checkbox].switch::before { transition: none; }
  }

  /* ---- color picker (Chakra's ColorPicker.Control) ----
     A hex Input next to a swatch Trigger, both editing the one value. The
     text box is the value that gets saved, not the swatch: a swatch can
     only hold a hex triple, and these fields also have to carry a blank
     ("follow the theme") and any CSS color name already sitting in
     appearance.json. .unset dims the swatch to say it isn't pinning
     anything -- it's still showing a color, because a native
     <input type=color> always does. */
  .cpick { display: flex; align-items: center; gap: .35rem; min-width: 0; }
  .cpick > .cpick-hex {
    flex: 1 1 auto; min-width: 0; width: auto;
    font-variant-numeric: tabular-nums;
  }
  .cpick > input[type=color] {
    flex: 0 0 auto; width: 1.9rem; height: 1.8rem; padding: 0;
    background: var(--bg); border: 1px solid var(--line); border-radius: 6px;
    cursor: pointer;
    /* The well is a box you set a value in, so it is raised with the rest
       of them -- and it drops the same way when you open the picker. */
    --ui-shadow-self: var(--ui-shadow-input);
    box-shadow: var(--ui-shadow-self) var(--ui-shadow-self) var(--ui-shadow-color);
  }
  .cpick > input[type=color]:focus { animation: ui-shadow-settle .45s ease-out forwards; }
  /* The native control insets its color well inside its box; these pull
     the fill back out to the border so it reads as a color chip. */
  .cpick > input[type=color]::-webkit-color-swatch-wrapper { padding: 2px; }
  .cpick > input[type=color]::-webkit-color-swatch { border: 0; border-radius: 4px; }
  .cpick > input[type=color]::-moz-color-swatch { border: 0; border-radius: 4px; }
  .cpick.unset > input[type=color] { opacity: .45; }
  .cpick > .cpick-clear {
    flex: 0 0 auto; margin: 0; padding: .1rem .25rem;
    font-size: .95rem; line-height: 1; font-weight: 400; color: var(--faint);
    background: transparent; border-color: transparent;
  }
  .cpick > .cpick-clear:hover:not(:disabled) { background: transparent; color: var(--text); }
  .cpick.unset > .cpick-clear { visibility: hidden; }

  /* ---- slider (Chakra's Slider: a Track with a Range, and a Thumb) ----
     A real <input type=range> next to a number box that holds the value,
     so the form still reads a plain .value off one element. The filled
     Range is a hard-stopped gradient at --pct, which the script sets from
     the box -- ::-moz-range-progress does it natively but only in
     Firefox. */
  .slider { display: flex; align-items: center; gap: .55rem; flex: 1 1 auto; min-width: 0; }
  /* Typable, not just a readout: the track is for sweeping and the box is
     for the value you already know. Spinners off -- the track is the
     nudge-by-a-step control, and they'd eat half the box's width. */
  .slider > .slider-num {
    flex: 0 0 3.1rem; width: auto; min-width: 0;
    padding: .25rem .35rem; border-radius: 7px; text-align: right;
    font-variant-numeric: tabular-nums;
    appearance: textfield; -moz-appearance: textfield;
  }
  .slider > .slider-num::-webkit-outer-spin-button,
  .slider > .slider-num::-webkit-inner-spin-button {
    -webkit-appearance: none; margin: 0;
  }
  .slider > input[type=range] {
    appearance: none; -webkit-appearance: none;
    flex: 1 1 auto; min-width: 0; width: auto; height: 1.1rem;
    margin: 0; padding: 0; background: none; border: 0; cursor: pointer;
  }
  .slider > input[type=range]::-webkit-slider-runnable-track {
    height: .3rem; border-radius: 999px;
    background: linear-gradient(to right,
      var(--accent) 0 var(--pct, 0%), var(--btn-line) var(--pct, 0%) 100%);
  }
  .slider > input[type=range]::-webkit-slider-thumb {
    -webkit-appearance: none; appearance: none;
    width: .8rem; height: .8rem; margin-top: -.25rem;
    border-radius: 50%; background: #fff; border: 1px solid var(--accent);
    box-shadow: 0 1px 3px rgba(0,0,0,.3);
  }
  .slider > input[type=range]::-moz-range-track {
    height: .3rem; border-radius: 999px; background: var(--btn-line);
  }
  .slider > input[type=range]::-moz-range-progress {
    height: .3rem; border-radius: 999px; background: var(--accent);
  }
  .slider > input[type=range]::-moz-range-thumb {
    width: .8rem; height: .8rem; border-radius: 50%;
    background: #fff; border: 1px solid var(--accent);
  }
  .slider > input[type=range]:focus-visible {
    outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 999px;
  }

  .row { display: flex; gap: .6rem; flex-wrap: wrap; align-items: center; }

  /* ---- field groups and card footers (Chakra's Field.Root and
     Card.Footer) ----
     A label stacked directly over its input, and the actions gathered at
     the bottom right behind a divider. This is the shape for a form you
     fill in once: the five setup steps and the Advanced credentials block.
     The .fld rows in the Appearance and Layout tabs are deliberately the
     opposite arrangement -- a settings form is twenty short values and
     reads better as two aligned columns, where a form you fill in once
     reads better as a stack. */
  .fields { display: flex; flex-direction: column; gap: .95rem; }
  .field { display: flex; flex-direction: column; gap: .3rem; }
  .field > label { margin: 0; font-size: .82rem; color: var(--dim); }
  .field > .sub2 { color: var(--faint); font-size: .78rem; }

  .foot {
    display: flex; justify-content: flex-end; align-items: center;
    gap: .6rem; flex-wrap: wrap;
    margin-top: 1.3rem; padding-top: 1rem; border-top: 1px solid var(--line);
  }
  .foot > button { margin-top: 0; }
  /* The result of pressing the button belongs beside it, not stranded
     under it -- and taking the left end of the row is also what keeps the
     buttons at their end once there's something to say. */
  .foot > .msg { margin: 0 auto 0 0; text-align: left; }
  .msg { margin-top: .9rem; font-size: .89rem; }
  .msg.bad { color: var(--bad); }
  .msg.ok { color: var(--ok); }
  ul.banks { list-style: none; padding: 0; margin: .5rem 0 0; }
  ul.banks li {
    display: flex; justify-content: space-between; align-items: center; gap: 1rem;
    padding: .55rem .7rem; background: var(--card-2); border-radius: 8px; margin-bottom: .4rem;
  }
  ul.banks .none { color: var(--faint); background: none; padding-left: 0; }
  /* A profile row: a tick to enable it, its name, and a chevron that opens
     the same fields Add uses. Bordered rather than striped, because an
     expanded row has to stay visibly one thing while it is twice the
     height of its neighbours. */
  .prof { border: 1px solid var(--line); border-radius: .45rem; margin: .4rem 0; }
  .prof .top {
    display: flex; align-items: center; gap: .55rem;
    padding: .5rem .6rem; cursor: default;
  }
  .prof .top .name { font-weight: 500; }
  .prof .top .tail { color: var(--faint); font-variant-numeric: tabular-nums; }
  .prof .top .count { margin-left: auto; color: var(--dim); font-size: .88em; }
  /* A fixed square that centres its own icon, rather than a text button
     whose glyph sits wherever the font's baseline puts it: the row is
     align-items:center, so centring the box was never the problem, the
     glyph inside it was. Same stroked-SVG idiom as the tab icons. */
  .prof .chev {
    background: none; border: 0; color: var(--dim); padding: 0;
    width: 1.5rem; height: 1.5rem; flex: none; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
  }
  .prof .chev svg {
    width: .8rem; height: .8rem; display: block;
    transition: transform .15s ease;
  }
  .prof .chev.open svg { transform: rotate(180deg); }
  .prof .body { padding: 0 .6rem .6rem; border-top: 1px solid var(--hair); }
  .prof .body .fields { margin-top: .6rem; }
  .prof .acts { display: flex; flex-wrap: wrap; gap: .4rem; margin-top: .6rem; }
  .prof.off .top .name, .prof.off .top .tail { color: var(--dim); }
  .prof .cap { color: var(--bad); font-size: .88em; margin: .35rem 0 0; }
  .grp2 { margin: 1.2rem 0 .2rem; font-size: .95em; }
  /* Which profile a row belongs to. Same job as .profile-tag on the
     widget and the same quietness: with one profile enabled it is never
     drawn at all. */
  .plab { margin-left: .4rem; color: var(--faint); font-size: .85em; }
  /* Backups are a log, not settings: one line each, newest first, with
     the actions on the right where they do not interrupt the scan. */
  .bk {
    display: flex; align-items: center; gap: .5rem; padding: .35rem .1rem;
    border-bottom: 1px solid var(--hair); font-size: .9em;
  }
  .bk:last-child { border-bottom: 0; }
  .bk .when { color: var(--dim); font-variant-numeric: tabular-nums; }
  .bk .what { color: var(--faint); }
  .bk .acts { margin-left: auto; display: flex; gap: .3rem; }
  .bk button { font-size: .9em; padding: .18rem .45rem; }
  code { background: var(--card-2); padding: .1rem .35rem; border-radius: 4px;
         font-size: .85em; color: var(--dim); word-break: break-all; }
  a { color: var(--accent); }
  .check { display: flex; align-items: center; gap: .55rem; margin-top: 1rem;
           color: var(--dim); font-size: .9rem; }
  /* Left alone deliberately: the base rule's reserved strip is what keeps
     the label off this box's shadow. */
  footer { color: var(--faint); font-size: .82rem; text-align: center; margin-top: 1.5rem; }

  /* ---- management tabs ---- */
  /* The outline variant: the list carries one continuous rule and each tab
     is an outlined shape sitting astride it, so the active one reads as a
     folder tab merging into the panel below. The -1px pulls the tabs down
     onto that rule; the active tab's own fill then hides the span of it
     underneath, which is what joins tab to content. */
  nav.tabs { display: flex; gap: .2rem; margin: 0 0 1rem; border-bottom: 1px solid var(--line); }
  nav.tabs[hidden] { display: none; }
  /* Tabs happen to be <button>s for the keyboard, not to look like one --
     hence the explicit climb back out of the base button's fill and radius. */
  .tab {
    /* No shadow transition, unlike every other button: selecting a tab is
       a jump from one panel to another, and a shadow easing in behind the
       new tab reads as the page still settling after you have already
       moved. The base button rule's 0.45s is dropped here. */
    transition: background .12s ease, border-color .12s ease;
    margin: 0 0 -1px; padding: .45rem .8rem; border-radius: 8px 8px 0 0;
    background: transparent; color: var(--dim); font-size: .92rem; font-weight: 500;
    border: 1px solid transparent; gap: .4rem;
  }
  .tab > svg { width: .95em; height: .95em; flex: none; }
  .tab:hover:not(.on) { background: var(--card-2); color: var(--text); }
  .tab.on {
    color: var(--text); background: var(--card);
    border-color: var(--line); border-bottom-color: transparent;
    /* Down and to the right like everything else, and cut off at the
       rule the tab sits on rather than allowed to cross it: the active
       tab's bottom edge is missing so it merges into the panel below, and
       a shadow running past that line would paint the seam back in as a
       dark bar. The inset clips the bottom at the tab's own edge while the
       negative sides leave the sideways reach alone. */
    box-shadow: var(--ui-shadow-tab) var(--ui-shadow-tab) var(--ui-shadow-color);
    clip-path: inset(-50px -50px 0 -50px);
  }
  h3.grp {
    font-size: .78rem; text-transform: uppercase; letter-spacing: .06em; color: var(--faint);
    margin: 1.7rem 0 .2rem; padding-bottom: .35rem; border-bottom: 1px solid var(--line);
  }
  h3.grp:first-of-type { margin-top: .4rem; }
  .hint2 { color: var(--dim); font-size: .85rem; margin: .35rem 0 .6rem; }
  .hint2.warn { color: var(--bad); }

  /* Label left, control right: these are mostly numbers and checkboxes, and
     a stacked label per row turned a 20-field form into a very long page. */
  .fld {
    display: flex; align-items: center; gap: 1rem; padding: .42rem 0;
    border-bottom: 1px solid var(--hair);
  }
  .fld > .lbl { flex: 1 1 auto; font-size: .9rem; }
  .fld > .lbl .sub2 { display: block; color: var(--faint); font-size: .78rem; }
  /* The shortcut folders are the only labels holding a run of text with
     nowhere to break -- a path has no spaces until "Start Menu". A flex
     item is min-width:auto, so that run set the row's minimum and pushed
     the switch out through the side of the box. Breaking the path and
     letting the label shrink keeps it in. */
  #shortcutsCard .fld > .lbl { min-width: 0; }
  #shortcutsCard .fld .sub2 { overflow-wrap: anywhere; }
  /* One width for every row's control column, so the boxes line up on
     their left edge as well as their right. A picker or a slider gets no
     extra room: widening those rows was what pushed the accent box and
     the opacity track out past the plain number boxes under them. */
  /* The strip on the right is the room the controls' shadows need. Taken
     on the column rather than on each control, so every one of them --
     number box, select, color well, checkbox -- keeps the same indent and
     they all still end on one line. */
  .fld > .ctl { flex: 0 0 10.5rem; display: flex; align-items: center;
                justify-content: flex-end; gap: .4rem;
                padding-right: var(--ui-shadow-input); }
  /* The same box at form-row scale: a 10px radius on a 1.8rem-tall
     control reads as a pill, so the compact ones take a tighter one. */
  .fld input[type=text], .fld input[type=number], .fld select {
    width: 100%; padding: .32rem .45rem; border-radius: 7px;
  }
  /* Right-aligned like the numbers beside them: in a form of label-left,
     control-right rows, every value on the page starts at the same edge,
     and an accent hex or a section title that read from the left was the
     odd one out. The wizard's own full-width boxes are untouched -- a
     client_id typed from the right edge would be unreadable. */
  .fld input[type=text] { text-align: right; }
  .fld input[type=color] {
    width: 2.6rem; height: 2rem; padding: 0; background: var(--bg);
    border: 1px solid var(--line); border-radius: 6px;
    --ui-shadow-self: var(--ui-shadow-input);
    box-shadow: var(--ui-shadow-self) var(--ui-shadow-self) var(--ui-shadow-color);
  }
  .fld input[type=color]:focus { animation: ui-shadow-settle .45s ease-out forwards; }
  /* Right-indented, not left: the control column's other occupants (the
     number boxes, the selects, the color swatch) all end at its right
     edge, and a checkbox parked at the left edge was the one control in
     the form that didn't line up with anything. margin-left: auto pushes
     it over to join them. */
  /* No right margin of its own here: the column above already reserved
     the strip, and taking it twice would break the one line they share. */
  .fld input[type=checkbox] { margin: 0 0 var(--ui-shadow-check) auto; }
  .fld.off { opacity: .42; }
  .unit { color: var(--faint); font-size: .8rem; }
  .badge {
    font-size: .68rem; text-transform: uppercase; letter-spacing: .04em; color: var(--faint);
    border: 1px solid var(--line); border-radius: 999px; padding: .05rem .4rem; margin-left: .4rem;
  }
  /* Destructive on the left, the one you actually reach for on the right,
     so Restore to Default isn't sitting under the cursor that just pressed
     Save. */
  .acts { display: flex; gap: .6rem; flex-wrap: wrap; align-items: center;
          margin-top: 1.4rem; padding-top: 1.1rem; border-top: 1px solid var(--line); }
  .acts button { margin-top: 0; }
  /* A table, so the tick, the bank and the account line up down the page
     instead of every row laying itself out between two flex ends. Separate
     borders with a row gap rather than collapse: a collapsed table can't
     round its cells, and these rows want the same pill shape the bank list
     above them has. No column headers -- three columns of one word each
     don't need titling, and the section heading already says what the
     block is. */
  table.accts {
    width: 100%; margin: .3rem 0 .2rem; font-size: .9rem;
    border-collapse: separate; border-spacing: 0 .3rem;
  }
  table.accts td {
    background: var(--card-2); padding: .38rem .6rem; vertical-align: middle;
  }
  /* Raised like the layout rows, and flattening the same way when the row
     is picked: there the trigger is the drag, here it is the row's own
     tick, which is the only "selected" an account row has. The shadow goes
     on the cells rather than the <tr> because a row in a separate-border
     table has no box of its own to cast one. */
  table.accts tr > td {
    --ui-shadow-self: var(--ui-shadow-row);
    box-shadow: var(--ui-shadow-self) var(--ui-shadow-self) var(--ui-shadow-color);
  }
  /* The middle cells would otherwise cast onto their own neighbours, so
     only the last one in the row reaches sideways; the rest cast straight
     down, which is the part of the row edge they actually own. */
  table.accts tr > td:not(:last-child) {
    box-shadow: 0 var(--ui-shadow-self) var(--ui-shadow-color);
  }
  table.accts tr[data-selected] > td {
    animation: ui-shadow-settle .45s ease-out forwards;
  }
  table.accts tr[data-selected] > td:not(:last-child) {
    animation: ui-shadow-settle-down .45s ease-out forwards;
  }
  @keyframes ui-shadow-settle-down {
    from { box-shadow: 0 var(--ui-shadow-self) var(--ui-shadow-color); }
    to   { box-shadow: 0 0 var(--ui-shadow-color); }
  }
  @media (prefers-reduced-motion: reduce) {
    table.accts tr[data-selected] > td { animation: none; box-shadow: none; }
  }
  table.accts td:first-child {
    width: 1px; padding-right: 0; border-radius: 8px 0 0 8px;
  }
  table.accts td:last-child { border-radius: 0 8px 8px 0; }
  table.accts td:first-child input { display: block; }
  /* The bank name is the row's checkbox label, so it's a click target too
     rather than a 17px box being the only way to toggle the row. */
  table.accts .who label { display: block; margin: 0; font-size: inherit;
                           color: var(--text); cursor: pointer; }
  table.accts .what { color: var(--dim); text-align: right; white-space: nowrap; }
  table.accts .num4 { font-variant-numeric: tabular-nums; color: var(--faint); }
  /* Still listed, visibly not drawn: removing the row would make hiding an
     account look like unlinking the bank. Keyed off data-selected, which
     is the one thing the row's own checkbox sets. */
  table.accts tr:not([data-selected]) { opacity: .45; }
  table.accts tr:not([data-selected]) .what::after { content: ' (hidden)'; color: var(--faint); }

  /* Select-all sits in the section heading: the rows have no column
     headers for it to live in, and the heading is already the element that
     spans the group. */
  h3.grp > .pickall {
    display: flex; align-items: center; gap: .5rem; margin: 0;
    font: inherit; color: inherit; letter-spacing: inherit; cursor: pointer;
  }
  h3.grp > .pickall > .count {
    margin-left: auto; color: var(--faint);
    text-transform: none; letter-spacing: 0;
  }
  ol.order { color: var(--dim); font-size: .86rem; margin: .3rem 0 0; padding-left: 1.4rem; }
  ol.order code { color: var(--text); }

  /* ---- layout tab: drag to reorder ---- */
  ul.sortable { list-style: none; padding: 0; margin: .4rem 0 0; }
  ul.sortable li {
    display: flex; align-items: center; gap: .6rem; padding: .5rem .7rem;
    background: var(--card-2); border: 1px solid transparent; border-radius: 8px;
    margin-bottom: .35rem; font-size: .9rem; cursor: grab;
    --ui-shadow-self: var(--ui-shadow-row);
    box-shadow: var(--ui-shadow-self) var(--ui-shadow-self) var(--ui-shadow-color);
  }
  /* Every row sits raised, and the one you pick up is the one that drops:
     its distance closes to nothing as you move it, so the card you are
     carrying is the only flat thing in the list. */
  ul.sortable li.dragging {
    opacity: .4;
    animation: ui-shadow-settle .45s ease-out forwards;
  }
  @media (prefers-reduced-motion: reduce) {
    ul.sortable li.dragging { animation: none; box-shadow: none; }
  }
  /* The gap opens where the card would land, rather than a line that has to
     be read as "above" or "below" something. */
  ul.sortable li.dropAbove { border-top-color: var(--accent); }
  ul.sortable li.dropBelow { border-bottom-color: var(--accent); }
  ul.sortable li.hiddenAcct { opacity: .45; }
  .grip { color: var(--faint); cursor: grab; letter-spacing: -2px; user-select: none; }
  .sortable .who2 { flex: 1 1 auto; }
  .sortable .num4 { font-variant-numeric: tabular-nums; color: var(--faint); }
  .why {
    flex: 0 0 auto; font-size: .7rem; text-transform: uppercase; letter-spacing: .04em;
    color: var(--faint); border: 1px solid var(--line); border-radius: 999px;
    padding: .08rem .45rem; white-space: nowrap;
  }
  .why.pair { color: var(--dim); }
  .why.solo { color: var(--accent); border-color: var(--accent); }
  .why.off { color: var(--faint); }
</style>
</head>
<body>
<main>
  <h1>DesktopCards</h1>
  <p class="sub" id="subtitle">Setting things up.</p>

  <!-- ===================================================== manage view -->
  <nav class="tabs" id="tabs" role="tablist" hidden>
    <button class="tab on" data-tab="accounts" role="tab" aria-selected="true" aria-controls="tab-accounts">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
           stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 10h18M3 10l9-6 9 6M5 10v10h14V10"/></svg>
      Accounts
    </button>
    <button class="tab" data-tab="layout" role="tab" aria-selected="false" aria-controls="tab-layout">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
           stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 4h18v16H3z"/><path d="M3 10h18M10 10v10"/></svg>
      Layout
    </button>
    <button class="tab" data-tab="appearance" role="tab" aria-selected="false" aria-controls="tab-appearance">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
           stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M19.1 4.9 17 7M7 17l-2.1 2.1"/></svg>
      Appearance
    </button>
  </nav>

  <section id="tab-accounts" class="panel" hidden>
    <div class="grid" data-panel="accounts">

      <div class="card" data-first>
        <h3 class="grp">Linked banks</h3>
        <p class="hint2">
          Removing a bank forgets it here and leaves it linked at Plaid, so
          adding it back later is another sign-in &ndash; which on the free
          Trial plan spends another of your 10 banks rather than reusing the
          old one.
        </p>
        <ul class="banks" id="bankList"></ul>
        <p class="hint2" id="trialUse"></p>
        <p class="hint2" id="sandboxCreds" hidden>
          Sandbox: phone <code>+14155550138</code> (prefilled), code
          <code>123456</code>, then <code>user_good</code> /
          <code>pass_good</code> &ndash; or <code>user_loan_loc</code> for
          a credit card with a due date.
        </p>
        <!-- Hidden unless two or more profiles are enabled: with one
             there is nothing to choose, and an unnecessary question
             about a concept you are not using is worse than silence. -->
        <div class="row" id="linkTarget" hidden></div>
        <!-- The one button that stays out of the ribbon. It acts on this
             list rather than on the tab, and the ribbon is for the tab. -->
        <div class="row">
          <button class="solid" id="addBank">Add a bank</button>
        </div>
      </div>

      <div class="card">
        <h3 class="grp">Accounts on the widget</h3>
        <p class="hint2">
          Untick one to leave it off the widget &ndash; it stays linked,
          it just isn't drawn.
        </p>
        <div id="groups"></div>
      </div>

      <!-- Both boxes below are full width and sorted to the end: a
           profile row opens into a form, and the backup list is a log.
           Neither reads as a column of settings beside another. -->
      <div class="card wide" data-last>
        <h3 class="grp">Advanced</h3>
        <div class="check" style="margin-top:.3rem">
          <input type="checkbox" id="advToggle">
          <label for="advToggle" style="margin:0">Show/Add Plaid profiles</label>
        </div>
        <div id="advanced" hidden>
          <p class="hint2">
            A profile is one Plaid client ID in one environment. Tick the ones
            to draw. Sandbox runs on its own.
          </p>
          <div id="profileList"></div>
          <div class="foot">
            <div class="msg" id="advMsg"></div>
            <button id="addProfile">Add profile</button>
          </div>
        <!-- Same stacked shape as step 1, because this is a form you fill
             in rather than a row of settings you scan. -->
        <div class="narrow" id="newProfile" hidden>
          <div class="fields">
            <div class="field">
              <label for="npEnv">Environment</label>
              <select id="npEnv">
                <option value="production">production</option>
                <option value="sandbox">sandbox</option>
              </select>
            </div>
            <div class="field">
              <label for="npNick">Nickname (Optional)</label>
              <input type="text" id="npNick" autocomplete="off" spellcheck="false"
                     placeholder="work">
            </div>
            <div class="field">
              <label for="npClientId">Client ID</label>
              <input type="text" id="npClientId" autocomplete="off" spellcheck="false">
            </div>
            <div class="field">
              <label for="npSecret">Secret</label>
              <input type="password" id="npSecret" autocomplete="off" spellcheck="false">
            </div>
          </div>
          <div class="foot">
            <div class="msg" id="npMsg"></div>
            <div>
              <button id="cancelProfile">Cancel</button>
              <button class="solid" id="saveProfile">Add</button>
            </div>
          </div>
        </div>

        </div>
      </div>

      <div class="card wide" id="backupsCard" data-last hidden>
        <h3 class="grp">Backups</h3>
        <p class="hint2">
          A backup holds a profile's secret and its bank tokens, so
          restoring brings its banks back without spending Plaid Items.
          Taken automatically before unlink, reset and delete.
        </p>
        <div id="backupList"></div>
        <div class="foot">
          <div class="msg" id="bkMsg"></div>
          <button id="backupNow">Back up now</button>
        </div>
      </div>

    </div>
  </section>

  <section id="tab-layout" class="panel" hidden>
    <p class="hint">
      Drag a card to move it. Each widget section is its own list &ndash;
      which one a card lands in comes from the account type, not a
      preference. Everything here saves as soon as you change it.
    </p>
    <!-- One box per widget section, built by renderLayout: its heading,
         its row rules, then its cards. The rules used to be gathered in a
         column of their own so the lists were an uninterrupted run of
         drag targets; with a box per section they sit at the top of the
         box they belong to, which says the same thing more directly. -->
    <div class="grid" id="layoutSections" data-panel="layout"></div>
  </section>

  <section id="tab-appearance" class="panel" hidden>
    <p class="hint">
      The widget's settings from <code>appearance.json</code>, plus the
      Windows shortcuts &ndash; card order and section headings are on the
      Layout tab instead. Most of it
      takes effect the next time the widget's page loads; anything marked <span class="badge">restart</span>
      is read once at launch, so save it with <em>Save and Restart</em> from
      the arrow next to Save.
    </p>
    <div class="grid" id="appearanceForm" data-panel="appearance">
      <div class="card" id="shortcutsCard">
        <h3 class="grp">Shortcuts</h3>
        <p class="hint2" id="notInstalled" hidden>
          Not installed yet. Saving here, or Redo shortcuts, installs and
          writes them.
        </p>
        <div class="fld">
          <span class="lbl">Start with Windows
            <span class="sub2" id="pathStartup"></span></span>
          <span class="ctl"><input type="checkbox" class="switch" id="scStartup"></span>
        </div>
        <div class="fld">
          <span class="lbl">Start menu
            <span class="sub2" id="pathStartMenu"></span></span>
          <span class="ctl"><input type="checkbox" class="switch" id="scStartMenu"></span>
        </div>
        <div class="fld">
          <span class="lbl">Desktop shortcut
            <span class="sub2" id="pathDesktop"></span></span>
          <span class="ctl"><input type="checkbox" class="switch" id="scDesktop"></span>
        </div>
      </div>
    </div>
  </section>

  <!-- ====================================================== step 1 -->
  <section id="s1" class="active">
    <h2><span class="num">1</span> Plaid credentials</h2>
    <p class="hint">
      DesktopCards talks to your banks through
      <a href="https://dashboard.plaid.com" target="_blank" rel="noopener">Plaid</a>,
      and uses your own Plaid account rather than someone else's. Create one,
      then copy the client ID and secret from the dashboard's Keys page.
    </p>
    <p class="hint2">
      A new Plaid account starts on the free <strong>Trial plan</strong>,
      which covers both the things this widget reads &ndash; balances and
      credit card details &ndash; at no cost. It allows
      <strong>10 linked banks in total</strong>, and that total counts every
      bank you ever link: removing one here does not give the slot back. Link
      the banks you actually want and you will not run out, but there is no
      free way to undo a bank linked by mistake.
    </p>
    <p class="hint2">
      <strong>Already had a Plaid account before April 2026?</strong> It is on
      an older plan that asks you to apply for production access, and it
      cannot be moved onto the Trial plan. Make a new team instead &ndash;
      team switcher at the top left of the dashboard, then Teams, then Create
      team &ndash; and use that team's keys here. A team made today starts on
      Trial with no application to fill in.
    </p>
    <div class="body">
      <div class="fields">
        <div class="field">
          <label for="clientId">Client ID</label>
          <input type="text" id="clientId" autocomplete="off" spellcheck="false">
        </div>
        <div class="field">
          <label for="secret">Secret</label>
          <input type="password" id="secret" autocomplete="off" spellcheck="false">
        </div>
        <div class="field">
          <label for="environment">Environment</label>
          <select id="environment">
            <option value="production">production &ndash; your real accounts</option>
            <option value="sandbox">sandbox &ndash; Plaid's fake test bank</option>
          </select>
        </div>
      </div>
      <div class="foot">
        <div class="msg" id="credsMsg"></div>
        <button class="solid" id="saveCreds">Check and save</button>
      </div>
    </div>
  </section>

  <!-- ====================================================== step 2 -->
  <section id="s2" hidden>
    <h2><span class="num">2</span> Link a bank</h2>
    <p class="hint">
      Opens Plaid's own login screen over this page. You sign in to your
      bank directly &ndash; your bank password never passes through
      DesktopCards.
    </p>
    <div class="body">
      <!-- Sandbox only: Plaid's fake bank wants credentials like a real one,
           and they are not guessable. Shown in place rather than linked to
           the docs, because needing them is the one moment you cannot go
           and read anything. -->
      <div id="sandboxHelp" hidden>
        <h3 class="grp" style="margin-top:0">Sandbox test credentials</h3>
        <p class="hint2">
          Plaid's fake bank will ask for these. The phone number is already
          filled in for you &ndash; it picks the <em>OAuth Multi-item</em>
          profile, the busiest one, so the link runs through an OAuth
          hand-off and a bank with several accounts. Plaid masks it to the
          last four digits, so expect to see only <code>&bull;&bull;&bull;&bull; 0138</code>.
        </p>
        <p class="hint2">
          Plaid's own window suggests a different test number in a banner of
          its own. Ignore it &ndash; that banner is inside Plaid's window and
          nothing here can change it, and the number already in the box is
          the one that gets used.
        </p>
        <ul class="hint2" style="margin:.4rem 0 0; padding-left:1.1rem">
          <li>Phone &ndash; <code>+14155550138</code></li>
          <li>One-time code &ndash; <code>123456</code></li>
          <li>Username &ndash; <code>user_good</code></li>
          <li>Password &ndash; <code>pass_good</code></li>
          <li>
            For a credit card with a payment due date, link a second bank
            with <code>user_loan_loc</code> instead of
            <code>user_good</code>.
          </li>
        </ul>
      </div>
      <ul class="banks" id="linkedSoFar"></ul>
      <div class="foot">
        <div class="msg" id="linkMsg"></div>
        <button id="doneLinking" hidden>Done adding banks</button>
        <button class="solid" id="startLink">Open Plaid Link</button>
      </div>
    </div>
  </section>

  <!-- ====================================================== step 3 -->
  <section id="s3" hidden>
    <h2><span class="num">3</span> Check it works</h2>
    <p class="hint">
      Pulls your balances once, now, so anything wrong shows up here rather
      than as an empty widget later.
    </p>
    <div class="body">
      <div class="foot">
        <div class="msg" id="verifyMsg"></div>
        <button class="solid" id="verify">Fetch my balances</button>
      </div>
    </div>
  </section>

  <!-- ====================================================== step 4 -->
  <section id="s4" hidden>
    <h2><span class="num">4</span> Install</h2>
    <p class="hint">
      Puts DesktopCards in your Start menu so you can reopen it if you ever
      close it by accident.
    </p>
    <div class="body">
      <div class="check">
        <input type="checkbox" id="autostart" checked>
        <label for="autostart" style="margin:0">Also start it with Windows</label>
      </div>
      <div class="foot">
        <div class="msg" id="installMsg"></div>
        <button class="solid" id="install">Install</button>
      </div>
    </div>
  </section>

  <!-- ====================================================== step 5 -->
  <section id="s5" hidden>
    <h2><span class="num">5</span> Done</h2>
    <div class="body">
      <p class="hint" style="margin-left:0">
        The widget sits on your desktop and survives Win+D. Everything else
        lives in its system tray icon &ndash; reload, restart, add accounts, quit.
      </p>
      <div class="foot">
        <div class="msg" id="doneMsg"></div>
        <button id="quit">Close setup</button>
        <button class="solid" id="launch">Launch DesktopCards</button>
      </div>
    </div>
  </section>

  <footer id="where"></footer>
</main>

<!-- Every tab's buttons, latched to the foot of the window. Hidden until
     loadState knows this is the manage view: the five setup steps are a
     sequence, and each step's button belongs to that step. -->
<div class="savebar" id="savebar" hidden>
  <div class="inner">
    <div class="msg" id="manageMsg" data-bar="accounts"></div>
    <div class="msg" id="layoutMsg" data-bar="layout" hidden></div>
    <div class="msg" id="appearanceMsg" data-bar="appearance" hidden></div>

    <div class="acts" data-bar="accounts">
      <span class="menu-root">
        <span class="btn-group">
          <button class="solid" id="saveAccounts">Save</button>
          <button class="solid" id="saveAccountsMenuBtn" aria-haspopup="menu"
                  aria-expanded="false" aria-label="More save options">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                 stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="m6 9 6 6 6-6"/>
            </svg>
          </button>
        </span>
        <div class="menu" id="saveAccountsMenu" role="menu" hidden>
          <button role="menuitem" id="saveAccountsRestart">Reload and Restart</button>
        </div>
      </span>
    </div>

    <div class="acts" data-bar="layout">
      <button id="undoLayout" disabled>Undo last move</button>
      <button id="restartLayout">Restart Widget</button>
    </div>

    <div class="acts" data-bar="appearance">
      <button id="reinstall">Redo shortcuts</button>
      <button id="restoreAppearance">Restore to Default</button>
      <span class="menu-root">
        <span class="btn-group">
          <button class="solid" id="saveAppearance">Save</button>
          <button class="solid" id="saveMenuBtn" aria-haspopup="menu"
                  aria-expanded="false" aria-label="More save options">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                 stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
              <path d="m6 9 6 6 6-6"/>
            </svg>
          </button>
        </span>
        <div class="menu" id="saveMenu" role="menu" hidden>
          <button role="menuitem" id="saveRestart">Save and Restart</button>
          <button role="menuitem" id="saveReloadRestart">Reload and Restart</button>
        </div>
      </span>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
let STATE = {};

// ------------------------------------------------------- button loading
// A button that kicks off a request says so on itself, rather than only in
// the .msg line below the section -- the message is easy to miss, and the
// three slow operations here (saving, restarting the widget, rewriting the
// shortcuts) all look like nothing happened until they finish.
//
// The label is stashed on the element so a second loading() for the next
// stage of the same operation (Save, then Restart) doesn't lose the resting
// one, and so loaded() can restore it from any error path. disabled is set
// here too, so no handler has to remember to do both.
function loading(btn, text) {
  if (!btn) return;
  if (btn.dataset.label === undefined) btn.dataset.label = btn.textContent;
  if (text) btn.textContent = text;
  btn.classList.add('loading');
  btn.disabled = true;
  btn.setAttribute('aria-busy', 'true');
}

// Set a button's resting label, whether or not it's currently showing a
// loading one: while it is, the new label goes to the stash that loaded()
// will restore from instead of to the button.
function relabel(btn, text) {
  if (!btn) return;
  if (btn.dataset.label !== undefined) btn.dataset.label = text;
  else btn.textContent = text;
}

function loaded(btn) {
  if (!btn) return;
  if (btn.dataset.label !== undefined) {
    btn.textContent = btn.dataset.label;
    delete btn.dataset.label;
  }
  btn.classList.remove('loading');
  btn.disabled = false;
  btn.removeAttribute('aria-busy');
}

// ---------------------------------------------------------- menu plumbing
// One dropdown on the page (Save), so this is the whole of it: a trigger
// that toggles its menu, and a menu that closes on a click anywhere else
// or on Escape. A click on an item bubbles up to the menu and closes it,
// so the menu isn't left hanging open over the button it just started.
function wireMenu(triggerId, menuId) {
  const trigger = $(triggerId), menu = $(menuId);
  if (!trigger || !menu) return;
  const open = on => {
    menu.hidden = !on;
    trigger.setAttribute('aria-expanded', on ? 'true' : 'false');
  };
  trigger.onclick = e => { e.stopPropagation(); open(menu.hidden); };
  menu.onclick = () => open(false);
  document.addEventListener('click', e => {
    if (!menu.hidden && !menu.contains(e.target)) open(false);
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !menu.hidden) { open(false); trigger.focus(); }
  });
}

// ---------------------------------------------------- keeping the server
// This page is the only reason its server exists, so the server should go
// when the page does. Closing the tab used to leave the process running,
// and since the tray's Modify started a new one every click, they piled up.
//
// Two signals: a heartbeat saying "still here", and a beacon on the way out
// saying "gone". The server also exits on its own if neither has arrived
// for a few minutes, which covers the browser being killed outright.
let heartbeat = null;

function startHeartbeat(seconds) {
  if (heartbeat) clearInterval(heartbeat);
  const every = Math.max(5, seconds || 25) * 1000;
  heartbeat = setInterval(() => {
    fetch('/api/alive', { method: 'POST', keepalive: true }).catch(() => {});
  }, every);
}

// pagehide, not unload: it's the one browsers actually guarantee for a tab
// being closed, and it works with the back/forward cache. sendBeacon
// because a normal fetch is cancelled when the page goes away, which is
// precisely the moment this request has to survive.
window.addEventListener('pagehide', () => {
  // A Plaid OAuth bank login navigates this page away and returns to it
  // later. That fires pagehide, and it is emphatically not a close.
  if (linkOpen) return;
  try { navigator.sendBeacon('/api/closing'); } catch (e) {}
});

// Restored from the back/forward cache, so not gone after all. The server
// gives a closing page a few seconds' grace, and this lands inside it.
window.addEventListener('pageshow', () => {
  fetch('/api/alive', { method: 'POST' }).catch(() => {});
});

async function api(path, body) {
  const res = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let data = {};
  try { data = await res.json(); } catch (e) {}
  if (!res.ok) {
    const err = new Error(data.error || ('Request failed (' + res.status + ')'));
    // The rest of the body comes with it, so a caller can branch on a
    // flag the server set -- confirmable, say -- rather than matching on
    // the wording of the message.
    Object.assign(err, data);
    throw err;
  }
  return data;
}

function say(el, text, kind) {
  el.textContent = text || '';
  el.className = 'msg' + (kind ? ' ' + kind : '');
}

function step(id, state) {
  const el = $(id);
  el.hidden = state === 'hidden';
  el.classList.toggle('active', state === 'active');
  el.classList.toggle('done', state === 'done');
}

// How much of the free Trial allowance this Plaid account has spent.
// Informational only: a paid plan lifts the cap and nothing here can tell
// which plan the client_id is on, so this never blocks linking -- it just
// stops the 10-bank limit being a surprise discovered at the 11th bank.
function renderTrialUse() {
  const el = $('trialUse');
  if (!el) return;
  // Nothing to report in sandbox: its Items are unlimited and free, and
  // none of them touch the Production allowance this is counting.
  if (STATE.environment === 'sandbox') { el.hidden = true; return; }
  el.hidden = false;
  const cap = STATE.trial_item_cap || 10;
  // The team's spend, not this profile's: the sentence below names
  // the per-team allowance, so the number has to match it.
  const used = STATE.trial_items_used || STATE.items_created || 0;
  const left = Math.max(0, cap - used);
  const linked = (STATE.items || []).length;
  // Both numbers and nothing inferred from the gap between them: the
  // count is a local tally of Items ever created, Plaid has no API for it,
  // and it is hand-editable, so why it differs from what is linked now is
  // not something this can know. Says nothing about which plan either,
  // for the same reason.
  let text = used + ' of ' + cap + ' Plaid banks used';
  if (used > linked) text += ' (' + linked + ' linked now)';
  text += left
    ? '. Trial allows ' + cap + ' per Plaid team, for its lifetime.'
    : '. If you are still on Trial, linking another will be refused.';
  el.textContent = text;
  el.className = left <= 2 ? 'hint2 warn' : 'hint2';
}

// Every linked bank across the enabled profiles, so the Accounts tab
// lists what the widget actually draws rather than one profile's share of
// it. Labelled only when there are two or more to tell apart.
function allLinkedBanks() {
  const on = enabledProfiles();
  if (!on.length) return STATE.items || [];
  const many = on.length > 1;
  const out = [];
  for (const p of on) {
    for (const it of p.items || []) {
      out.push(Object.assign({}, it, { profile_label: many ? p.label : null }));
    }
  }
  return out;
}

function renderBanks(ul, items, removable) {
  ul.innerHTML = '';
  if (!items.length) {
    const li = document.createElement('li');
    li.className = 'none';
    li.textContent = 'No banks linked yet.';
    ul.append(li);
    return;
  }
  for (const it of items) {
    const li = document.createElement('li');
    const name = document.createElement('span');
    name.textContent = it.institution_name;
    li.append(name);
    if (it.profile_label) {
      const who = document.createElement('span');
      who.className = 'tail';
      who.textContent = '·' + it.profile_label;
      li.append(who);
    }
    if (removable) {
      const btn = document.createElement('button');
      btn.className = 'danger';
      btn.textContent = 'Remove';
      btn.onclick = async () => {
        btn.disabled = true;
        try {
          const r = await api('/api/remove', { item_id: it.item_id });
          STATE.items = r.items;
          if (r.items_created !== undefined) STATE.items_created = r.items_created;
          if (r.profiles) STATE.profiles = r.profiles;
          renderBanks(ul, allLinkedBanks(), removable);
          renderProfiles();
          renderLinkTarget();
          renderTrialUse();
          say($('manageMsg'), 'Removed. The widget picks that up on its next refresh.', 'ok');
        } catch (e) { say($('manageMsg'), e.message, 'bad'); btn.disabled = false; }
      };
      li.append(btn);
    }
    ul.append(li);
  }
}

const SECTION_LABELS = {
  depository: 'Checking & Savings', credit: 'Credit Cards', other: 'Other',
};

// The individual accounts behind the linked banks, split into the same
// sections the widget draws. Showing them here is what makes the bank list
// legible: "Chase" alone doesn't say whether it brought one checking
// account or three cards.
//
// No balances anywhere in this view, on purpose -- see account_summary.
// Bank on the left, what the account is on the right, and a checkbox for
// whether the widget draws it at all.
function renderGroups(groups) {
  const host = $('groups');
  host.innerHTML = '';
  const nonEmpty = (groups || []).filter(g => g.accounts.length);
  if (!nonEmpty.length) {
    const p = document.createElement('p');
    p.className = 'hint2';
    p.textContent = 'Nothing fetched yet. Link a bank, then refresh from the tray icon.';
    host.append(p);
    return;
  }
  for (const g of nonEmpty) {
    const rows = [];

    const h = document.createElement('h3');
    h.className = 'grp';
    h.style.marginTop = '1rem';

    const all = document.createElement('input');
    all.type = 'checkbox';
    all.setAttribute('aria-label', 'Show every account in this section');

    const count = document.createElement('span');
    count.className = 'count';

    const head = document.createElement('label');
    head.className = 'pickall';
    head.append(all, document.createTextNode(SECTION_LABELS[g.type] || g.type), count);
    h.append(head);
    host.append(h);

    // The header box and the count are derived from the rows every time
    // rather than tracked alongside them, so a failed request that rolls
    // one row back can't leave them disagreeing with it. indeterminate is
    // the partial state the <input> already knows how to draw.
    const syncHead = () => {
      const on = rows.filter(r => r.box.checked).length;
      all.checked = on === rows.length;
      all.indeterminate = on > 0 && on < rows.length;
      count.textContent = on + ' of ' + rows.length + ' shown';
    };

    const table = document.createElement('table');
    table.className = 'accts';
    const tbody = document.createElement('tbody');

    for (const a of g.accounts) {
      const tr = document.createElement('tr');

      const box = document.createElement('input');
      box.type = 'checkbox';
      // Needed as a label target, since a <label> can't wrap two cells the
      // way it wrapped the old single-row layout.
      box.id = 'acct:' + g.type + ':' + a.key;
      box.checked = !a.hidden;
      // Present means the widget draws it -- the row styling keys off this
      // one attribute rather than a class of its own.
      const mark = () => tr.toggleAttribute('data-selected', box.checked);
      mark();

      const row = { box: box, acct: a, mark: mark };
      rows.push(row);

      box.onchange = async () => {
        box.disabled = true;
        try {
          await api('/api/visibility', { key: a.key, type: g.type, visible: box.checked });
          a.hidden = !box.checked;
          mark();
          say($('manageMsg'),
            (box.checked ? 'Showing ' : 'Hiding ') + a.institution_name +
            '. The widget picks that up on its next refresh.', 'ok');
          // The Appearance tab prints the order lists, which this changes.
          APPEARANCE = null;
        } catch (e) {
          box.checked = !box.checked;
          say($('manageMsg'), e.message, 'bad');
        }
        box.disabled = false;
        syncHead();
      };

      const pick = document.createElement('td');
      pick.append(box);

      const who = document.createElement('td');
      who.className = 'who';
      const lab = document.createElement('label');
      lab.setAttribute('for', box.id);
      lab.textContent = a.institution_name;
      who.append(lab);
      if (a.profile_label) {
        const tag = document.createElement('span');
        tag.className = 'plab';
        tag.textContent = a.profile_label;
        who.append(tag);
      }

      const what = document.createElement('td');
      what.className = 'what';
      what.innerHTML = a.name + (a.mask ? ' <span class="num4">&bull;&bull;' + a.mask + '</span>' : '');

      tr.append(pick, who, what);
      tbody.append(tr);
    }

    // One request per account, in sequence: /api/visibility writes a single
    // key at a time, and stopping at the first failure with the rows that
    // did land left alone beats guessing which half of a section applied.
    all.onchange = async () => {
      const want = all.checked;
      const changing = rows.filter(r => r.box.checked !== want);
      if (!changing.length) { syncHead(); return; }
      for (const r of rows) r.box.disabled = true;
      all.disabled = true;
      let done = 0, failed = null;
      for (const r of changing) {
        try {
          await api('/api/visibility', { key: r.acct.key, type: g.type, visible: want });
          r.acct.hidden = !want;
          r.box.checked = want;
          r.mark();
          done++;
        } catch (e) { failed = e; break; }
      }
      for (const r of rows) r.box.disabled = false;
      all.disabled = false;
      syncHead();
      if (failed) {
        say($('manageMsg'), failed.message, 'bad');
      } else {
        say($('manageMsg'), (want ? 'Showing ' : 'Hiding ') + done + ' account' +
          (done === 1 ? '' : 's') + '. The widget picks that up on its next refresh.', 'ok');
        APPEARANCE = null;
      }
    };

    syncHead();
    table.append(tbody);
    host.append(table);
  }
}

// ------------------------------------------------------- packing the boxes
//
// CSS cannot do this on its own: in a two-column grid every row is as
// tall as its tallest box, so a short box next to a tall one is either
// stretched or trails a strip of empty page. The fix, from
// DesktopTasks' settings page: measure each box, round its height up to
// a whole number of --row-unit rows, and hand the grid that span, with
// `dense` backfilling the holes. Tallest first, which is what lets the
// big boxes claim their rows while there is still a free column beside
// them.
//
// Measuring needs the browser, so this is a class the script adds.
// Without it the grid still works -- just with the row heights CSS would
// have given it.
//
// Below the one-column breakpoint there is nothing to pack around, so
// binning is switched off rather than computed and ignored.
const TWO_COLUMN = window.matchMedia('(min-width: 901px)');

function gridMetrics() {
  const style = getComputedStyle(document.documentElement);
  return {
    gap: parseFloat(style.getPropertyValue('--gap')) || 16,
    unit: parseFloat(style.getPropertyValue('--row-unit')) || 4,
  };
}

function layoutGrid(grid) {
  const boxes = Array.from(grid.querySelectorAll(':scope > .card'))
    .filter(box => !box.hidden);
  for (const box of grid.querySelectorAll(':scope > .card[hidden]')) {
    box.style.removeProperty('--span');
    box.style.removeProperty('order');
  }
  if (!boxes.length) return;

  if (!TWO_COLUMN.matches) {
    grid.classList.remove('binned');
    for (const box of boxes) {
      box.style.removeProperty('--span');
      box.style.removeProperty('order');
    }
    return;
  }

  // Measured with the bins off, so each box is its own content's height
  // rather than the height of the bin it was last given. The column
  // widths are the same either way, so the measurement holds.
  grid.classList.remove('binned');
  const heights = boxes.map(box => box.getBoundingClientRect().height);

  const { gap, unit } = gridMetrics();
  boxes.forEach((box, i) => {
    // The gap rides along with every row but the last, so one row of
    // content is worth unit + gap pixels. Rounded up: a box short of its
    // bin shows a few pixels of its own background, a box over it would
    // have its last field clipped.
    box.style.setProperty('--span',
      Math.max(1, Math.ceil((heights[i] + gap) / (unit + gap))));
  });

  // order, not a DOM move: the markup stays in the order it reads in,
  // which is the order the tab key follows. Two exceptions to sorting by
  // height, both marked on the box itself:
  //
  //   data-first  stays at the head, so the box a tab is *about* is the
  //               one you read first -- packing put "Accounts on the
  //               widget" above "Linked banks" purely for being taller
  //   data-last   sorts to the end whatever its height, for the
  //               full-width boxes, which cannot sit beside anything
  boxes
    .map((box, i) => ({
      box, i, h: heights[i],
      first: box.hasAttribute('data-first'),
      last: box.hasAttribute('data-last'),
    }))
    .sort((a, b) => (b.first - a.first) || (a.last - b.last) ||
      // The pinned-to-the-end boxes keep the order they are written in.
      // They are stacked rather than side by side, so height buys
      // nothing there -- and Backups belongs under Advanced, not above
      // it for being the taller of the two.
      (a.last ? a.i - b.i : b.h - a.h))
    .forEach((entry, rank) => { entry.box.style.order = rank; });

  grid.classList.add('binned');
  evenColumnBottoms(grid, boxes, gap, unit);
}

// Squares off the bottom of the grid. Dense packing has already moved
// what it can, so a ragged edge left here is one no box can fill: the
// columns ran out of boxes, not out of room. The bottom box in the short
// column grows to the other column's bottom rather than leaving a strip
// of page under it -- it is mostly its own padding either way, and a box
// that ends where its neighbour ends reads as deliberate.
//
// Full-width boxes sit this out: one cannot be ragged against anything,
// and both of ours are pinned to the end.
function evenColumnBottoms(grid, boxes, gap, unit) {
  const gridRect = grid.getBoundingClientRect();
  const colWidth = (gridRect.width - gap) / 2;

  // Reading these forces the layout the spans just asked for, so the
  // positions are the packed ones rather than the previous pass's.
  const placed = boxes.map(box => {
    const rect = box.getBoundingClientRect();
    return {
      box,
      bottom: rect.bottom - gridRect.top,
      wide: rect.width > colWidth + gap / 2,
      // Which column, by where the box starts. Half a column of slack,
      // so sub-pixel column widths cannot flip the answer.
      col: rect.left - gridRect.left > colWidth / 2 ? 1 : 0,
    };
  });

  const narrow = placed.filter(p => !p.wide);
  if (narrow.length < 2) return;
  const target = Math.max(...narrow.map(p => p.bottom));

  for (const col of [0, 1]) {
    const inColumn = narrow.filter(p => p.col === col);
    if (!inColumn.length) continue;
    const last = inColumn.reduce((a, b) => (b.bottom > a.bottom ? b : a));
    // Both bottoms sit on row boundaries, so the distance between them is
    // a whole number of rows; rounding just absorbs sub-pixel error.
    const rows = Math.round((target - last.bottom) / (unit + gap));
    if (rows < 1) continue;
    const span = Number(last.box.style.getPropertyValue('--span')) || 1;
    last.box.style.setProperty('--span', span + rows);
  }
}

// Anything that changes what a box holds changes how tall it is, so this
// runs after every redraw rather than once on load. Coalesced to one
// pass per frame: a single load calls it from half a dozen draws, and
// each pass costs a forced reflow.
let relayoutQueued = false;

function relayout() {
  if (relayoutQueued) return;
  relayoutQueued = true;
  requestAnimationFrame(() => {
    relayoutQueued = false;
    for (const grid of document.querySelectorAll('.grid')) {
      if (grid.closest('section[hidden]')) continue;
      layoutGrid(grid);
    }
    // The spans and the order just written are mutations of these same
    // grids. Dropping the records keeps the watcher below from calling
    // this straight back -- the repack is not news.
    gridWatcher.takeRecords();
  });
}

// A dozen places redraw a box: the bank list, the account table, the
// profile rows, the backup log, a chevron opening a profile's fields.
// Rather than remember to repack from each of them, watch the grids
// themselves -- a box added, removed, hidden or re-rendered is a
// mutation, and that is exactly the set of events that changes a
// height.
const gridWatcher = new MutationObserver(relayout);
for (const grid of document.querySelectorAll('.grid')) {
  gridWatcher.observe(grid, {
    childList: true, subtree: true,
    attributes: true, attributeFilter: ['hidden', 'class'],
  });
}

// Column widths change with the window, and so does every wrapped line
// inside them.
window.addEventListener('resize', relayout);
// Text measured before the real font arrives is measured at the
// fallback's metrics, which is a box or two out by a line.
if (document.fonts && document.fonts.ready) document.fonts.ready.then(relayout);

function showTab(name) {
  for (const btn of document.querySelectorAll('.tab')) {
    const on = btn.dataset.tab === name;
    btn.classList.toggle('on', on);
    btn.setAttribute('aria-selected', on ? 'true' : 'false');
  }
  $('tab-accounts').hidden = name !== 'accounts';
  $('tab-layout').hidden = name !== 'layout';
  $('tab-appearance').hidden = name !== 'appearance';
  // The ribbon is per tab: only this tab's buttons and only its message.
  for (const el of document.querySelectorAll('.savebar [data-bar]')) {
    const on = el.dataset.bar === name;
    el.classList.toggle('on', on);
    if (el.classList.contains('msg')) el.hidden = !on;
  }
  // A hidden grid measures zero, so a tab is packed when it is shown and
  // not before.
  relayout();
  // Loaded on first open rather than up front: each is a file read, and
  // most visits to this page only want the Accounts tab. Reset to null by
  // anything that changes the file, so reopening re-reads.
  if (name === 'appearance' && !APPEARANCE) loadAppearance();
  if (name === 'layout') loadLayout();
}

for (const btn of document.querySelectorAll('.tab')) {
  btn.onclick = () => showTab(btn.dataset.tab);
}

// The page wears the widget's theme, so configuring it doesn't mean
// switching between a dark page and a light widget (or the reverse). One
// attribute, same as dashboard.html. The accent comes along too, since it's
// the color the widget is already using.
function applyPageTheme(dark, accent) {
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  if (accent) document.documentElement.style.setProperty('--accent', accent);
}

async function loadState() {
  STATE = await api('/api/state');
  applyPageTheme(STATE.dark_theme, STATE.accent);
  startHeartbeat(STATE.heartbeat_seconds);
  $('where').textContent = 'Settings and data: ' + STATE.data_dir;
  if (STATE.configured) {
    $('subtitle').textContent = 'Already set up. Change what you need.';
    document.querySelector('main').classList.add('manage');
    $('tabs').hidden = false;
    $('savebar').hidden = false;
    for (const s of ['s1','s2','s3','s4','s5']) step(s, 'hidden');
    showTab(document.querySelector('.tab.on').dataset.tab);
    fillShortcuts();
    renderProfiles();
    renderLinkTarget();
    renderBackups();
    renderBanks($('bankList'), allLinkedBanks(), true);
    renderTrialUse();
    $('sandboxCreds').hidden = STATE.environment !== 'sandbox';
    renderGroups(STATE.groups);
    // Every box above has just been filled, so their heights are only
    // now what they will be.
    relayout();
  } else {
    $('subtitle').textContent = 'Five steps, about two minutes.';
    document.querySelector('main').classList.remove('manage');
    $('tabs').hidden = true;
    $('savebar').hidden = true;
    $('tab-accounts').hidden = true;
    $('tab-layout').hidden = true;
    $('tab-appearance').hidden = true;
  }

  // Say what the button will actually do. Pressing it while a widget is
  // running restarts that one rather than starting a second.
  // relabel(), not textContent: a loadState() that lands while one of
  // these is mid-restart would otherwise write over the spinner's
  // "Restarting…" and then have loaded() put the stale old label back.
  // The menu option launches rather than restarts when nothing is running,
  // and /api/launch already does both -- so only the wording moves.
  relabel($('saveAccountsRestart'),
          STATE.running ? 'Reload and Restart' : 'Reload and Launch');
  relabel($('launch'), STATE.running ? 'Restart DesktopCards' : 'Launch DesktopCards');
}

// ---------------------------------------------------------------- step 1
$('saveCreds').onclick = async () => {
  const btn = $('saveCreds');
  loading(btn, 'Checking…');
  say($('credsMsg'), 'Checking with Plaid…');
  try {
    await api('/api/credentials', {
      client_id: $('clientId').value,
      secret: $('secret').value,
      environment: $('environment').value,
    });
    say($('credsMsg'), 'Credentials work.', 'ok');
    step('s1', 'done');
    step('s2', 'active');
    // Read from the form, not STATE: the environment was only just saved
    // and STATE is from page load, so it still says whatever was there
    // before (usually nothing, on a first run).
    $('sandboxHelp').hidden = $('environment').value !== 'sandbox';
    renderBanks($('linkedSoFar'), [], false);
  } catch (e) {
    say($('credsMsg'), e.message, 'bad');
  }
  loaded(btn);
};

// ---------------------------------------------------------------- step 2
let linked = [];
// True while Plaid's widget is up, so the pagehide handler can tell an
// OAuth redirect to the bank apart from the tab being closed.
let linkOpen = false;

async function runLink(msgEl, onDone) {
  say(msgEl, 'Opening Plaid…');
  let token;
  // Both declared out here on purpose: onSuccess below runs long after
  // this try block has ended, and it needs the same profile the token was
  // made for -- the bank has to be saved against the profile that asked
  // for it, not whichever one is selected by the time Plaid comes back.
  let target;
  try {
    target = linkTargetId();
    token = (await api('/api/link_token', { profile_id: target })).link_token;
  } catch (e) { say(msgEl, e.message, 'bad'); return; }

  linkOpen = true;
  Plaid.create({
    token,
    onSuccess: async (public_token, metadata) => {
      linkOpen = false;
      say(msgEl, 'Saving…');
      try {
        const r = await api('/api/exchange', {
          public_token,
          profile_id: target,
          institution_name: metadata.institution ? metadata.institution.name : 'Unknown',
          institution_id: metadata.institution ? metadata.institution.institution_id : null,
        });
        say(msgEl, 'Linked ' + r.institution_name + '.', 'ok');
        onDone(r);
      } catch (e) { say(msgEl, e.message, 'bad'); }
    },
    onExit: (err) => {
      linkOpen = false;
      say(msgEl, err ? ('Plaid: ' + (err.error_message || err.error_code))
                     : 'Cancelled – nothing was saved.', err ? 'bad' : null);
    },
  }).open();
}

$('startLink').onclick = () => runLink($('linkMsg'), (r) => {
  linked.push({ institution_name: r.institution_name });
  renderBanks($('linkedSoFar'), linked, false);
  $('doneLinking').hidden = false;
  $('startLink').textContent = 'Add another bank';
});

$('doneLinking').onclick = () => {
  step('s2', 'done');
  step('s3', 'active');
};

// ---------------------------------------------------------------- step 3
$('verify').onclick = async () => {
  const btn = $('verify');
  loading(btn, 'Fetching…');
  say($('verifyMsg'), 'Asking your banks… this can take a few seconds.');
  try {
    const r = await api('/api/verify', {});
    let text = 'Got ' + r.accounts + ' account' + (r.accounts === 1 ? '' : 's') + '.';
    if (r.errors && r.errors.length) text += ' Some banks reported: ' + r.errors.join('; ');
    say($('verifyMsg'), text, r.accounts > 0 ? 'ok' : 'bad');
    if (r.accounts > 0) { step('s3', 'done'); step('s4', 'active'); }
  } catch (e) {
    say($('verifyMsg'), e.message, 'bad');
  }
  loaded(btn);
};

// ---------------------------------------------------------------- step 4
$('install').onclick = async () => {
  const btn = $('install');
  loading(btn, 'Installing…');
  say($('installMsg'), 'Installing…');
  try {
    const r = await api('/api/install', { autostart: $('autostart').checked });
    const bits = [];
    if (r.app_path) bits.push('installed to ' + r.app_path);
    if (r.start_menu) bits.push('Start-menu shortcut created');
    if (r.startup) bits.push('will start with Windows');
    say($('installMsg'), bits.join(', ') + '.', 'ok');
    step('s4', 'done');
    step('s5', 'active');
  } catch (e) {
    say($('installMsg'), e.message, 'bad');
  }
  loaded(btn);
};

// ---------------------------------------------------------------- step 5
$('launch').onclick = async () => {
  const btn = $('launch');
  loading(btn, STATE.running ? 'Restarting…' : 'Starting…');
  say($('doneMsg'), 'Starting…');
  try {
    const r = await api('/api/launch', {});
    say($('doneMsg'), r.action === 'restarted'
      ? 'Restarted the running widget.'
      : 'Launched. Look for the icon in your system tray.', 'ok');
  } catch (e) { say($('doneMsg'), e.message, 'bad'); }
  loaded(btn);
};
$('quit').onclick = async () => {
  const btn = $('quit');
  btn.disabled = true;
  say($('doneMsg'), 'Stopping setup…');
  try {
    await api('/api/quit', {});
  } catch (e) {
    // The server stops moments after answering, so a dropped connection
    // here means it did exactly what was asked, not that anything failed.
  }
  say($('doneMsg'), 'Setup has stopped. You can close this tab.', 'ok');
  // Only permitted for a window script opened itself. The browser refuses
  // it for a tab webbrowser.open created, which is why the message above
  // says to close it rather than this being relied on.
  try { window.close(); } catch (e) {}
};

// ----------------------------------------------------------- manage view
$('addBank').onclick = () => runLink($('manageMsg'), async () => {
  await loadState();
});
// Pull fresh balances, and on the menu option restart the widget after.
// The button says Save because that is what is left for a button to do
// here: every tick on this tab writes as you change it, so bringing the
// numbers up to date is the only pending work.
//
// Same order as the Appearance tab's version and for the same reason: the
// widget reads data.json as it comes up, so fetching first means it draws
// the new balances immediately rather than the previous ones.
async function reloadWidget(restart) {
  const btn = $('saveAccounts'), chevron = $('saveAccountsMenuBtn');
  loading(btn, 'Reloading…');
  chevron.disabled = true;
  say($('manageMsg'), 'Fetching balances…');

  let accounts = null;
  try {
    const r = await api('/api/verify', {});
    accounts = r.accounts;
    if (r.errors && r.errors.length) {
      say($('manageMsg'), 'Fetched with ' + r.errors.length + ' error' +
        (r.errors.length === 1 ? '' : 's') + ': ' + r.errors.join('; '), 'bad');
    } else {
      say($('manageMsg'), 'Got ' + accounts + ' account' +
        (accounts === 1 ? '' : 's') + '.', 'ok');
    }
  } catch (e) {
    say($('manageMsg'), e.message, 'bad');
    loaded(btn); chevron.disabled = false;
    return;
  }

  if (!restart) {
    // The fetch can add or drop accounts, so the lists on this tab are
    // stale the moment it returns.
    loaded(btn); chevron.disabled = false; loadState();
    return;
  }

  loading(btn, 'Restarting…');
  try {
    const r = await api('/api/launch', {});
    say($('manageMsg'), r.action === 'restarted'
      ? 'Reloaded and restarted the widget.'
      : 'Reloaded. Launched the widget – look for the icon in your system tray.', 'ok');
  } catch (e) {
    say($('manageMsg'), 'Reloaded, but the restart failed: ' + e.message, 'bad');
  }
  // The restart takes a moment to come back up; re-read after it has, so
  // the tab reflects reality rather than the instant mid-restart.
  setTimeout(() => { loaded(btn); chevron.disabled = false; loadState(); }, 4000);
}

$('saveAccounts').onclick = () => reloadWidget(false);
$('saveAccountsRestart').onclick = () => reloadWidget(true);
wireMenu('saveAccountsMenuBtn', 'saveAccountsMenu');

// ------------------------------------------------------------- profiles
// A profile is one Plaid client ID in one environment. Several can be
// enabled at once, which is what puts work and personal accounts on one
// widget; enabling a sandbox profile switches the whole widget to test
// data, on its own.
$('advToggle').onchange = () => {
  const on = $('advToggle').checked;
  $('advanced').hidden = !on;
  // Backups is the same switch: it is the other half of what Advanced
  // holds, it is just its own box now.
  $('backupsCard').hidden = !on;
  relayout();
};

function applyProfiles(r) {
  if (r && r.backups) STATE.backups = r.backups;
  if (r && r.profiles) STATE.profiles = r.profiles;
  if (r && r.trial_item_cap) STATE.trial_item_cap = r.trial_item_cap;
  if (r && r.trial_items_used !== undefined) STATE.trial_items_used = r.trial_items_used;
  renderProfiles();
  renderLinkTarget();
  // Unlink, reset and delete each take a backup, so the list below has
  // changed by the time any of them come back.
  renderBackups();
  // The bank list and the Trial count come from the profiles too.
  // Leaving them stale made an unlink look like it had not happened.
  renderBanks($('bankList'), allLinkedBanks(), true);
  renderTrialUse();
}

function profileCount(p) {
  const cap = STATE.trial_item_cap || 10;
  const banks = (p.items || []).length;
  const label = banks + (banks === 1 ? ' bank' : ' banks');
  // Sandbox Items are free and unlimited, so counting them against a cap
  // would be inventing a limit that does not exist.
  if (p.environment === 'sandbox') {
    return label + ' · ' + p.items_created + ' test links';
  }
  return label + ' · ' + p.trial_items_used + ' of ' + cap + ' Items used';
}

function profileBody(p) {
  const wrap = document.createElement('div');
  const fields = document.createElement('div');
  fields.className = 'fields';
  fields.innerHTML =
    '<div class="field"><label>Nickname (Optional)</label>' +
    '<input type="text" class="pNick" autocomplete="off" spellcheck="false"></div>' +
    '<div class="field"><label>Client ID</label>' +
    '<input type="text" class="pClient" autocomplete="off" spellcheck="false">' +
    '<span class="sub2">Leave blank to keep the saved one</span></div>' +
    '<div class="field"><label>Secret</label>' +
    '<input type="password" class="pSecret" autocomplete="off" spellcheck="false">' +
    '<span class="sub2">Leave blank to keep the saved one</span></div>';
  const nick = fields.querySelector('.pNick');
  const client = fields.querySelector('.pClient');
  const secret = fields.querySelector('.pSecret');
  nick.value = p.nickname || '';
  // The saved credentials are never sent back to the page, so the field
  // shows only what the server will admit to: the last 4.
  client.placeholder = p.client_id_tail ? '••••••••' + p.client_id_tail : '';
  wrap.append(fields);

  const msg = document.createElement('div');
  msg.className = 'msg';
  wrap.append(msg);

  const acts = document.createElement('div');
  acts.className = 'acts';

  const save = document.createElement('button');
  save.className = 'solid';
  save.textContent = 'Save';
  save.onclick = async () => {
    loading(save, 'Checking…');
    try {
      const r = await api('/api/profiles/' + p.id + '/credentials', {
        nickname: nick.value,
        client_id: client.value,
        secret: secret.value,
      });
      client.value = '';
      secret.value = '';
      applyProfiles(r);
      say($('advMsg'), 'Saved. Your linked banks are untouched.', 'ok');
    } catch (e) {
      say(msg, e.message, 'bad');
    }
    loaded(save);
  };
  acts.append(save);

  const banks = (p.items || []).length;
  if (banks) {
    const unlink = document.createElement('button');
    unlink.className = 'danger';
    unlink.textContent = 'Unlink banks';
    unlink.onclick = async () => {
      // Said plainly, because it is the part people get wrong: removing a
      // bank here does not give a Plaid Item back.
      const warn = 'Unlink ' + banks + (banks === 1 ? ' bank' : ' banks') +
        ' from ' + p.label + '?\n\nA backup is saved first.' +
        (p.environment === 'sandbox' ? ''
          : '\n\nIts Items stay spent on Plaid. Re-linking costs new ones.');
      if (!confirm(warn)) return;
      unlink.disabled = true;
      try {
        applyProfiles(await api('/api/profiles/' + p.id + '/unlink', {}));
        say($('advMsg'), 'Unlinked. A backup was saved first.', 'ok');
      } catch (e) { say(msg, e.message, 'bad'); unlink.disabled = false; }
    };
    acts.append(unlink);
  }

  if (p.environment === 'sandbox') {
    const reset = document.createElement('button');
    reset.textContent = 'Reset sandbox';
    reset.onclick = async () => {
      if (!confirm('Reset ' + p.label + '?\n\nClears its test link count, ' +
                   'unlinks its banks and drops its cached balances. ' +
                   'Sandbox Items are free, so this costs nothing.')) return;
      reset.disabled = true;
      try {
        applyProfiles(await api('/api/profiles/' + p.id + '/reset', {}));
        say($('advMsg'), 'Sandbox reset.', 'ok');
      } catch (e) { say(msg, e.message, 'bad'); reset.disabled = false; }
    };
    acts.append(reset);
  }

  const del = document.createElement('button');
  del.className = 'danger';
  del.textContent = 'Delete profile';
  del.onclick = async () => {
    let warn = 'Delete ' + p.label + '?\n\nRemoves its saved secret, its ' +
      banks + (banks === 1 ? ' linked bank' : ' linked banks') +
      ' and its cached balances. A backup is saved first.';
    if (p.environment !== 'sandbox' && p.items_created) {
      warn += '\n\nIts Items stay spent on Plaid. Deleting does not give ' +
        'the slots back.';
    }
    if (!confirm(warn)) return;
    del.disabled = true;
    try {
      applyProfiles(await api('/api/profiles/' + p.id + '/delete', {}));
      say($('advMsg'), 'Deleted. A backup was saved first.', 'ok');
    } catch (e) { say(msg, e.message, 'bad'); del.disabled = false; }
  };
  acts.append(del);

  wrap.append(acts);
  return wrap;
}

async function setProfileEnabled(p, tick) {
  const want = tick.checked;
  tick.disabled = true;
  say($('advMsg'), want ? 'Checking with Plaid…' : 'Saving…');
  try {
    let r;
    try {
      r = await api('/api/profiles/' + p.id + '/enabled', { enabled: want });
    } catch (e) {
      // Plaid refused this profile's secret. Enabling anyway is exactly
      // what you want when the reason you are enabling it is to fix it,
      // so this asks rather than deciding.
      if (e.confirmable && confirm(e.message + '\n\nEnable it anyway?')) {
        r = await api('/api/profiles/' + p.id + '/enabled',
                      { enabled: want, force: true });
      } else {
        throw e;
      }
    }
    const off = (r.disabled || []).length;
    applyProfiles(r);
    say($('advMsg'), want
      ? 'Enabled.' + (off ? ' Sandbox runs alone, so the rest were switched off.' : '') +
        ' Restarting the widget and refreshing…'
      : 'Disabled. Restarting the widget…', 'ok');
  } catch (e) {
    tick.checked = !want;
    say($('advMsg'), e.message, 'bad');
  }
  tick.disabled = false;
}

function profileRow(p) {
  const cap = STATE.trial_item_cap || 10;
  const row = document.createElement('div');
  row.className = 'prof' + (p.enabled ? '' : ' off');

  const top = document.createElement('div');
  top.className = 'top';

  const tick = document.createElement('input');
  tick.type = 'checkbox';
  tick.checked = !!p.enabled;
  tick.disabled = !p.has_credentials;
  tick.onchange = () => setProfileEnabled(p, tick);
  top.append(tick);

  const name = document.createElement('span');
  name.className = 'name';
  name.textContent = p.label;
  top.append(name);

  // Only when a nickname is hiding them: the nickname is what you call
  // this profile, the environment and tail are what Plaid calls it, and
  // with two teams in the list you need both.
  if (p.nickname) {
    const tail = document.createElement('span');
    tail.className = 'tail';
    tail.textContent = p.environment +
      (p.client_id_tail ? ' ·' + p.client_id_tail : '');
    top.append(tail);
  }

  const count = document.createElement('span');
  count.className = 'count';
  count.textContent = profileCount(p);
  top.append(count);

  const chev = document.createElement('button');
  chev.className = 'chev';
  chev.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" ' +
    'aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';
  chev.title = 'Edit';
  top.append(chev);
  row.append(top);

  if (p.environment !== 'sandbox' && p.trial_items_used >= cap) {
    const note = document.createElement('p');
    note.className = 'cap';
    // Never claims which plan this team is on, because nothing here can
    // tell: Plaid has no API for the count or the plan.
    note.textContent = 'Trial allows ' + cap + ' per Plaid team for its ' +
      'lifetime. If you are still on Trial, new production links will be refused.';
    row.append(note);
  }

  const body = document.createElement('div');
  body.className = 'body';
  body.hidden = true;
  body.append(profileBody(p));
  chev.onclick = () => {
    body.hidden = !body.hidden;
    chev.classList.toggle('open', !body.hidden);
  };
  row.append(body);
  return row;
}

function renderProfiles() {
  const list = $('profileList');
  if (!list) return;
  list.innerHTML = '';
  const all = STATE.profiles || [];
  if (!all.length) {
    const none = document.createElement('p');
    none.className = 'hint2';
    none.textContent = 'No profiles yet.';
    list.append(none);
    return;
  }
  for (const p of all) list.append(profileRow(p));
}

// ---- adding one
$('addProfile').onclick = () => {
  $('newProfile').hidden = false;
  $('addProfile').hidden = true;
  say($('npMsg'), '');
};

$('cancelProfile').onclick = () => {
  $('newProfile').hidden = true;
  $('addProfile').hidden = false;
  for (const id of ['npNick', 'npClientId', 'npSecret']) $(id).value = '';
};

$('saveProfile').onclick = async () => {
  const btn = $('saveProfile');
  loading(btn, 'Checking…');
  say($('npMsg'), 'Checking with Plaid…');
  try {
    const r = await api('/api/profiles', {
      nickname: $('npNick').value,
      client_id: $('npClientId').value,
      secret: $('npSecret').value,
      environment: $('npEnv').value,
    });
    for (const id of ['npNick', 'npClientId', 'npSecret']) $(id).value = '';
    $('newProfile').hidden = true;
    $('addProfile').hidden = false;
    applyProfiles(r);
    say($('advMsg'), 'Added, switched off. Tick it to draw its accounts.', 'ok');
  } catch (e) {
    say($('npMsg'), e.message, 'bad');
  }
  loaded(btn);
};

// ---- which profile a new bank goes into
// Asked only when there is something to ask: with one profile enabled the
// Accounts tab looks exactly as it did before profiles existed.
function enabledProfiles() {
  return (STATE.profiles || []).filter(p => p.enabled && p.has_credentials);
}

function linkTargetId() {
  const picked = document.querySelector('input[name="linkTarget"]:checked');
  if (picked) return picked.value;
  const first = enabledProfiles()[0];
  return first ? first.id : null;
}

function renderLinkTarget() {
  const el = $('linkTarget');
  if (!el) return;
  const on = enabledProfiles();
  el.innerHTML = '';
  if (on.length < 2) { el.hidden = true; return; }
  el.hidden = false;
  const lead = document.createElement('span');
  lead.className = 'sub2';
  lead.textContent = 'Link into:';
  el.append(lead);
  on.forEach((p, i) => {
    const wrap = document.createElement('label');
    wrap.className = 'check';
    const radio = document.createElement('input');
    radio.type = 'radio';
    radio.name = 'linkTarget';
    radio.value = p.id;
    radio.checked = i === 0;
    wrap.append(radio);
    const text = document.createElement('span');
    text.textContent = p.label;
    wrap.append(text);
    el.append(wrap);
  });
}

// --------------------------------------------------------------- backups
// Restoring makes a new profile, switched off, rather than overwriting
// anything: the profile a backup came from may still be there.

function applyBackups(r) {
  if (r && r.backups) STATE.backups = r.backups;
  renderBackups();
}

function backupWhen(iso) {
  // The filename carries the timestamp too, but it is a filename. This
  // reads the date the backup says it was taken, and falls back to the
  // raw string rather than printing "Invalid Date".
  const d = new Date(iso);
  if (!iso || isNaN(d)) return iso || 'unknown';
  return d.toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' });
}

function backupWhat(b) {
  const bits = [b.label];
  bits.push(b.banks + (b.banks === 1 ? ' bank' : ' banks'));
  if (b.kind === 'manual') bits.push('manual');
  else if (b.kind === 'pre-migration') bits.push('before upgrade');
  else bits.push(b.kind.replace(/^before-/, 'before '));
  return bits.join(' · ');
}

function backupRow(b) {
  const row = document.createElement('div');
  row.className = 'bk';

  const when = document.createElement('span');
  when.className = 'when';
  when.textContent = backupWhen(b.taken);
  row.append(when);

  const what = document.createElement('span');
  what.className = 'what';
  what.textContent = backupWhat(b);
  row.append(what);

  const acts = document.createElement('div');
  acts.className = 'acts';

  if (b.restorable) {
    const restore = document.createElement('button');
    restore.textContent = 'Restore';
    restore.onclick = async () => {
      let warn = 'Restore ' + b.label + '?\n\nAdds it back as a new ' +
        'profile, switched off, with its ' + b.banks_restorable +
        (b.banks_restorable === 1 ? ' bank' : ' banks') + '.';
      if (b.environment !== 'sandbox') {
        warn += ' Its banks keep their existing Plaid Items, so this costs nothing.';
      }
      if (!confirm(warn)) return;
      loading(restore, 'Checking…');
      try {
        let r;
        try {
          r = await api('/api/backups/' + encodeURIComponent(b.name) + '/restore', {});
        } catch (e) {
          // A rotated secret is the usual reason, and the saved tokens are
          // still good, so restoring and then fixing the secret is the
          // way through rather than a dead end.
          if (e.confirmable && confirm(e.message + '\n\nRestore it anyway?')) {
            r = await api('/api/backups/' + encodeURIComponent(b.name) + '/restore',
                          { force: true });
          } else { throw e; }
        }
        applyProfiles(r);
        say($('bkMsg'), 'Restored, switched off. Tick it to draw its accounts.', 'ok');
      } catch (e) {
        say($('bkMsg'), e.message, 'bad');
      }
      loaded(restore);
    };
    acts.append(restore);
  } else {
    const no = document.createElement('span');
    no.className = 'sub2';
    no.textContent = 'not restorable';
    acts.append(no);
  }

  const del = document.createElement('button');
  del.className = 'danger';
  del.textContent = 'Delete';
  del.onclick = async () => {
    if (!confirm('Delete this backup of ' + b.label + '?\n\n' +
                 'Its saved bank tokens go with it.')) return;
    del.disabled = true;
    try {
      applyBackups(await api('/api/backups/' + encodeURIComponent(b.name) + '/delete', {}));
      say($('bkMsg'), 'Backup deleted.', 'ok');
    } catch (e) { say($('bkMsg'), e.message, 'bad'); del.disabled = false; }
  };
  acts.append(del);

  row.append(acts);
  return row;
}

function renderBackups() {
  const list = $('backupList');
  if (!list) return;
  list.innerHTML = '';
  const all = STATE.backups || [];
  if (!all.length) {
    const none = document.createElement('p');
    none.className = 'hint2';
    none.textContent = 'No backups yet.';
    list.append(none);
    return;
  }
  for (const b of all) list.append(backupRow(b));
}

$('backupNow').onclick = async () => {
  const btn = $('backupNow');
  loading(btn, 'Saving…');
  try {
    const r = await api('/api/backups', {});
    applyProfiles(r);
    say($('bkMsg'), 'Backed up ' + r.saved +
        (r.saved === 1 ? ' profile.' : ' profiles.'), 'ok');
  } catch (e) {
    say($('bkMsg'), e.message, 'bad');
  }
  loaded(btn);
};

// --------------------------------------------------------------- layout
// Order and row rules, one list per section. Section membership isn't
// editable: it comes from the Plaid account type, and the widget draws
// debit cards two-up and credit cards full width, so a card can't
// meaningfully move between them.
let LAYOUT = null;          // layout block from appearance.json
let LAYOUT_ROWS = null;     // {section: [account, ...]} in current order
const UNDO = [];            // {section, order} snapshots, newest last

function layoutBlock(section) {
  return (LAYOUT && LAYOUT[section]) || {};
}

function threshold(section, field, fallback) {
  const v = layoutBlock(section)[field];
  return Number.isFinite(v) && v > 0 ? v : fallback;
}

// The same pairing walk dashboard.js does (layoutDebitRows), over the cards
// that will actually be drawn, so the badge on a row says what the widget
// is really going to do with it. Hidden cards take no part: they aren't
// drawn, so they can't pair with anything.
function rowReasons(section) {
  const reasons = new Map();
  const cards = (LAYOUT_ROWS[section] || []);
  if (section !== 'depository') {
    // Credit cards are full width one per row by construction, so there's
    // nothing to explain and no badge worth the noise.
    for (const a of cards) if (a.hidden) reasons.set(a.key, ['off', 'hidden']);
    return reasons;
  }
  const digits = threshold(section, 'new_row_digit_threshold', 6);
  const nameMax = threshold(section, 'new_row_name_length_threshold', 12);
  let pending = null;
  const flush = () => {
    if (pending) reasons.set(pending.key, ['pair', 'half row']);
    pending = null;
  };
  for (const a of cards) {
    if (a.hidden) { reasons.set(a.key, ['off', 'hidden']); continue; }
    if (a.digits >= digits) {
      flush();
      reasons.set(a.key, ['solo', 'own row: big balance']);
    } else if (a.institution_name.length > nameMax) {
      flush();
      reasons.set(a.key, ['solo', 'own row: long name']);
    } else if (pending) {
      reasons.set(pending.key, ['pair', 'two-up']);
      reasons.set(a.key, ['pair', 'two-up']);
      pending = null;
    } else {
      pending = a;
    }
  }
  flush();
  return reasons;
}

function renderLayout() {
  const host = $('layoutSections');
  host.innerHTML = '';
  // A box per widget section: its heading, its row rules, then its cards.
  const sectionCard = section => {
    const box = document.createElement('div');
    box.className = 'card';
    const h = document.createElement('h3');
    h.className = 'grp';
    h.textContent = SECTION_LABELS[section];
    box.append(h);
    host.append(box);
    return box;
  };

  for (const section of ['depository', 'credit']) {
    const cards = LAYOUT_ROWS[section] || [];

    const list = sectionCard(section);
    list.append(layoutField(section, 'title', 'Heading shown on the widget', 'text'));
    if (section === 'depository') {
      list.append(layoutField(section, 'new_row_digit_threshold',
        'Own row at this many balance digits', 'int', 'Default 6, i.e. $100,000+'));
      list.append(layoutField(section, 'new_row_name_length_threshold',
        'Own row past this many name characters', 'int', 'Default 12'));
    }

    if (!cards.length) {
      const p = document.createElement('p');
      p.className = 'hint2';
      p.textContent = 'Nothing in this section yet.';
      list.append(p);
      continue;
    }

    const reasons = rowReasons(section);
    const ul = document.createElement('ul');
    ul.className = 'sortable';
    ul.dataset.section = section;
    cards.forEach((a, i) => ul.append(sortableRow(section, a, i, reasons.get(a.key))));
    list.append(ul);
  }
  $('undoLayout').disabled = UNDO.length === 0;
  relayout();
}

function layoutField(section, field, label, type, sub) {
  const row = document.createElement('div');
  row.className = 'fld';
  const lbl = document.createElement('span');
  lbl.className = 'lbl';
  lbl.innerHTML = label + (sub ? '<span class="sub2">' + sub + '</span>' : '');
  const ctl = document.createElement('span');
  ctl.className = 'ctl';
  const input = document.createElement('input');
  input.type = type === 'int' ? 'number' : 'text';
  if (type === 'int') input.min = 1;
  input.value = layoutBlock(section)[field] ?? '';
  input.id = 'lay:' + section + '.' + field;
  input.onchange = async () => {
    const value = type === 'int' ? parseInt(input.value, 10) : input.value;
    if (type === 'int' && !Number.isFinite(value)) return;
    try {
      const r = await api('/api/layout', { type: section, patch: { [field]: value } });
      LAYOUT = r.layout;
      // Row badges are computed from these two numbers, so they have to be
      // redrawn when one changes, not just on the next load.
      renderLayout();
      say($('layoutMsg'), 'Saved.', 'ok');
    } catch (e) { say($('layoutMsg'), e.message, 'bad'); }
  };
  ctl.append(input);
  row.append(lbl, ctl);
  return row;
}

let dragFrom = null;

function sortableRow(section, acc, index, reason) {
  const li = document.createElement('li');
  li.draggable = true;
  li.dataset.index = index;
  li.classList.toggle('hiddenAcct', !!acc.hidden);

  const grip = document.createElement('span');
  grip.className = 'grip';
  grip.textContent = '⠇⠇';

  const who = document.createElement('span');
  who.className = 'who2';
  who.innerHTML = acc.institution_name +
    (acc.mask ? ' <span class="num4">&bull;&bull;' + acc.mask + '</span>' : '') +
    (acc.profile_label
      ? ' <span class="plab">' + acc.profile_label + '</span>' : '');

  li.append(grip, who);
  if (reason) {
    const why = document.createElement('span');
    why.className = 'why ' + reason[0];
    why.textContent = reason[1];
    li.append(why);
  }

  li.ondragstart = e => {
    dragFrom = { section, index };
    li.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    // Firefox refuses to start a drag without data on the transfer.
    e.dataTransfer.setData('text/plain', acc.key);
  };
  li.ondragend = () => {
    li.classList.remove('dragging');
    clearDropMarks();
    dragFrom = null;
  };
  li.ondragover = e => {
    // Only within the same section: a credit card has no meaning in the
    // debit list, and dropping across would silently do nothing.
    if (!dragFrom || dragFrom.section !== section) return;
    e.preventDefault();
    clearDropMarks();
    const box = li.getBoundingClientRect();
    const below = e.clientY > box.top + box.height / 2;
    li.classList.add(below ? 'dropBelow' : 'dropAbove');
  };
  li.ondragleave = () => li.classList.remove('dropAbove', 'dropBelow');
  li.ondrop = e => {
    if (!dragFrom || dragFrom.section !== section) return;
    e.preventDefault();
    const box = li.getBoundingClientRect();
    const below = e.clientY > box.top + box.height / 2;
    moveCard(section, dragFrom.index, index + (below ? 1 : 0));
  };
  return li;
}

function clearDropMarks() {
  for (const el of document.querySelectorAll('.dropAbove, .dropBelow')) {
    el.classList.remove('dropAbove', 'dropBelow');
  }
}

// Splice semantics: `to` is the slot the card should end up occupying once
// it has been lifted out, which is why it's decremented when moving down.
function moveCard(section, from, to) {
  const cards = LAYOUT_ROWS[section];
  if (from === to || from === to - 1) return;
  const before = cards.map(a => a.key);
  const [card] = cards.splice(from, 1);
  cards.splice(to > from ? to - 1 : to, 0, card);
  UNDO.push({ section, order: before });
  renderLayout();
  saveOrder(section, 'Moved ' + card.institution_name + '.');
}

async function saveOrder(section, message) {
  const order = LAYOUT_ROWS[section].map(a => a.key);
  try {
    const r = await api('/api/layout', { type: section, patch: { order } });
    LAYOUT = r.layout;
    // The Appearance tab holds a whole copy of appearance.json; drop it so
    // it reloads rather than saving a stale layout back over this.
    APPEARANCE = null;
    say($('layoutMsg'), message + ' The widget picks it up on its next refresh.', 'ok');
  } catch (e) {
    say($('layoutMsg'), e.message, 'bad');
  }
}

$('undoLayout').onclick = () => {
  const last = UNDO.pop();
  if (!last) return;
  // saveOrder is fire-and-forget from here, so there's nothing to wait on
  // past the re-render; the button is disabled again by renderLayout when
  // the undo stack empties.
  const byKey = new Map(LAYOUT_ROWS[last.section].map(a => [a.key, a]));
  LAYOUT_ROWS[last.section] = last.order.map(k => byKey.get(k)).filter(Boolean);
  renderLayout();
  saveOrder(last.section, 'Move undone.');
};

$('restartLayout').onclick = async () => {
  const btn = $('restartLayout');
  loading(btn, 'Restarting…');
  say($('layoutMsg'), 'Restarting…');
  try {
    const r = await api('/api/launch', {});
    say($('layoutMsg'), r.action === 'restarted'
      ? 'Restarted the widget.'
      : 'Launched. Look for the icon in your system tray.', 'ok');
    setTimeout(() => { loaded(btn); loadState(); }, 4000);
    return;
  } catch (e) { say($('layoutMsg'), e.message, 'bad'); }
  loaded(btn);
};

async function loadLayout() {
  try {
    const [ap, st] = [await api('/api/appearance'), STATE];
    LAYOUT = ap.appearance.layout || {};
    LAYOUT_ROWS = {};
    for (const section of ['depository', 'credit']) {
      const accounts = (st.groups || []).find(g => g.type === section);
      const cards = accounts ? accounts.accounts.slice() : [];
      // Shown in the saved order, with anything the order doesn't mention
      // appended -- the same rule dashboard.js's applyOrder follows.
      const pos = new Map((layoutBlock(section).order || []).map((k, i) => [k, i]));
      cards.sort((a, b) => (pos.has(a.key) ? pos.get(a.key) : 1e6) -
                           (pos.has(b.key) ? pos.get(b.key) : 1e6));
      LAYOUT_ROWS[section] = cards;
    }
    UNDO.length = 0;
    renderLayout();
    say($('layoutMsg'), '');
  } catch (e) {
    say($('layoutMsg'), e.message, 'bad');
  }
}

// ----------------------------------------------------------- appearance
// One spec rather than twenty hand-written rows: every field is one of a
// handful of shapes, and keeping them in appearance.json's own order is
// what makes the editor and the file recognisably the same thing.
//
//   dep     only meaningful while another field is on, so it greys out
//   restart read once at launch by widget.py, so reloading the widget's
//           page isn't enough to apply it
const APPEARANCE_FIELDS = [
  { grp: 'Color and size' },
  { path: 'accent', label: 'Accent color', type: 'color',
    sub: 'Copied from your Windows accent at setup' },
  { path: 'corner_radius', label: 'Corner radius', type: 'int', unit: 'px' },
  { path: 'scale', label: 'Scale', type: 'float', step: 0.05, sub: '1.0 is default size' },

  { grp: 'Spacing' },
  { path: 'extra_spacing', label: 'Extra spacing', type: 'int', unit: 'px',
    sub: 'All four sides; negative pulls content tighter' },
  { path: 'custom_spacing', label: 'Use per-side spacing instead', type: 'bool' },
  { path: 'spacing.left', label: 'Left', type: 'int', unit: 'px', dep: 'custom_spacing' },
  { path: 'spacing.top', label: 'Top', type: 'int', unit: 'px', dep: 'custom_spacing' },
  { path: 'spacing.right', label: 'Right', type: 'int', unit: 'px', dep: 'custom_spacing' },
  { path: 'spacing.bottom', label: 'Bottom', type: 'int', unit: 'px', dep: 'custom_spacing' },

  { grp: 'Theme' },
  { path: 'dark_theme', label: 'Dark theme', type: 'bool' },
  { path: 'translucent_background', label: 'Translucent background', type: 'bool', restart: true },
  { path: 'opacity', label: 'Opacity', type: 'float', control: 'slider',
    step: 0.05, min: 0.15, max: 1,
    dep: 'translucent_background', sub: '0.15 to 1; exactly 1 turns it off' },

  { grp: 'Card shadow' },
  { path: 'shadow.enabled', label: 'Drop shadow', type: 'bool' },
  { path: 'shadow.offset', label: 'Offset', type: 'int', unit: 'px', dep: 'shadow.enabled' },
  { path: 'shadow.border', label: 'Border', type: 'int', unit: 'px', dep: 'shadow.enabled' },
  // optional: blank is a meaningful value here (the key goes away and the
  // widget falls back to the theme), which is why the hex box and not the
  // swatch is what gets saved -- a swatch has no way to be blank.
  { path: 'shadow.color', label: 'Color', type: 'color', optional: true,
    dep: 'shadow.enabled',
    placeholder: 'theme default', sub: 'Blank follows the theme' },

  { grp: 'Window' },
  { path: 'window.x', label: 'X position', type: 'int', unit: 'px', restart: true },
  { path: 'window.y', label: 'Y position', type: 'int', unit: 'px', restart: true },
  { path: 'window.survive_show_desktop', label: 'Stay visible through Win+D',
    type: 'bool', restart: true },
  // Section headings, row rules and card order all live in the Layout tab.
  // Deliberately not duplicated here: two controls writing one value means
  // one of them is always showing something stale.
];

let APPEARANCE = null;
let APPEARANCE_DEFAULTS = null;

const getPath = (obj, path) =>
  path.split('.').reduce((o, k) => (o === undefined || o === null ? undefined : o[k]), obj);

function setPath(obj, path, value) {
  const keys = path.split('.');
  const last = keys.pop();
  let cur = obj;
  for (const k of keys) {
    if (typeof cur[k] !== 'object' || cur[k] === null) cur[k] = {};
    cur = cur[k];
  }
  cur[last] = value;
}

// A hex triple is the only thing a native swatch can hold, so it's also
// the only thing worth copying into one.
const HEX6 = /^#[0-9a-f]{6}$/i;

// path -> the type=color input beside that field's hex box. Kept here
// rather than given an id of its own, so 'ap:<path>' stays the one id per
// field and mainInput() can find the value-bearing input unambiguously.
const COLOR_SWATCH = {};

// The input carrying ap:<path>, i.e. the one holding the value: the
// control itself for a plain field, the hex box or the track for the
// composed ones.
const mainInput = node =>
  node.tagName === 'INPUT' ? node : node.querySelector('.ap-main');

function colorField(f) {
  const wrap = document.createElement('span');
  wrap.className = 'cpick';

  const hex = document.createElement('input');
  hex.type = 'text';
  hex.className = 'cpick-hex ap-main';
  hex.id = 'ap:' + f.path;
  hex.spellcheck = false;
  hex.autocomplete = 'off';
  if (f.placeholder) hex.placeholder = f.placeholder;

  const swatch = document.createElement('input');
  swatch.type = 'color';
  swatch.setAttribute('aria-label', f.label + ' picker');
  COLOR_SWATCH[f.path] = swatch;

  // Typing a hex moves the swatch; picking a color writes the hex. input,
  // not change, on the swatch: the OS picker streams as you drag it, and
  // the box should keep up rather than jump once at the end.
  hex.oninput = () => syncColor(f.path);
  swatch.oninput = () => { hex.value = swatch.value; syncColor(f.path); };

  wrap.append(hex, swatch);

  // Getting back to "follow the theme" shouldn't mean selecting the box
  // and deleting it -- once the picker has been opened the box always has
  // a color in it.
  if (f.optional) {
    const clear = document.createElement('button');
    clear.type = 'button';
    clear.className = 'cpick-clear';
    clear.textContent = '×';
    clear.title = 'Follow the theme instead';
    clear.setAttribute('aria-label', 'Follow the theme instead');
    clear.onclick = () => { hex.value = ''; syncColor(f.path); };
    wrap.append(clear);
  }
  return wrap;
}

// Swatch and the .unset mark follow the box, never the other way round.
function syncColor(path) {
  const hex = $('ap:' + path), swatch = COLOR_SWATCH[path];
  if (!hex || !swatch) return;
  const text = hex.value.trim();
  hex.parentNode.classList.toggle('unset', !text);
  if (HEX6.test(text)) swatch.value = text.toLowerCase();
}

// The number box, not the track, is the field's value. A range input
// quantises whatever it's given to its own step, so routing the saved
// number through one would turn a typed 0.83 into 0.85 behind the user's
// back; this way the track is a way of setting the box and the box is what
// gets read.
function sliderField(f) {
  const wrap = document.createElement('span');
  wrap.className = 'slider';
  const step = f.type === 'int' ? 1 : (f.step || 0.05);

  const track = document.createElement('input');
  track.type = 'range';
  track.step = step;
  track.setAttribute('aria-label', f.label);

  const num = document.createElement('input');
  num.type = 'number';
  num.className = 'slider-num ap-main';
  num.id = 'ap:' + f.path;
  num.step = step;

  for (const el of [track, num]) {
    if (f.min !== undefined) el.min = f.min;
    if (f.max !== undefined) el.max = f.max;
  }

  track.oninput = () => { num.value = track.value; syncSlider(f.path); };
  // Nothing is clamped while it's being typed -- on the way to "0.15" you
  // pass through "0.1", and snapping that up to the minimum mid-keystroke
  // makes the box impossible to type into. The track just doesn't follow
  // until the number is one it can hold.
  num.oninput = () => syncSlider(f.path);
  // addEventListener, not .onchange: buildAppearanceForm puts refreshDeps
  // on the main input's onchange, which would replace this.
  num.addEventListener('change', () => {
    const v = parseFloat(num.value);
    const min = parseFloat(num.min), max = parseFloat(num.max);
    if (!Number.isFinite(v)) num.value = Number.isFinite(min) ? min : 0;
    else if (Number.isFinite(min) && v < min) num.value = min;
    else if (Number.isFinite(max) && v > max) num.value = max;
    syncSlider(f.path);
  });

  // The box first: it's the value, and starting the row with it puts its
  // left edge where every other control's is.
  wrap.append(num, track);
  return wrap;
}

// Track position and fill follow the box, never the other way round.
function syncSlider(path) {
  const num = $('ap:' + path);
  if (!num) return;
  const track = num.parentNode.querySelector('input[type=range]');
  if (!track) return;
  const v = parseFloat(num.value);
  if (!Number.isFinite(v)) return; // mid-typing: leave the track where it is
  const min = parseFloat(track.min) || 0, max = parseFloat(track.max);
  track.value = v;
  // The filled part of the track, computed from the box rather than from
  // track.value, which the browser has just snapped to the nearest step --
  // so the fill ends at the number actually showing.
  if (Number.isFinite(max) && max > min) {
    const pct = Math.min(100, Math.max(0, (v - min) / (max - min) * 100));
    track.style.setProperty('--pct', pct.toFixed(2) + '%');
  }
}

// Removes a key rather than blanking it -- an absent key is what the
// widget reads as "no preference saved here".
function delPath(obj, path) {
  const keys = path.split('.');
  const last = keys.pop();
  const parent = keys.length ? getPath(obj, keys.join('.')) : obj;
  if (parent) delete parent[last];
}

function inputFor(f) {
  if (f.type === 'color') return colorField(f);
  if (f.control === 'slider') return sliderField(f);

  const el = document.createElement('input');
  // Every boolean in this form is "the widget does this, or it doesn't",
  // which is a switch. See the .switch rules in the stylesheet.
  if (f.type === 'bool') { el.type = 'checkbox'; el.className = 'switch'; }
  else if (f.type === 'int' || f.type === 'float') {
    el.type = 'number';
    el.step = f.type === 'int' ? 1 : (f.step || 0.05);
    if (f.min !== undefined) el.min = f.min;
    if (f.max !== undefined) el.max = f.max;
  } else {
    el.type = 'text';
    if (f.placeholder) el.placeholder = f.placeholder;
  }
  el.id = 'ap:' + f.path;
  el.classList.add('ap-main');
  return el;
}

function buildAppearanceForm() {
  const host = $('appearanceForm');
  // Everything except the hand-written Shortcuts box, which is markup
  // rather than a generated field and holds ticks that are pending.
  for (const el of [...host.children]) {
    if (el.id !== 'shortcutsCard') el.remove();
  }
  // Each heading in APPEARANCE_FIELDS opens a box, and its fields go in
  // there. The list's first entry is a heading, so no field can land
  // outside one -- the fallback below is for a hand-added field that
  // forgets that, which belongs on the page rather than nowhere.
  let block = null;
  const into = el => (block || host).append(el);
  for (const f of APPEARANCE_FIELDS) {
    if (f.grp) {
      block = document.createElement('div');
      block.className = 'card';
      const h = document.createElement('h3');
      h.className = 'grp';
      h.textContent = f.grp;
      block.append(h);
      host.append(block);
      continue;
    }
    const row = document.createElement('div');
    row.className = 'fld';
    row.dataset.path = f.path;
    if (f.dep) row.dataset.dep = f.dep;

    const lbl = document.createElement('span');
    lbl.className = 'lbl';
    lbl.innerHTML = f.label + (f.restart ? '<span class="badge">restart</span>' : '') +
      (f.sub ? '<span class="sub2">' + f.sub + '</span>' : '');

    const ctl = document.createElement('span');
    ctl.className = 'ctl';
    const node = inputFor(f);
    // A picker or a slider is a wrapper around its input, so the handler
    // goes on the input rather than on the span that holds it.
    mainInput(node).onchange = refreshDeps;
    ctl.append(node);
    if (f.unit) {
      const u = document.createElement('span');
      u.className = 'unit';
      u.textContent = f.unit;
      ctl.append(u);
    }
    row.append(lbl, ctl);
    into(row);
  }
  relayout();
}

function fillAppearanceForm(source) {
  for (const f of APPEARANCE_FIELDS) {
    if (f.grp) continue;
    const el = $('ap:' + f.path);
    const v = getPath(source, f.path);
    if (f.type === 'bool') el.checked = !!v;
    else if (f.type === 'color') {
      // An optional color with nothing saved stays blank -- that blank is
      // the "follow the theme" the field means. Only a required one needs
      // a value to fall back on.
      el.value = v || (f.optional ? '' : '#0078d4');
      // Seeded to the stylesheet's own shadow default, so opening an unset
      // picker starts from the color the widget would actually have drawn.
      if (f.optional && !v) COLOR_SWATCH[f.path].value = '#323232';
      syncColor(f.path);
    }
    else {
      el.value = v === undefined || v === null ? '' : v;
      if (f.control === 'slider') syncSlider(f.path);
    }
  }
  refreshDeps();
}

// A dependent field is greyed rather than hidden: it still has a value in
// the file, and hiding it would make the form look like the setting had
// gone away rather than being inactive.
function refreshDeps() {
  for (const row of document.querySelectorAll('.fld[data-dep]')) {
    const master = $('ap:' + row.dataset.dep);
    const on = master ? master.checked : true;
    row.classList.toggle('off', !on);
    for (const el of row.querySelectorAll('input, select, button')) el.disabled = !on;
  }
}

// Collected onto the object that was loaded, so keys this editor doesn't
// know about (anything hand-added, or added by a later version) survive a
// save instead of being dropped.
function collectAppearance() {
  const out = JSON.parse(JSON.stringify(APPEARANCE));
  for (const f of APPEARANCE_FIELDS) {
    if (f.grp) continue;
    const el = $('ap:' + f.path);
    if (f.type === 'bool') { setPath(out, f.path, el.checked); continue; }
    if (f.type === 'int' || f.type === 'float') {
      const n = f.type === 'int' ? parseInt(el.value, 10) : parseFloat(el.value);
      setPath(out, f.path, Number.isFinite(n) ? n : 0);
      continue;
    }
    const text = el.value.trim();
    if (!text) {
      // An empty optional color means "follow the theme", which is the key
      // being absent -- writing "" would pin every card's shadow to an
      // invalid color instead.
      if (f.optional) { delPath(out, f.path); continue; }
      // Blank where blank means nothing: take the swatch, which can't hold
      // an invalid value, rather than saving an empty accent.
      if (f.type === 'color') { setPath(out, f.path, COLOR_SWATCH[f.path].value); continue; }
    }
    // Saved exactly as typed, hex or not: appearance.json is allowed any
    // CSS color, and re-reading one through the swatch would quietly turn
    // a "black" someone wrote by hand into "#000000".
    setPath(out, f.path, text);
  }
  return out;
}

async function loadAppearance() {
  try {
    const r = await api('/api/appearance');
    APPEARANCE = r.appearance;
    APPEARANCE_DEFAULTS = r.defaults;
    buildAppearanceForm();
    fillAppearanceForm(APPEARANCE);
    say($('appearanceMsg'), '');
  } catch (e) {
    say($('appearanceMsg'), e.message, 'bad');
  }
}

// Save, and optionally restart straight afterwards -- the two halves of
// the split button. Restarting is sequential rather than a second button
// you remember to press: a restart before the save landed would read the
// old file, which is exactly the mistake the old side-by-side pair invited.
//
// reload adds a fresh pull from Plaid in between. It still saves first:
// this menu hangs off the Save button, so an item here that quietly
// discarded the edits sitting in the form would be a trap.
async function saveAppearance(restart, reload) {
  const btn = $('saveAppearance'), chevron = $('saveMenuBtn');
  loading(btn, restart ? 'Saving…' : null);
  chevron.disabled = true;
  say($('appearanceMsg'), 'Saving…');
  let saved = false;
  try {
    const r = await api('/api/appearance', { appearance: collectAppearance() });
    APPEARANCE = r.appearance;
    buildAppearanceForm();
    fillAppearanceForm(APPEARANCE);
    // The one place the page's own theme moves. Doing it per keystroke made
    // the whole page flash on every checkbox, and a theme that changes
    // before it's saved is claiming something that isn't true yet.
    applyPageTheme(r.appearance.dark_theme, r.appearance.accent);
    STATE.dark_theme = r.appearance.dark_theme;
    STATE.accent = r.appearance.accent;
    saved = true;
    if (shortcutsChanged()) {
      say($('appearanceMsg'), 'Saved. Writing shortcuts…');
      try {
        await api('/api/install', shortcutsWanted());
        Object.assign(STATE, shortcutsWanted());
      } catch (e) {
        say($('appearanceMsg'), 'Settings saved, but the shortcuts failed: ' +
            e.message, 'bad');
        loaded(btn); chevron.disabled = false;
        return;
      }
    }
    say($('appearanceMsg'), STATE.running
      ? 'Saved. The widget picks most of this up on its next refresh; use ' +
        'Save and Restart for anything marked restart.'
      : 'Saved.', 'ok');
  } catch (e) {
    say($('appearanceMsg'), e.message, 'bad');
  }

  if (!saved || (!restart && !reload)) { loaded(btn); chevron.disabled = false; return; }

  // Before the restart, not after: the widget reads data.json as it comes
  // up, so pulling first means it draws the new balances immediately
  // instead of the old ones until the next scheduled fetch.
  let reloadOk = null;
  if (reload) {
    loading(btn, 'Reloading…');
    say($('appearanceMsg'), 'Saved. Fetching balances…');
    try {
      const r = await api('/api/verify', {});
      reloadOk = true;
      if (r.errors && r.errors.length) {
        say($('appearanceMsg'),
          'Saved. Fetched with ' + r.errors.length + ' error' +
          (r.errors.length === 1 ? '' : 's') + ': ' + r.errors.join('; '), 'bad');
      }
    } catch (e) {
      // Non-fatal: a failed pull leaves the previous data.json in place,
      // and the restart below is still worth doing.
      reloadOk = false;
      say($('appearanceMsg'), 'Saved, but the reload failed: ' + e.message, 'bad');
    }
  }

  if (!restart) { loaded(btn); chevron.disabled = false; loadState(); return; }

  loading(btn, 'Restarting…');
  say($('appearanceMsg'), 'Saved. Restarting…');
  try {
    const r = await api('/api/launch', {});
    // Don't let the restart's success bury a reload that didn't work.
    const did = reloadOk === false ? 'Saved (the reload failed)'
              : reloadOk ? 'Saved and reloaded' : 'Saved';
    say($('appearanceMsg'), r.action === 'restarted'
      ? did + ', and restarted the widget.'
      : did + '. Launched the widget – look for the icon in your system tray.',
      reloadOk === false ? 'bad' : 'ok');
    setTimeout(() => { loaded(btn); chevron.disabled = false; loadState(); }, 4000);
    return;
  } catch (e) {
    say($('appearanceMsg'), 'Saved, but the restart failed: ' + e.message, 'bad');
  }
  loaded(btn);
  chevron.disabled = false;
}

$('saveAppearance').onclick = () => saveAppearance(false, false);
$('saveRestart').onclick = () => saveAppearance(true, false);
$('saveReloadRestart').onclick = () => saveAppearance(true, true);
wireMenu('saveMenuBtn', 'saveMenu');

// ---------------------------------------------------------- shortcuts
// Not in appearance.json, so not part of collectAppearance: these three
// are files on disk. They follow the tab's rule anyway -- a tick is
// pending until Save, so a stray click is undone by not saving.
function fillShortcuts() {
  $('scStartup').checked = STATE.autostart;
  $('scStartMenu').checked = STATE.start_menu;
  $('scDesktop').checked = STATE.desktop;
  const dirs = STATE.shortcut_dirs || {};
  $('pathStartup').textContent = dirs.startup || '';
  $('pathStartMenu').textContent = dirs.start_menu || '';
  $('pathDesktop').textContent = dirs.desktop || '';
  $('notInstalled').hidden = STATE.installed;
}

function shortcutsWanted() {
  return {
    autostart: $('scStartup').checked,
    start_menu: $('scStartMenu').checked,
    desktop: $('scDesktop').checked,
  };
}

// Only when a tick actually moved. An install copies the exe and rewrites
// every shortcut, which is not work a Save that changed a color should be
// doing.
function shortcutsChanged() {
  const want = shortcutsWanted();
  return want.autostart !== STATE.autostart ||
         want.start_menu !== STATE.start_menu ||
         want.desktop !== STATE.desktop;
}

$('reinstall').onclick = async () => {
  const btn = $('reinstall');
  loading(btn, 'Writing shortcuts…');
  say($('appearanceMsg'), 'Writing shortcuts…');
  try {
    await api('/api/install', shortcutsWanted());
    say($('appearanceMsg'), 'Shortcuts updated.', 'ok');
    await loadState();
  } catch (e) { say($('appearanceMsg'), e.message, 'bad'); }
  loaded(btn);
};

// Loaded into the form rather than written straight to disk: one wrong
// click shouldn't be able to erase a tuned position and palette with no
// way back, and Save is right there.
$('restoreAppearance').onclick = () => {
  if (!APPEARANCE_DEFAULTS) return;
  fillAppearanceForm(APPEARANCE_DEFAULTS);
  fillShortcuts();
  say($('appearanceMsg'), 'Defaults loaded into the form. Press Save to apply them.', 'ok');
};


loadState().catch(e => say($('credsMsg'), e.message, 'bad'));
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
