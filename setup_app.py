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
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser

from flask import Flask, jsonify, request

import plaid_link_setup as link
from paths import (
    APPEARANCE_PATH,
    ASSET_DIR,
    CONFIG_PATH,
    DATA_DIR,
    DETACHED,
    FROZEN,
    clean_child_env,
    ensure_data_dir,
)

SETUP_PORT = 8009  # 5000 is the dashboard, 8008 is standalone --link
APP_NAME = "DesktopCards"

shutdown_event = threading.Event()
app = Flask(__name__)


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
            f"http://localhost:{dashboard_port()}/appearance.json", timeout=1
        )
        return True
    except urllib.error.HTTPError:
        return True  # something replied, which is the question
    except Exception:
        return False


def ask_widget_to_restart():
    """Asks the running widget to relaunch itself. True if it accepted."""
    req = urllib.request.Request(
        f"http://localhost:{dashboard_port()}/restart", method="POST"
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
        example = os.path.join(ASSET_DIR, "appearance.example.json")
        if os.path.exists(example):
            shutil.copy2(example, APPEARANCE_PATH)
            print(f"seeded {APPEARANCE_PATH}")


def config_skeleton(client_id, secret, environment):
    """A new config.json, starting from the shipped example so defaults
    like port and fetch_interval_minutes come along rather than being
    re-invented here."""
    example = os.path.join(ASSET_DIR, "config.example.json")
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


# ------------------------------------------------------------------ routes

@app.route("/")
def index():
    return PAGE


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
    )


@app.route("/api/credentials", methods=["POST"])
def api_credentials():
    body = request.get_json(silent=True) or {}
    client_id = (body.get("client_id") or "").strip()
    secret = (body.get("secret") or "").strip()
    environment = (body.get("environment") or "production").strip()

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
    if existing.get("items"):
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


def watch_for_shutdown():
    shutdown_event.wait()
    # Flask's development server has no clean programmatic stop, and this
    # is a single-purpose process whose job is finished -- nothing here
    # owns state worth unwinding.
    os._exit(0)


def main():
    threading.Thread(target=watch_for_shutdown, daemon=True).start()
    url = f"http://localhost:{SETUP_PORT}"
    print(f"Setup is at {url}")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    # localhost only. This form carries a Plaid secret; it has no business
    # being reachable from the network.
    app.run(host="127.0.0.1", port=SETUP_PORT)


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DesktopCards Setup</title>
<script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
<style>
  :root {
    --bg: #15181e; --card: #1d2128; --card-2: #23272f;
    --text: #e8eaed; --dim: #9aa0a6; --faint: #6b7280;
    --accent: #4c8dff; --ok: #34c759; --bad: #ff6b6b; --line: #2d323b;
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
</style>
</head>
<body>
<main>
  <h1>DesktopCards</h1>
  <p class="sub" id="subtitle">Setting things up.</p>

  <!-- ===================================================== manage view -->
  <section id="manage" hidden>
    <h2><span class="num">&#10003;</span> Linked banks</h2>
    <p class="hint">Already set up. Add another bank, or remove one.</p>
    <div class="body">
      <ul class="banks" id="bankList"></ul>
      <div class="row">
        <button id="addBank">Add a bank</button>
        <button class="ghost" id="reinstall">Redo shortcuts</button>
        <button class="ghost" id="launchNow">Launch widget</button>
      </div>
      <p class="hint" id="notInstalled" hidden style="margin:.9rem 0 0">
        Not installed yet &mdash; there's no Start-menu shortcut. Launching
        or redoing shortcuts will create one.
      </p>
      <div class="check">
        <input type="checkbox" id="autostartManage">
        <label for="autostartManage" style="margin:0">Start with Windows</label>
      </div>
      <div class="msg" id="manageMsg"></div>
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
        <option value="production">production &mdash; your real accounts</option>
        <option value="sandbox">sandbox &mdash; Plaid's fake test bank</option>
      </select>
      <button id="saveCreds">Check and save</button>
      <div class="msg" id="credsMsg"></div>
    </div>
  </section>

  <!-- ====================================================== step 2 -->
  <section id="s2" hidden>
    <h2><span class="num">2</span> Link a bank</h2>
    <p class="hint">
      Opens Plaid's own login window. You sign in to your bank directly &mdash;
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
        lives in its system tray icon &mdash; reload, restart, add accounts, quit.
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

async function loadState() {
  STATE = await api('/api/state');
  $('where').textContent = 'Settings and data: ' + STATE.data_dir;
  if (STATE.configured) {
    $('subtitle').textContent = 'Already set up. Change what you need.';
    step('manage', 'active');
    for (const s of ['s1','s2','s3','s4','s5']) step(s, 'hidden');
    $('autostartManage').checked = STATE.autostart;
    $('notInstalled').hidden = STATE.installed;
    renderBanks($('bankList'), STATE.items, true);
  } else {
    $('subtitle').textContent = 'Five steps, about two minutes.';
    step('manage', 'hidden');
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
async function runLink(msgEl, onDone) {
  say(msgEl, 'Opening Plaid…');
  let token;
  try {
    token = (await api('/api/link_token', {})).link_token;
  } catch (e) { say(msgEl, e.message, 'bad'); return; }

  Plaid.create({
    token,
    onSuccess: async (public_token, metadata) => {
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
      say(msgEl, err ? ('Plaid: ' + (err.error_message || err.error_code))
                     : 'Cancelled — nothing was saved.', err ? 'bad' : null);
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
  try { await api('/api/quit', {}); } catch (e) {}
  say($('doneMsg'), 'Setup closed. You can close this tab.', 'ok');
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

loadState().catch(e => say($('credsMsg'), e.message, 'bad'));
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
