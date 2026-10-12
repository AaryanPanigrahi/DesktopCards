"""
Minimal local HTTP server for the dashboard folder. Exists only so
dashboard.html's fetch('data.json') works without CORS issues. Idle except
when a request actually comes in -- no polling, no background work.

Also expands appearance.json's /appearance.json response (accent + scale +
spacing + layout) on every request. appearance.json holds both appearance
settings and the section layout/ordering (previously a separate
layout.json) -- one file, one fetch. Its custom_spacing flag picks which
spacing source wins: false uses extra_spacing (one number, applied to all
four sides), true uses the per-side spacing object instead.
appearance.json has no secrets in it (unlike config.json, which is never
sent to the browser), so editing it takes effect on the very next
dashboard load with no separate sync script to re-run.

POST /refresh calls fetch_data.main() directly in this already-running
process -- that's what the system tray icon's "Reload" (widget.py) calls.
This used to spawn fetch_data.py as a brand new `python fetch_data.py`
subprocess instead, which meant re-importing plaid-python's whole
dependency tree (requests, urllib3, its model/validation layer, etc.) from
a cold interpreter on every single click, often costing more wall-clock
time than the actual Plaid API calls. Importing fetch_data as a module
here instead pays that import cost once, at serve.py's own startup, not
per click -- it still re-reads config.json fresh every call (main() does
that itself), so nothing about *what* gets refreshed changes, only how
fast. It still re-reads config.json fresh on every call.

The interval refresh is no longer a separate process either: it's a timer
thread in widget.py (fetch_loop), and it shares fetch_data's lock with
/refresh so the two can't overlap.

This module is normally hosted inside the widget process rather than run
on its own -- see serve_in_thread. Running it as a script still works and
is useful for debugging the page without the window in the way.
"""

import fnmatch
import http.server
import json
import os
import threading
import time

import fetch_data
import net_guard

from paths import APPEARANCE_PATH, ASSET_DIR, CONFIG_PATH, DATA_DIR, LOOPBACK

def read_port():
    """The port, or the default if config.json isn't readable yet.

    This used to be a bare json.load at import time, which turned a missing
    or half-written config.json into a FileNotFoundError traceback before
    anything else could run -- fatal and, under a windowless build, silent.
    A server that comes up on the default port is recoverable; one that
    can't be imported isn't."""
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f).get("port", 5000)
    except (OSError, ValueError):
        return 5000


PORT = read_port()

# Allowlist, not denylist: config.json (Plaid secret + access tokens) and
# anything else in these folders must never be reachable over HTTP by default.
ALLOWED_PATTERNS = ["dashboard*.html", "dashboard.js", "data.json"]

# Which URLs are answered out of DATA_DIR rather than ASSET_DIR. Everything
# the app ships with is an asset; everything it writes is data. Identical
# folders when running from source, so this only matters to a frozen build.
DATA_OWNED = ("data.json", "cache/")

# Set by widget.py when it hosts this server, so POST /restart has
# something to call. Left None by a standalone `--serve`, which has no
# widget to restart -- see Handler.handle_restart.
restart_hook = None


def resolve_spacing(appearance):
    if appearance.get("custom_spacing"):
        s = appearance.get("spacing", {})
        return {
            "left": s.get("left", 0),
            "top": s.get("top", 0),
            "right": s.get("right", 0),
            "bottom": s.get("bottom", 0),
        }
    v = appearance.get("extra_spacing", 0)
    return {"left": v, "top": v, "right": v, "bottom": v}


