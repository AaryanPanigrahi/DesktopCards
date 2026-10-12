"""
One-time (or run-again-to-add-another-bank) setup script.

Starts a tiny local server that hosts Plaid's real hosted Link widget in your
browser. You log into your bank directly through Plaid/your bank's own login
page (OAuth where supported) -- your credentials never pass through this
script. Once you finish linking, the resulting access token is saved to
config.json and the server shuts itself down.

Run again any time you want to add another bank/card.
"""

import json
import os
import sys
import threading
import webbrowser

from flask import Flask, jsonify, request
import plaid
from plaid.api import plaid_api
from plaid.model.country_code import CountryCode
from plaid.model.products import Products
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.item_public_token_exchange_request import (
    ItemPublicTokenExchangeRequest,
)
from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest

import net_guard
from paths import CONFIG_PATH, LOOPBACK

LINK_PORT = 8008

ENV_MAP = {
    "sandbox": plaid.Environment.Sandbox,
    "production": plaid.Environment.Production,
}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        sys.exit(
            "config.json not found. Run setup first, or copy "
            "config.example.json to config.json and fill in your Plaid "
            "client_id/secret/environment."
        )
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def save_config(config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def build_client(config):
    """A Plaid client for these credentials.

    Takes the config rather than reading it, because setup_app holds
    credentials that aren't on disk yet -- it validates them before
    writing anything, so there's nothing to load at that point."""
    env = config.get("environment")
    if env not in ENV_MAP:
        raise ValueError(
            f"'environment' must be one of {list(ENV_MAP)}, got {env!r}"
        )
    plaid_config = plaid.Configuration(
        host=ENV_MAP[env],
        api_key={"clientId": config["client_id"], "secret": config["secret"]},
    )
    return plaid_api.PlaidApi(plaid.ApiClient(plaid_config))


def new_link_token(client):
    """A short-lived token authorising one run of the Link widget.

    Also the cheapest call that proves a client_id/secret pair actually
    works, which is how setup validates credentials before saving them."""
    req = LinkTokenCreateRequest(
        products=[Products("auth")],
        required_if_supported_products=[Products("liabilities")],
        client_name="Personal Balance Dashboard",
        country_codes=[CountryCode("US")],
        language="en",
        user=LinkTokenCreateRequestUser(client_user_id="local-user"),
    )
    return client.link_token_create(req)["link_token"]


def add_item(client, config, public_token, institution_name, institution_id):
    """Exchanges a finished Link session for a lasting access token and
    appends it to config['items'].

    Appends rather than replaces -- that's what makes linking a second,
    third or fifth bank the same operation as the first. Each Link run
    produces its own Item with its own token."""
    exchange_req = ItemPublicTokenExchangeRequest(public_token=public_token)
    exchange_resp = client.item_public_token_exchange(exchange_req)

    item = {
        "access_token": exchange_resp["access_token"],
        "item_id": exchange_resp["item_id"],
        "institution_name": institution_name or "Unknown",
        "institution_id": institution_id,
        "use_custom_logo": False,
        "logo_url": None,
    }
    config.setdefault("items", []).append(item)
    save_config(config)
    print(f"Linked: {item['institution_name']} (item_id={item['item_id']})")
    return item


# Standalone --link mode below. Built lazily so importing this module for
# the helpers above costs nothing and can't exit the process.
app = Flask(__name__)
shutdown_event = threading.Event()


@app.before_request
def only_this_machine():
    """Same guard the other two servers use. See net_guard.

    This form is the most sensitive one in the app -- it exchanges a public
    token for an access token -- so it gets the same treatment as the
    dashboard rather than relying on loopback alone."""
    reason = net_guard.rejection(
        LINK_PORT,
        method=request.method,
        host=request.host,
        origin=request.headers.get("Origin"),
        fetch_site=request.headers.get("Sec-Fetch-Site"),
        fetch_mode=request.headers.get("Sec-Fetch-Mode"),
    )
    if reason:
        print(f"[link] refused {request.method} {request.path}: {reason}")
        return jsonify(ok=False, error="Refused: not a local request."), 403

PAGE = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Link a bank account</title>
  <style>
    body { font-family: system-ui, sans-serif; background: #111; color: #eee;
           display: flex; align-items: center; justify-content: center;
           height: 100vh; margin: 0; }
    #status { font-size: 1.1rem; text-align: center; }
  </style>
</head>
<body>
  <div id="status">Opening Plaid Link&hellip;</div>
  <script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
  <script>
    fetch('/create_link_token', { method: 'POST' })
      .then(r => {
        if (!r.ok) throw new Error('Server error creating link token (status ' + r.status + ')');
        return r.json();
      })
      .then(data => {
        const handler = Plaid.create({
          token: data.link_token,
          onSuccess: (public_token, metadata) => {
            document.getElementById('status').textContent = 'Saving...';
            fetch('/exchange_public_token', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                public_token: public_token,
                institution_name: metadata.institution ? metadata.institution.name : 'Unknown',
                institution_id: metadata.institution ? metadata.institution.institution_id : null
              })
            }).then(() => {
              document.getElementById('status').textContent =
                'Linked! You can close this tab.';
              fetch('/shutdown', { method: 'POST' });
            });
          },
          onExit: (err, metadata) => {
            document.getElementById('status').textContent =
              err ? ('Error: ' + err.error_message) : 'Cancelled. You can close this tab.';
          },
        });
        handler.open();
      })
      .catch(err => {
        document.getElementById('status').textContent = 'Error: ' + err.message;
      });
  </script>
</body>
</html>
"""


@app.route("/")
def index():
    return PAGE


@app.route("/create_link_token", methods=["POST"])
def create_link_token():
    config = load_config()
    return jsonify(link_token=new_link_token(build_client(config)))


@app.route("/exchange_public_token", methods=["POST"])
def exchange_public_token():
    body = request.get_json()
    # Re-read rather than holding a module-level copy: the widget may have
    # rewritten config.json (fetch_data backfills institution ids) while
    # this page was open, and clobbering that is avoidable.
    config = load_config()
    add_item(
        build_client(config),
        config,
        body["public_token"],
        body.get("institution_name"),
        body.get("institution_id"),
    )
    return jsonify(ok=True)


@app.route("/shutdown", methods=["POST"])
def shutdown():
    shutdown_event.set()
    return jsonify(ok=True)


def watch_for_shutdown():
    shutdown_event.wait()
    os._exit(0)


def main():
    threading.Thread(target=watch_for_shutdown, daemon=True).start()
    url = f"http://{LOOPBACK}:{LINK_PORT}"
    print(f"Opening {url} in your browser to link a bank account...")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    # Explicit, though it is also Flask's default: this form carries a Plaid
    # secret and an access token, and "it happens to default to loopback"
    # is not something that should be left implicit in the one file where
    # being wrong would publish bank credentials to the network.
    app.run(host=LOOPBACK, port=LINK_PORT)


if __name__ == "__main__":
    main()
