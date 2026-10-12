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
    style = f' style="--accent:{accent}"' if _HEX_COLOR.match(accent) else ""
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


@app.route("/api/install", methods=["POST"])
def api_install():
    body = request.get_json(silent=True) or {}
    autostart = bool(body.get("autostart"))
    n = native()
    result = {"app_path": None, "start_menu": None, "startup": None}

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
     color-scheme is here for the native checkboxes, selects and the color
     picker, which a stylesheet can't reach into. */
  :root {
    --bg: #15181e; --card: #1d2128; --card-2: #23272f;
    --text: #e8eaed; --dim: #9aa0a6; --faint: #6b7280;
    --accent: #4c8dff; --ok: #34c759; --bad: #ff6b6b; --line: #2d323b;
    --hair: rgba(255,255,255,.04);
    color-scheme: dark;
  }
  :root[data-theme="light"] {
    --bg: #f4f5f7; --card: #ffffff; --card-2: #eceff3;
    --text: #1b2230; --dim: #596070; --faint: #878f9d;
    --accent: #0b66d0; --ok: #1a8f43; --bad: #c62828; --line: #dde1e8;
    --hair: rgba(0,0,0,.05);
    color-scheme: light;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 2.5rem 1rem 4rem; background: var(--bg); color: var(--text);
    font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
    display: flex; justify-content: center;
  }
  main { width: 100%; max-width: 620px; }
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
  input[type=text], input[type=password], select {
    width: 100%; padding: .6rem .7rem; border-radius: 8px; color: var(--text);
    background: var(--bg); border: 1px solid var(--line); font: inherit;
  }
  input:focus, select:focus { outline: none; border-color: var(--accent); }
  button {
    margin-top: 1rem; padding: .62rem 1.1rem; border-radius: 8px; border: 0;
    background: var(--accent); color: #fff; font: inherit; font-weight: 550; cursor: pointer;
  }
  button.ghost { background: transparent; border: 1px solid var(--line); color: var(--text); }
  button.danger { background: transparent; border: 1px solid var(--line); color: var(--bad);
                  padding: .3rem .6rem; margin: 0; font-size: .8rem; }
  button:disabled { opacity: .5; cursor: default; }
  .row { display: flex; gap: .6rem; flex-wrap: wrap; align-items: center; }
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
  .check input { width: auto; }
  footer { color: var(--faint); font-size: .82rem; text-align: center; margin-top: 1.5rem; }

  /* ---- management tabs ---- */
  nav.tabs { display: flex; gap: .3rem; margin: 0 0 1rem; border-bottom: 1px solid var(--line); }
  nav.tabs[hidden] { display: none; }
  .tab {
    margin: 0; padding: .55rem .95rem; border-radius: 8px 8px 0 0; background: transparent;
    color: var(--dim); font-weight: 500; border-bottom: 2px solid transparent;
  }
  .tab.on { color: var(--text); background: var(--card); border-bottom-color: var(--accent); }
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
  .fld > .ctl { flex: 0 0 11rem; display: flex; align-items: center; gap: .4rem; }
  .fld input[type=text], .fld input[type=number], .fld select { width: 100%; padding: .4rem .5rem; }
  .fld input[type=color] {
    width: 2.6rem; height: 2rem; padding: 0; background: var(--bg);
    border: 1px solid var(--line); border-radius: 6px;
  }
  .fld input[type=checkbox] { width: auto; margin: 0 auto 0 0; }
  .fld.off { opacity: .42; }
  .unit { color: var(--faint); font-size: .8rem; }
  .badge {
    font-size: .68rem; text-transform: uppercase; letter-spacing: .04em; color: var(--faint);
    border: 1px solid var(--line); border-radius: 999px; padding: .05rem .4rem; margin-left: .4rem;
  }
  /* Destructive on the left, the two you actually reach for on the right,
     so Restore to Default isn't sitting under the cursor that just pressed
     Save. */
  .acts { display: flex; gap: .6rem; flex-wrap: wrap; align-items: center;
          margin-top: 1.4rem; padding-top: 1.1rem; border-top: 1px solid var(--line); }
  .acts button { margin-top: 0; }
  .acts .right { margin-left: auto; display: flex; gap: .6rem; flex-wrap: wrap; }
  ul.accts { list-style: none; padding: 0; margin: .3rem 0 0; }
  ul.accts li {
    display: flex; justify-content: space-between; align-items: center; gap: 1rem;
    padding: .4rem .7rem; background: var(--card-2); border-radius: 8px;
    margin-bottom: .3rem; font-size: .9rem;
  }
  /* The whole left side is the checkbox's label, so the bank name is a
     click target rather than a 13px box being the only way to toggle. */
  ul.accts .who {
    display: flex; align-items: center; gap: .55rem; margin: 0;
    color: var(--text); font-size: .9rem; cursor: pointer;
  }
  ul.accts .who input { width: auto; margin: 0; cursor: pointer; }
  ul.accts .what { color: var(--dim); text-align: right; }
  ul.accts .num4 { font-variant-numeric: tabular-nums; color: var(--faint); }
  /* Still listed, visibly not drawn: removing the row would make hiding an
     account look like unlinking the bank. */
  ul.accts li.hiddenAcct { opacity: .45; }
  ul.accts li.hiddenAcct .what::after { content: ' (hidden)'; color: var(--faint); }
  ul.accts .none { color: var(--faint); background: none; padding-left: 0; }
  ol.order { color: var(--dim); font-size: .86rem; margin: .3rem 0 0; padding-left: 1.4rem; }
  ol.order code { color: var(--text); }

  /* ---- layout tab: drag to reorder ---- */
  ul.sortable { list-style: none; padding: 0; margin: .4rem 0 0; }
  ul.sortable li {
    display: flex; align-items: center; gap: .6rem; padding: .5rem .7rem;
    background: var(--card-2); border: 1px solid transparent; border-radius: 8px;
    margin-bottom: .35rem; font-size: .9rem; cursor: grab;
  }
  ul.sortable li.dragging { opacity: .4; }
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
  <nav class="tabs" id="tabs" hidden>
    <button class="tab on" data-tab="accounts">Accounts</button>
    <button class="tab" data-tab="layout">Layout</button>
    <button class="tab" data-tab="appearance">Appearance</button>
  </nav>

  <section id="tab-accounts" hidden>
    <h2><span class="num">&#10003;</span> Linked banks</h2>
    <p class="hint">
      Add another bank, or remove one. Removing one drops its access token
      here but leaves the bank linked at Plaid, so re-adding it later costs
      nothing.
    </p>
    <div class="body">
      <ul class="banks" id="bankList"></ul>
      <div class="row">
        <button id="addBank">Add a bank</button>
        <button class="ghost" id="reinstall">Redo shortcuts</button>
        <button class="ghost" id="launchNow">Launch widget</button>
      </div>
      <p class="hint" id="notInstalled" hidden style="margin:.9rem 0 0">
        Not installed yet &ndash; there's no Start-menu shortcut. Launching
        or redoing shortcuts will create one.
      </p>
      <div class="check">
        <input type="checkbox" id="autostartManage">
        <label for="autostartManage" style="margin:0">Start with Windows</label>
      </div>
      <div class="msg" id="manageMsg"></div>

      <h3 class="grp">Accounts on the widget</h3>
      <p class="hint2">
        What the last fetch returned, in the sections the widget draws them
        in. Untick one to leave it off the widget &ndash; it stays linked, it
        just isn't drawn. New banks show up here after the next refresh.
      </p>
      <div id="groups"></div>

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
        <div class="fld">
          <span class="lbl">Environment</span>
          <span class="ctl">
            <select id="advEnv">
              <option value="production">production</option>
              <option value="sandbox">sandbox</option>
            </select>
          </span>
        </div>
        <div class="fld">
          <span class="lbl">Client ID<span class="sub2">Leave blank to keep the saved one</span></span>
          <span class="ctl"><input type="text" id="advClientId" autocomplete="off" spellcheck="false"></span>
        </div>
        <div class="fld">
          <span class="lbl">Secret<span class="sub2">Leave blank to keep the saved one</span></span>
          <span class="ctl"><input type="password" id="advSecret" autocomplete="off" spellcheck="false"></span>
        </div>
        <p class="hint2">
          These start blank on purpose: setup never sends your saved
          credentials back to the page, so they can't end up in a browser
          cache or a screenshot.
        </p>
        <div class="acts">
          <button id="saveAdvanced">Save and unlink banks</button>
        </div>
        <div class="msg" id="advMsg"></div>
      </div>
    </div>
  </section>

  <section id="tab-layout" hidden>
    <h2>Layout</h2>
    <p class="hint">
      Drag a card to move it. Each section is its own list &ndash; which
      section a card lands in comes from the account type, not a preference,
      and the widget draws the two kinds differently. Drops save straight
      away.
    </p>
    <div class="body">
      <div id="layoutSections"></div>
      <div class="acts">
        <button class="ghost" id="undoLayout" disabled>Undo last move</button>
        <span class="right">
          <button class="ghost" id="restartLayout">Restart Widget</button>
        </span>
      </div>
      <div class="msg" id="layoutMsg"></div>
    </div>
  </section>

  <section id="tab-appearance" hidden>
    <h2>Appearance</h2>
    <p class="hint">
      Everything in <code>appearance.json</code>, in the order it appears
      there. Most of it takes effect the next time the widget's page loads;
      anything marked <span class="badge">restart</span> is read once at
      launch, so it needs the widget restarted.
    </p>
    <div class="body">
      <div id="appearanceForm"></div>
      <div class="acts">
        <button class="ghost" id="restoreAppearance">Restore to Default</button>
        <span class="right">
          <button id="saveAppearance">Save</button>
          <button class="ghost" id="restartWidget">Restart Widget</button>
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
      <label for="clientId">Client ID</label>
      <input type="text" id="clientId" autocomplete="off" spellcheck="false">
      <label for="secret">Secret</label>
      <input type="password" id="secret" autocomplete="off" spellcheck="false">
      <label for="environment">Environment</label>
      <select id="environment">
        <option value="production">production &ndash; your real accounts</option>
        <option value="sandbox">sandbox &ndash; Plaid's fake test bank</option>
      </select>
      <button id="saveCreds">Check and save</button>
      <div class="msg" id="credsMsg"></div>
    </div>
  </section>

  <!-- ====================================================== step 2 -->
  <section id="s2" hidden>
    <h2><span class="num">2</span> Link a bank</h2>
    <p class="hint">
      Opens Plaid's own login window. You sign in to your bank directly &ndash;
      your bank password never passes through DesktopCards.
    </p>
    <div class="body">
      <ul class="banks" id="linkedSoFar"></ul>
      <div class="row">
        <button id="startLink">Open Plaid Link</button>
        <button class="ghost" id="doneLinking" hidden>Done adding banks</button>
      </div>
      <div class="msg" id="linkMsg"></div>
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
      <button id="verify">Fetch my balances</button>
      <div class="msg" id="verifyMsg"></div>
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
      <button id="install">Install</button>
      <div class="msg" id="installMsg"></div>
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
      <div class="row">
        <button id="launch">Launch DesktopCards</button>
        <button class="ghost" id="quit">Close setup</button>
      </div>
      <div class="msg" id="doneMsg"></div>
    </div>
  </section>

  <footer id="where"></footer>