def build_appearance():
    with open(APPEARANCE_PATH, "r") as f:
        appearance = json.load(f)
    # fetch_interval_minutes comes from config.json, not appearance.json --
    # it's not an appearance setting, but the countdown-to-next-update
    # display needs it, and this is the one existing channel for handing
    # the browser a safe, secret-free number out of config.json without
    # ever serving that file itself.
    with open(CONFIG_PATH, "r") as f:
        config = json.load(f)
    return json.dumps({
        "accent": appearance.get("accent"),
        "scale": appearance.get("scale", 1.0),
        "spacing": resolve_spacing(appearance),
        "corner_radius": appearance.get("corner_radius", 0),
        # Passed through as-is; dashboard.js fills in the per-key defaults
        # so the whole block can be reduced to {"enabled": true}.
        "shadow": appearance.get("shadow", {}),
        # One flag, not a palette: the dark colors live in dashboard.html's
        # [data-theme="dark"] block, so there's nothing per-color to pass.
        "dark_theme": appearance.get("dark_theme", False),
        # The flag and its one number. Same split as dark_theme: the per-tier
        # alphas are derived in dashboard.js from this single opacity, so
        # there's nothing per-element to pass. widget.py reads the flag too,
        # independently -- it decides whether the OS window itself is
        # transparent, which no amount of CSS can do from in here.
        "translucent_background": appearance.get("translucent_background", False),
        "opacity": appearance.get("opacity", 1.0),
        "fetch_interval_minutes": config.get("fetch_interval_minutes"),
        "layout": appearance.get("layout", {}),
    }).encode("utf-8")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ASSET_DIR, **kwargs)

    def translate_path(self, path):
        """Map a URL onto one of the two roots.

        The base class resolves against a single directory, and does the
        work that matters for safety while it's at it -- collapsing `..`,
        dropping drive letters and absolute prefixes. So resolve against
        ASSET_DIR first and let it sanitize, then re-root the handful of
        URLs that belong to the data side. Re-rooting a path that's already
        been reduced to a safe relative one can't reintroduce traversal."""
        full = super().translate_path(path)
        rel = os.path.relpath(full, ASSET_DIR).replace("\\", "/")
        if rel.startswith(".."):
            return full  # outside the root entirely; the allowlist rejects it
        if rel.startswith(DATA_OWNED):
            return os.path.join(DATA_DIR, *rel.split("/"))
        return full

    def local_only(self):
        """403s anything that isn't this machine's own browser.

        Binding to loopback keeps this server off the network; this is the
        other half, against a page in the local browser reaching in by DNS
        rebinding or cross-site POST. See net_guard."""
        reason = net_guard.rejection(
            # The server's real port, not the module-level PORT: build_server
            # can be handed a different one.
            self.server.server_address[1],
            method=self.command,
            host=self.headers.get("Host"),
            origin=self.headers.get("Origin"),
            fetch_site=self.headers.get("Sec-Fetch-Site"),
            fetch_mode=self.headers.get("Sec-Fetch-Mode"),
        )
        if reason is None:
            return True
        print(f"[serve] refused {self.command} {self.path}: {reason}")
        self.send_error(403, "Forbidden")
        return False

    def do_GET(self):
        if not self.local_only():
            return

        if self.path == "/":
            self.path = "/dashboard.html"

        path_only = self.path.split("?", 1)[0]

        if path_only == "/appearance.json":
            body = build_appearance()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # Cached institution logos live under cache/logos/ -- allowed by
        # path prefix + extension rather than basename, same allowlist spirit.
        if path_only.startswith("/cache/logos/") and path_only.endswith(".png"):
            super().do_GET()
            return

        name = os.path.basename(path_only)
        if not any(fnmatch.fnmatch(name, pat) for pat in ALLOWED_PATTERNS):
            self.send_error(404, "Not Found")
            return

        super().do_GET()

    def do_POST(self):
        if not self.local_only():
            return
        if self.path == "/restart":
            self.handle_restart()
            return
        if self.path != "/refresh":
            self.send_error(404, "Not Found")
            return

        ok = True
        try:
            fetch_data.main()
        except (Exception, SystemExit) as e:
            # SystemExit too -- fetch_data.load_config() exits the process
            # outright if config.json is missing, which would otherwise
            # silently kill just this request thread and leave the client
            # hanging instead of getting a response.
            ok = False
            print(f"[serve] /refresh failed: {e}")

        self.send_json({"ok": ok}, 200 if ok else 500)

    def handle_restart(self):
        """Relaunches the widget on request from another process.

        Setup's "Launch widget" button needs this. A second instance can't
        just start -- the single-instance lock stops it, and rightly so --
        but doing nothing is the wrong answer when someone has clicked a
        button. Asking the running widget to restart itself is, and it's
        the same operation the tray's Restart performs.

        Only reachable when widget.py has registered the hook, so a bare
        `--serve` process correctly reports that it has no widget to
        restart rather than pretending to do something."""
        if restart_hook is None:
            self.send_json({"ok": False, "error": "no widget in this process"}, 404)
            return
        # Answered before acting: the restart tears down this very server,
        # so a reply sent afterwards would never arrive.
        self.send_json({"ok": True})
        try:
            self.wfile.flush()
        except OSError:
            pass
        threading.Thread(target=restart_hook, daemon=True).start()

    def send_json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        # This dashboard edits its own files constantly during setup/tuning --
        # a stale browser cache serving an old dashboard.js is a worse bug
        # than the cost of always refetching a few tiny local files.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        super().end_headers()

    def log_message(self, format, *args):
        pass  # keep it quiet


class Server(http.server.ThreadingHTTPServer):
    # HTTPServer defaults this to True, which on Windows lets a second
    # process silently bind the same port on top of a still-running one --
    # requests then get nondeterministically split between zombie
    # instances. Force a loud "port in use" error instead.
    allow_reuse_address = False


def build_server(port=None, attempts=12, delay=0.5):
    """A bound Server, retrying while the port is still occupied.

    allow_reuse_address is False on purpose (see Server), which means a
    previous instance's sockets sitting in TIME_WAIT make the bind fail
    outright rather than quietly stacking two servers on one port. The
    tray's Restart lands inside that window routinely: the old process
    exits and the replacement tries to bind a few milliseconds later.

    Waiting it out is the whole fix -- TIME_WAIT is a timer, not a
    contended lock, so retrying is the correct response and retrying
    faster wouldn't help. Raises if the port never frees up, which is a
    genuinely different situation (something else owns it) and should be
    reported rather than retried forever."""
    port = PORT if port is None else port
    last = None
    for attempt in range(attempts):
        try:
            return Server((LOOPBACK, port), Handler)
        except OSError as e:
            last = e
            if attempt + 1 < attempts:
                time.sleep(delay)
    raise OSError(
        f"could not bind {LOOPBACK}:{port} after {attempts} attempts "
        f"({last}). Another process may be using it."
    )


def serve_in_thread(port=None):
    """Runs the server on a daemon thread and hands back the Server.

    This is how the widget hosts it -- in-process rather than as a
    subprocess. Under a frozen onefile build a child spawned from
    sys.executable would re-extract the entire bundle a second time, and
    as a thread there's also nothing left to orphan: it dies with the
    process that owns it, so no server can outlive its widget and sit on
    the port."""
    httpd = build_server(port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main():
    with build_server() as httpd:
        print(f"Serving {ASSET_DIR} at http://{LOOPBACK}:{PORT}")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
