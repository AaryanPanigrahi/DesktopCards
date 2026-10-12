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

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
LINK_PORT = 8008

ENV_MAP = {
    "sandbox": plaid.Environment.Sandbox,
    "production": plaid.Environment.Production,
}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        sys.exit(
            "config.json not found. Copy config.example.json to config.json "
            "and fill in your Plaid client_id/secret/environment first."
        )
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def save_config(config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


config = load_config()

if config.get("environment") not in ENV_MAP:
    sys.exit(
        f"config.json 'environment' must be one of {list(ENV_MAP)}, "
        f"got {config.get('environment')!r}"
    )

plaid_config = plaid.Configuration(
    host=ENV_MAP[config["environment"]],
    api_key={"clientId": config["client_id"], "secret": config["secret"]},
)
api_client = plaid.ApiClient(plaid_config)
client = plaid_api.PlaidApi(api_client)

app = Flask(__name__)
shutdown_event = threading.Event()

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
    req = LinkTokenCreateRequest(
        products=[Products("auth")],
        required_if_supported_products=[Products("liabilities")],
        client_name="Personal Balance Dashboard",
        country_codes=[CountryCode("US")],
        language="en",
        user=LinkTokenCreateRequestUser(client_user_id="local-user"),
    )
    response = client.link_token_create(req)
    return jsonify(link_token=response["link_token"])


@app.route("/exchange_public_token", methods=["POST"])
def exchange_public_token():
    body = request.get_json()
    exchange_req = ItemPublicTokenExchangeRequest(public_token=body["public_token"])
    exchange_resp = client.item_public_token_exchange(exchange_req)

    config.setdefault("items", []).append(
        {
            "access_token": exchange_resp["access_token"],
            "item_id": exchange_resp["item_id"],
            "institution_name": body.get("institution_name", "Unknown"),
            "institution_id": body.get("institution_id"),
            "use_custom_logo": False,
            "logo_url": None,
        }
    )
    save_config(config)
    print(f"\nLinked: {body.get('institution_name', 'Unknown')} "
          f"(item_id={exchange_resp['item_id']})")
    return jsonify(ok=True)


@app.route("/shutdown", methods=["POST"])
def shutdown():
    shutdown_event.set()
    return jsonify(ok=True)


def watch_for_shutdown():
    shutdown_event.wait()
    os._exit(0)


if __name__ == "__main__":
    threading.Thread(target=watch_for_shutdown, daemon=True).start()
    url = f"http://localhost:{LINK_PORT}"
    print(f"Opening {url} in your browser to link a bank account...")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(port=LINK_PORT)