</main>

<script>
const $ = id => document.getElementById(id);
let STATE = {};

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
    const h = document.createElement('h3');
    h.className = 'grp';
    h.style.marginTop = '1rem';
    h.textContent = SECTION_LABELS[g.type] || g.type;
    host.append(h);
    const ul = document.createElement('ul');
    ul.className = 'accts';
    for (const a of g.accounts) {
      const li = document.createElement('li');
      li.classList.toggle('hiddenAcct', !!a.hidden);

      const left = document.createElement('label');
      left.className = 'who';
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.checked = !a.hidden;
      // "other" has no order or hidden list behind it in appearance.json,
      // so there's nothing to write a toggle into.
      box.disabled = g.type === 'other';
      box.onchange = async () => {
        box.disabled = true;
        try {
          await api('/api/visibility', { key: a.key, type: g.type, visible: box.checked });
          a.hidden = !box.checked;
          li.classList.toggle('hiddenAcct', !box.checked);
          say($('manageMsg'),
            (box.checked ? 'Showing ' : 'Hiding ') + a.institution_name +
            '. The widget picks that up on its next refresh.', 'ok');
          // The Appearance tab prints the order lists, which this changes.
          APPEARANCE = null;
        } catch (e) {
          box.checked = !box.checked;
          say($('manageMsg'), e.message, 'bad');
        }
        box.disabled = g.type === 'other';
      };
      left.append(box, document.createTextNode(a.institution_name));

      const right = document.createElement('span');
      right.className = 'what';
      right.innerHTML = a.name + (a.mask ? ' <span class="num4">&bull;&bull;' + a.mask + '</span>' : '');

      li.append(left, right);
      ul.append(li);
    }
    host.append(ul);
  }
}

