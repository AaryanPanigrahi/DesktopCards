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
from paths import (
    APPEARANCE_EXAMPLE_PATH,
    APPEARANCE_PATH,
    ASSET_DIR,
    CONFIG_EXAMPLE_PATH,
    CONFIG_PATH,
    DATA_DIR,
    DATA_PATH,
    DETACHED,
    FROZEN,
    LOOPBACK,
    SETUP_PORT,  # 5000 is the dashboard, 8008 is standalone --link
    clean_child_env,
    ensure_data_dir,
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
    if not os.path.exists(CONFIG_PATH):
        return None
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


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
    """A new config.json, starting from the shipped example so defaults
    like port and fetch_interval_minutes come along rather than being
    re-invented here."""
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
    config.setdefault("items", [])
    return config


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


def read_data():
    """data.json, or an empty dict before the first fetch has run."""
    try:
        with open(DATA_PATH, "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def order_key(account):
    """The "Bank Name,last4" string appearance.json's layout order uses.

    Must match dashboard.js's orderKey exactly, or a hand-written order
    silently matches nothing and every card falls through to the appended
    tail. Mask-less accounts fall back to name, same as there."""
    mask = account.get("mask")
    institution = account.get("institution_name", "Unknown")
    if mask:
        return f"{institution},{mask}"
    return f"{institution} {account.get('name', '')}"


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
        # Not a configurable section: the dashboard draws these under a
        # plain "Other" heading with no order or hidden list behind it, so
        # they're listed here for completeness but can't be toggled.
        groups.append({
            "type": "other",
            "accounts": [entry(a, "other") for a in other],
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
            return json.load(f)
    except (OSError, ValueError):
        return default_appearance()


def sync_layout_order(appearance):
    """Rewrite each section's order list with the accounts that exist.

    The shipped example ships placeholder keys ("Chase,0000"), which match
    nothing on a real dashboard -- so someone opening appearance.json to
    tweak the order found a list that named none of their banks and no way
    to learn the right strings. Filling it with the real keys makes the
    file self-documenting: the order you see is the order being used.

    Keys already present keep their position, so a hand-sorted order
    survives; new accounts are appended and departed ones dropped."""
    layout = appearance.setdefault("layout", {})
    accounts = read_data().get("accounts", [])
    for section in SECTION_TYPES:
        block = layout.setdefault(section, {})
        block.setdefault("title", SECTION_FALLBACK_TITLES[section])
        keys = [order_key(a) for a in accounts if a.get("type") == section]
        kept = [k for k in block.get("order", []) if k in keys]
        block["order"] = kept + [k for k in keys if k not in kept]
        # Deliberately NOT pruned to what's present, unlike order. A bank
        # that errors on one fetch drops out of data.json, and pruning here
        # would quietly unhide an account someone had switched off -- the
        # worse of the two failures. Stale keys cost nothing but a line.
        block.setdefault("hidden", [])
    return appearance


def write_appearance(appearance):
    ensure_data_dir()
    with open(APPEARANCE_PATH, "w") as f:
        json.dump(appearance, f, indent=2)


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
    config = read_config()
    _, _, _icon = launch_target()
    return jsonify(
        configured=bool(config and config.get("client_id") and config.get("secret")),
        environment=(config or {}).get("environment"),
        items=item_summary(config),
        frozen=FROZEN,
        data_dir=DATA_DIR,
        installed=os.path.exists(installed_exe_path()) if FROZEN else True,
        autostart=os.path.exists(native().autostart_path(APP_NAME)),
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
    if not key or section not in SECTION_TYPES:
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
    on_disk = read_appearance().get("shadow_ui")
    if on_disk is None:
        appearance.pop("shadow_ui", None)
    else:
        appearance["shadow_ui"] = on_disk
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
        # Reordering can only permute what's already there. Anything else is
        # a stale page posting keys for accounts that have since gone, and
        # sync_layout_order would drop them on the next read anyway.
        known = set(block.get("order", []))
        if set(order) != known:
            return jsonify(
                ok=False,
                error="That order doesn't match the accounts on file. "
                      "Reload the page and try again.",
            ), 409
        block["order"] = order

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
    saved = read_config() or {}
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
    existing = read_config() or {}
    # Keep any items already linked -- this path is also how someone fixes
    # a rotated secret, and that must not drop their banks.
    #
    # reset_items is the opposite case, and the Advanced settings form asks
    # for it deliberately: an access token belongs to one client_id in one
    # environment, so switching either leaves every linked bank pointing at
    # credentials that can no longer read it. Keeping them would just mean
    # a widget full of items that error on every fetch, so the page warns
    # and then wipes them rather than pretending they survived.
    if existing.get("items") and not body.get("reset_items"):
        candidate["items"] = existing["items"]
    with open(CONFIG_PATH, "w") as f:
        json.dump(candidate, f, indent=2)
    print(f"wrote credentials to {CONFIG_PATH} ({environment})")
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


@app.route("/api/link_token", methods=["POST"])
def api_link_token():
    config = read_config()
    if not config:
        return jsonify(ok=False, error="No credentials saved yet."), 400
    try:
        return jsonify(ok=True, link_token=link.new_link_token(link.build_client(config)))
    except Exception as e:
        return jsonify(ok=False, error=_plaid_message(e)), 400


@app.route("/api/exchange", methods=["POST"])
def api_exchange():
    body = request.get_json(silent=True) or {}
    config = read_config()
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
    config = read_config()
    if not config:
        return jsonify(ok=False, error="Nothing configured."), 400
    before = len(config.get("items", []))
    config["items"] = [it for it in config.get("items", []) if it.get("item_id") != item_id]
    link.save_config(config)
    print(f"removed item {item_id} ({before} -> {len(config['items'])})")
    # The access token is gone from config.json, but Plaid still holds the
    # Item until it's explicitly removed there. Not done automatically:
    # it's irreversible and someone re-adding the same bank in a minute
    # shouldn't be punished for it.
    return jsonify(ok=True, items=item_summary(config))


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
    n = native()
    result = {"app_path": None, "start_menu": None, "startup": None,
              "uninstaller": None}

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
    result["start_menu"] = n.create_shortcut(
        os.path.join(n.start_menu_dir(), f"{APP_NAME}.lnk"),
        target=exe,
        arguments=args,
        icon=icon,
        working_dir=working_dir,
        description="DesktopCards balance widget",
    )
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
  main.manage { max-width: 1060px; }

  /* The appearance form's groups, balanced into two columns. CSS columns
     rather than a grid because there are a dozen groups of a dozen
     different heights: columns fill to an equal stack height, where a grid
     would make every row as tall as its tallest cell. */
  .cols { columns: 2; column-gap: 1.6rem; }
  /* Spacing between groups hangs off the bottom of each block, never the
     top: a top margin on whichever block happens to land at the head of
     the second column would push that column out of line with the first,
     and which block that is isn't knowable from here. */
  .colblock { break-inside: avoid; margin-bottom: 1.5rem; }
  .colblock:last-child { margin-bottom: 0; }
  .colblock > h3.grp:first-child { margin-top: 0; }

  /* Two halves, one each, where that's what the content is -- the accounts
     tab ("linked banks and what to do with them" | "which of their
     accounts get drawn") and the layout tab ("what each section is called
     and when it breaks a row" | the lists you drag). A grid, not columns:
     with exactly two blocks, balancing could decide they both fit in the
     first column and leave the second empty. */
  .split { display: grid; grid-template-columns: 1fr 1fr; gap: 0 1.8rem; }
  .split > * { min-width: 0; }

  /* For blocks that stay single-column on a wide page: a label/control row
     stretched to 1060px leaves its two halves at opposite ends of the
     window with nothing in between. */
  .narrow { max-width: 560px; }

  /* Below this a column is too narrow for a .fld's label and its control
     side by side, which is the shape of nearly every row in these forms. */
  @media (max-width: 900px) {
    .cols { columns: 1; }
    .split { grid-template-columns: 1fr; }
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
  .acts .right { margin-left: auto; display: flex; gap: .6rem; flex-wrap: wrap; }
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

  <section id="tab-accounts" hidden>
    <h2><span class="num">&#10003;</span> Linked banks</h2>
    <p class="hint">
      Add another bank, or remove one. Removing one forgets it here but
      leaves the bank linked at Plaid, so adding it back later is just
      another sign-in. The account list is what the last fetch returned,
      grouped into the sections the widget draws them in; new banks join it
      after the next refresh.
    </p>
    <div class="body">
      <div class="split">
        <div>
          <ul class="banks" id="bankList"></ul>
          <div class="row">
            <button class="solid" id="addBank">Add a bank</button>
            <button id="reinstall">Redo shortcuts</button>
            <button id="launchNow">Launch widget</button>
          </div>
          <p class="hint" id="notInstalled" hidden style="margin:.9rem 0 0">
            Not installed yet, so there's no Start-menu shortcut either.
            Launching or redoing shortcuts creates both.
          </p>
          <div class="check">
            <input type="checkbox" id="autostartManage">
            <label for="autostartManage" style="margin:0">Start with Windows</label>
          </div>
          <div class="msg" id="manageMsg"></div>
        </div>
        <div>
          <h3 class="grp" style="margin-top:0">Accounts on the widget</h3>
          <p class="hint2">
            Untick one to leave it off the widget &ndash; it stays linked,
            it just isn't drawn.
          </p>
          <div id="groups"></div>
        </div>
      </div>

      <h3 class="grp">Advanced</h3>
      <div class="check" style="margin-top:.3rem">
        <input type="checkbox" id="advToggle">
        <label for="advToggle" style="margin:0">Show Plaid credentials and environment</label>
      </div>
      <div id="advanced" hidden>
        <p class="hint2 warn">
          Saving here unlinks every bank. An access token only works for the
          client ID and environment it was created under, so banks linked
          under the old ones would error on every fetch instead of carrying
          over. You'll re-link them from this tab afterwards.
        </p>
        <!-- The same three credentials as step 1, so the same stacked
             shape: this is a form you fill in, not a row of settings you
             scan. -->
        <div class="narrow">
          <div class="fields">
            <div class="field">
              <label for="advEnv">Environment</label>
              <select id="advEnv">
                <option value="production">production</option>
                <option value="sandbox">sandbox</option>
              </select>
            </div>
            <div class="field">
              <label for="advClientId">Client ID</label>
              <input type="text" id="advClientId" autocomplete="off" spellcheck="false">
              <span class="sub2">Leave blank to keep the saved one</span>
            </div>
            <div class="field">
              <label for="advSecret">Secret</label>
              <input type="password" id="advSecret" autocomplete="off" spellcheck="false">
              <span class="sub2">Leave blank to keep the saved one</span>
            </div>
          </div>
          <p class="hint2">
            These start blank on purpose: setup never sends your saved
            credentials back to the page, so they can't end up in a browser
            cache or a screenshot.
          </p>
          <div class="foot">
            <div class="msg" id="advMsg"></div>
            <button class="solid" id="saveAdvanced">Save and unlink banks</button>
          </div>
        </div>
      </div>
    </div>
  </section>

  <section id="tab-layout" hidden>
    <h2>Layout</h2>
    <p class="hint">
      Drag a card to move it. Each section is its own list &ndash; which
      section a card lands in comes from the account type, not a preference,
      and the widget draws the two kinds differently. Everything on this tab
      saves as soon as you change it. What each section is called and when
      it breaks a card onto its own row are on the left.
    </p>
    <div class="body">
      <!-- Settings left, cards right: the lists are the thing you came
           here to drag, so they get a column to themselves rather than
           three text fields interleaved between them. -->
      <div class="split">
        <div id="layoutRules"></div>
        <div id="layoutSections"></div>
      </div>
      <div class="acts">
        <button id="undoLayout" disabled>Undo last move</button>
        <span class="right">
          <button id="restartLayout">Restart Widget</button>
        </span>
      </div>
      <div class="msg" id="layoutMsg"></div>
    </div>
  </section>

  <section id="tab-appearance" hidden>
    <h2>Appearance</h2>
    <p class="hint">
      The widget's settings from <code>appearance.json</code>, in the order
      they appear there &ndash; card order and section headings are on the
      Layout tab instead. Most of it takes effect the next time the
      widget's page loads;
      anything marked <span class="badge">restart</span> is read once at
      launch, so save it with <em>Save and Restart</em> from the arrow next
      to Save.
    </p>
    <div class="body">
      <div class="cols" id="appearanceForm"></div>
      <div class="acts">
        <button id="restoreAppearance">Restore to Default</button>
        <span class="right">
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
            </div>
          </span>
        </span>
      </div>
      <div class="msg" id="appearanceMsg"></div>
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
  if (!res.ok) throw new Error(data.error || ('Request failed (' + res.status + ')'));
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
    if (removable) {
      const btn = document.createElement('button');
      btn.className = 'danger';
      btn.textContent = 'Remove';
      btn.onclick = async () => {
        btn.disabled = true;
        try {
          const r = await api('/api/remove', { item_id: it.item_id });
          STATE.items = r.items;
          renderBanks(ul, r.items, removable);
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
    // "other" has no order or hidden list behind it in appearance.json, so
    // there's nothing for a toggle in this section to write into.
    const locked = g.type === 'other';
    const rows = [];

    const h = document.createElement('h3');
    h.className = 'grp';
    h.style.marginTop = '1rem';

    const all = document.createElement('input');
    all.type = 'checkbox';
    all.disabled = locked;
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
      box.disabled = locked;
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
        box.disabled = locked;
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
      for (const r of rows) r.box.disabled = locked;
      all.disabled = locked;
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

function showTab(name) {
  for (const btn of document.querySelectorAll('.tab')) {
    const on = btn.dataset.tab === name;
    btn.classList.toggle('on', on);
    btn.setAttribute('aria-selected', on ? 'true' : 'false');
  }
  $('tab-accounts').hidden = name !== 'accounts';
  $('tab-layout').hidden = name !== 'layout';
  $('tab-appearance').hidden = name !== 'appearance';
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
    for (const s of ['s1','s2','s3','s4','s5']) step(s, 'hidden');
    showTab(document.querySelector('.tab.on').dataset.tab);
    $('autostartManage').checked = STATE.autostart;
    $('notInstalled').hidden = STATE.installed;
    if (STATE.environment) $('advEnv').value = STATE.environment;
    renderBanks($('bankList'), STATE.items, true);
    renderGroups(STATE.groups);
  } else {
    $('subtitle').textContent = 'Five steps, about two minutes.';
    document.querySelector('main').classList.remove('manage');
    $('tabs').hidden = true;
    $('tab-accounts').hidden = true;
    $('tab-layout').hidden = true;
    $('tab-appearance').hidden = true;
  }

  // Say what the button will actually do. Pressing it while a widget is
  // running restarts that one rather than starting a second.
  // relabel(), not textContent: a loadState() that lands while one of
  // these is mid-restart would otherwise write over the spinner's
  // "Restarting…" and then have loaded() put the stale old label back.
  relabel($('launchNow'), STATE.running ? 'Restart widget' : 'Launch widget');
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
  try {
    token = (await api('/api/link_token', {})).link_token;
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
$('reinstall').onclick = async () => {
  const btn = $('reinstall');
  loading(btn, 'Writing shortcuts…');
  say($('manageMsg'), 'Writing shortcuts…');
  try {
    await api('/api/install', { autostart: $('autostartManage').checked });
    say($('manageMsg'), 'Shortcuts updated.', 'ok');
  } catch (e) { say($('manageMsg'), e.message, 'bad'); }
  loaded(btn);
};
// Same work, from the checkbox rather than the button -- so it spins the
// button too, which is where the result shows up.
$('autostartManage').onchange = () => $('reinstall').onclick();
$('launchNow').onclick = async () => {
  const btn = $('launchNow');
  loading(btn, STATE.running ? 'Restarting…' : 'Starting…');
  say($('manageMsg'), 'Starting…');
  try {
    const r = await api('/api/launch', {});
    say($('manageMsg'), r.action === 'restarted'
      ? 'Restarted the running widget.'
      : 'Launched. Look for the icon in your system tray.', 'ok');
    // The restart takes a moment to come back up; re-read after it has, so
    // the button label reflects reality rather than the instant mid-restart.
    // The spinner stays up for that wait too: it's the same operation
    // finishing, and loadState() is what retitles the button.
    setTimeout(() => { loaded(btn); loadState(); }, 4000);
    return;
  } catch (e) { say($('manageMsg'), e.message, 'bad'); }
  loaded(btn);
};

// ------------------------------------------------------------- advanced
$('advToggle').onchange = () => { $('advanced').hidden = !$('advToggle').checked; };

$('saveAdvanced').onclick = async () => {
  const banks = (STATE.items || []).length;
  const warning = banks
    ? 'This unlinks ' + banks + ' bank' + (banks === 1 ? '' : 's') +
      ' and you will have to link them again. Continue?'
    : 'Save these credentials?';
  if (!confirm(warning)) return;
  const btn = $('saveAdvanced');
  loading(btn, 'Saving…');
  say($('advMsg'), 'Checking with Plaid…');
  try {
    await api('/api/credentials', {
      client_id: $('advClientId').value,
      secret: $('advSecret').value,
      environment: $('advEnv').value,
      reset_items: true,
    });
    $('advClientId').value = '';
    $('advSecret').value = '';
    await loadState();
    say($('advMsg'), 'Saved. Link your banks again above, then restart the widget.', 'ok');
  } catch (e) {
    say($('advMsg'), e.message, 'bad');
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
  const rules = $('layoutRules'), lists = $('layoutSections');
  rules.innerHTML = '';
  lists.innerHTML = '';
  // Both columns are written section by section, so each one's two halves
  // stay in the same order down the page and the headings line up across.
  const sectionBlock = (host, section) => {
    const col = document.createElement('div');
    col.className = 'colblock';
    const h = document.createElement('h3');
    h.className = 'grp';
    h.textContent = SECTION_LABELS[section];
    col.append(h);
    host.append(col);
    return col;
  };

  for (const section of ['depository', 'credit']) {
    const cards = LAYOUT_ROWS[section] || [];

    // Heading text and the row rules still belong to the section they
    // affect -- they're just all in the left column now, so the lists on
    // the right are one uninterrupted run of drag targets.
    const rule = sectionBlock(rules, section);
    rule.append(layoutField(section, 'title', 'Heading shown on the widget', 'text'));
    if (section === 'depository') {
      rule.append(layoutField(section, 'new_row_digit_threshold',
        'Own row at this many balance digits', 'int', 'Default 6, i.e. $100,000+'));
      rule.append(layoutField(section, 'new_row_name_length_threshold',
        'Own row past this many name characters', 'int', 'Default 12'));
    }

    const list = sectionBlock(lists, section);
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
    (acc.mask ? ' <span class="num4">&bull;&bull;' + acc.mask + '</span>' : '');

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
  host.innerHTML = '';
  // The host is a two-column flow, and a group that split across the
  // break would leave its heading at the foot of one column and half its
  // fields at the head of the next. So each heading opens a .colblock that
  // its fields go into, and break-inside: avoid keeps that block whole.
  let block = null;
  const into = el => (block || host).append(el);
  for (const f of APPEARANCE_FIELDS) {
    if (f.grp) {
      block = document.createElement('div');
      block.className = 'colblock';
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
async function saveAppearance(restart) {
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
    say($('appearanceMsg'), STATE.running
      ? 'Saved. The widget picks most of this up on its next refresh; use ' +
        'Save and Restart for anything marked restart.'
      : 'Saved.', 'ok');
  } catch (e) {
    say($('appearanceMsg'), e.message, 'bad');
  }

  if (!saved || !restart) { loaded(btn); chevron.disabled = false; return; }

  loading(btn, 'Restarting…');
  say($('appearanceMsg'), 'Saved. Restarting…');
  try {
    const r = await api('/api/launch', {});
    say($('appearanceMsg'), r.action === 'restarted'
      ? 'Saved and restarted the widget.'
      : 'Saved. Launched the widget – look for the icon in your system tray.', 'ok');
    setTimeout(() => { loaded(btn); chevron.disabled = false; loadState(); }, 4000);
    return;
  } catch (e) {
    say($('appearanceMsg'), 'Saved, but the restart failed: ' + e.message, 'bad');
  }
  loaded(btn);
  chevron.disabled = false;
}

$('saveAppearance').onclick = () => saveAppearance(false);
$('saveRestart').onclick = () => saveAppearance(true);
wireMenu('saveMenuBtn', 'saveMenu');

// Loaded into the form rather than written straight to disk: one wrong
// click shouldn't be able to erase a tuned position and palette with no
// way back, and Save is right there.
$('restoreAppearance').onclick = () => {
  if (!APPEARANCE_DEFAULTS) return;
  fillAppearanceForm(APPEARANCE_DEFAULTS);
  say($('appearanceMsg'), 'Defaults loaded into the form. Press Save to apply them.', 'ok');
};


loadState().catch(e => say($('credsMsg'), e.message, 'bad'));
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
