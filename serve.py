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
fast. The scheduled Task Scheduler run (setup_schedule.py) is unaffected:
it still launches fetch_data.py as its own one-shot process, same as
before, so there's no always-on resident process gained from this change.
"""

import fnmatch
import http.server
import json
import os

import fetch_data

DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(DIR, "config.json")
APPEARANCE_PATH = os.path.join(DIR, "appearance.json")
PORT = json.load(open(CONFIG_PATH)).get("port", 5000)

# Allowlist, not denylist: config.json (Plaid secret + access tokens) and
# anything else in this folder must never be reachable over HTTP by default.
ALLOWED_PATTERNS = ["dashboard*.html", "dashboard.js", "data.json"]


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
        "fetch_interval_minutes": config.get("fetch_interval_minutes"),
        "layout": appearance.get("layout", {}),
    }).encode("utf-8")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIR, **kwargs)

    def do_GET(self):
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

        body = json.dumps({"ok": ok}).encode("utf-8")
        self.send_response(200 if ok else 500)
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


if __name__ == "__main__":
    with Server(("localhost", PORT), Handler) as httpd:
        print(f"Serving {DIR} at http://localhost:{PORT}")
        httpd.serve_forever()