function showTab(name) {
  for (const btn of document.querySelectorAll('.tab')) {
    btn.classList.toggle('on', btn.dataset.tab === name);
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
    $('tabs').hidden = true;
    $('tab-accounts').hidden = true;
    $('tab-layout').hidden = true;
    $('tab-appearance').hidden = true;
  }

  // Say what the button will actually do. Pressing it while a widget is
  // running restarts that one rather than starting a second.
  $('launchNow').textContent = STATE.running ? 'Restart widget' : 'Launch widget';
  $('launch').textContent = STATE.running ? 'Restart DesktopCards' : 'Launch DesktopCards';
}

// ---------------------------------------------------------------- step 1
$('saveCreds').onclick = async () => {
  const btn = $('saveCreds');
  btn.disabled = true;
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
  btn.disabled = false;
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
  btn.disabled = true;
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
  btn.disabled = false;
};

// ---------------------------------------------------------------- step 4
$('install').onclick = async () => {
  const btn = $('install');
  btn.disabled = true;
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
  btn.disabled = false;
};

// ---------------------------------------------------------------- step 5
$('launch').onclick = async () => {
  say($('doneMsg'), 'Starting…');
  try {
    const r = await api('/api/launch', {});
    say($('doneMsg'), r.action === 'restarted'
      ? 'Restarted the running widget.'
      : 'Launched. Look for the icon in your system tray.', 'ok');
  } catch (e) { say($('doneMsg'), e.message, 'bad'); }
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
  say($('manageMsg'), 'Writing shortcuts…');
  try {
    await api('/api/install', { autostart: $('autostartManage').checked });
    say($('manageMsg'), 'Shortcuts updated.', 'ok');
  } catch (e) { say($('manageMsg'), e.message, 'bad'); }
};
$('autostartManage').onchange = $('reinstall').onclick;
$('launchNow').onclick = async () => {
  say($('manageMsg'), 'Starting…');
  try {
    const r = await api('/api/launch', {});
    say($('manageMsg'), r.action === 'restarted'
      ? 'Restarted the running widget.'
      : 'Launched. Look for the icon in your system tray.', 'ok');
    // The restart takes a moment to come back up; re-read after it has, so
    // the button label reflects reality rather than the instant mid-restart.
    setTimeout(loadState, 4000);
  } catch (e) { say($('manageMsg'), e.message, 'bad'); }
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
  btn.disabled = true;
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
  btn.disabled = false;
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
  for (const section of ['depository', 'credit']) {
    const cards = LAYOUT_ROWS[section] || [];
    const block = layoutBlock(section);

    const h = document.createElement('h3');
    h.className = 'grp';
    h.textContent = SECTION_LABELS[section];
    host.append(h);

    // Heading text and the row rules belong to the section they affect,
    // rather than in a separate settings block away from the list they
    // change the shape of.
    host.append(layoutField(section, 'title', 'Heading shown on the widget', 'text'));
    if (section === 'depository') {
      host.append(layoutField(section, 'new_row_digit_threshold',
        'Own row at this many balance digits', 'int', 'Default 6, i.e. $100,000+'));
      host.append(layoutField(section, 'new_row_name_length_threshold',
        'Own row past this many name characters', 'int', 'Default 12'));
    }

    if (!cards.length) {
      const p = document.createElement('p');
      p.className = 'hint2';
      p.textContent = 'Nothing in this section yet.';
      host.append(p);
      continue;
    }

    const reasons = rowReasons(section);
    const ul = document.createElement('ul');
    ul.className = 'sortable';
    ul.dataset.section = section;
    cards.forEach((a, i) => ul.append(sortableRow(section, a, i, reasons.get(a.key))));
    host.append(ul);
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
  const byKey = new Map(LAYOUT_ROWS[last.section].map(a => [a.key, a]));
  LAYOUT_ROWS[last.section] = last.order.map(k => byKey.get(k)).filter(Boolean);
  renderLayout();
  saveOrder(last.section, 'Move undone.');
};

$('restartLayout').onclick = async () => {
  say($('layoutMsg'), 'Restarting…');
  try {
    const r = await api('/api/launch', {});
    say($('layoutMsg'), r.action === 'restarted'
      ? 'Restarted the widget.'
      : 'Launched. Look for the icon in your system tray.', 'ok');
    setTimeout(loadState, 4000);
  } catch (e) { say($('layoutMsg'), e.message, 'bad'); }
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
    sub: 'Normally your live Windows accent color' },
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
  { path: 'opacity', label: 'Opacity', type: 'float', step: 0.05, min: 0.15, max: 1,
    dep: 'translucent_background', sub: '0.15 to 1; exactly 1 turns it off' },

  { grp: 'Card shadow' },
  { path: 'shadow.enabled', label: 'Drop shadow', type: 'bool' },
  { path: 'shadow.offset', label: 'Offset', type: 'int', unit: 'px', dep: 'shadow.enabled' },
  { path: 'shadow.border', label: 'Border', type: 'int', unit: 'px', dep: 'shadow.enabled' },
  { path: 'shadow.color', label: 'Color', type: 'text', dep: 'shadow.enabled',
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

function inputFor(f) {
  const el = document.createElement('input');
  if (f.type === 'bool') el.type = 'checkbox';
  else if (f.type === 'color') el.type = 'color';
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
  return el;
}

function buildAppearanceForm() {
  const host = $('appearanceForm');
  host.innerHTML = '';
  for (const f of APPEARANCE_FIELDS) {
    if (f.grp) {
      const h = document.createElement('h3');
      h.className = 'grp';
      h.textContent = f.grp;
      host.append(h);
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
    const input = inputFor(f);
    input.onchange = refreshDeps;
    ctl.append(input);
    if (f.unit) {
      const u = document.createElement('span');
      u.className = 'unit';
      u.textContent = f.unit;
      ctl.append(u);
    }
    row.append(lbl, ctl);
    host.append(row);
  }

}

function fillAppearanceForm(source) {
  for (const f of APPEARANCE_FIELDS) {
    if (f.grp) continue;
    const el = $('ap:' + f.path);
    const v = getPath(source, f.path);
    if (f.type === 'bool') el.checked = !!v;
    else if (f.type === 'color') el.value = v || '#0078d4';
    else el.value = v === undefined || v === null ? '' : v;
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
    for (const input of row.querySelectorAll('input, select')) input.disabled = !on;
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
    // An empty optional color means "follow the theme", which is the key
    // being absent -- writing "" would pin every card's shadow to an
    // invalid color instead.
    if (!text && f.path === 'shadow.color') {
      const parent = getPath(out, 'shadow');
      if (parent) delete parent.color;
      continue;
    }
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

$('saveAppearance').onclick = async () => {
  const btn = $('saveAppearance');
  btn.disabled = true;
  say($('appearanceMsg'), 'Saving…');
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
    say($('appearanceMsg'), STATE.running
      ? 'Saved. The widget picks most of this up on its next refresh; use ' +
        'Restart Widget for anything marked restart.'
      : 'Saved.', 'ok');
  } catch (e) {
    say($('appearanceMsg'), e.message, 'bad');
  }
  btn.disabled = false;
};

// Loaded into the form rather than written straight to disk: one wrong
// click shouldn't be able to erase a tuned position and palette with no
// way back, and Save is right there.
$('restoreAppearance').onclick = () => {
  if (!APPEARANCE_DEFAULTS) return;
  fillAppearanceForm(APPEARANCE_DEFAULTS);
  say($('appearanceMsg'), 'Defaults loaded into the form. Press Save to apply them.', 'ok');
};

$('restartWidget').onclick = async () => {
  say($('appearanceMsg'), 'Restarting…');
  try {
    const r = await api('/api/launch', {});
    say($('appearanceMsg'), r.action === 'restarted'
      ? 'Restarted the widget.'
      : 'Launched. Look for the icon in your system tray.', 'ok');
    setTimeout(loadState, 4000);
  } catch (e) { say($('appearanceMsg'), e.message, 'bad'); }
};

loadState().catch(e => say($('credsMsg'), e.message, 'bad'));
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
