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
import profiles
from paths import LOOPBACK

LINK_PORT = 8008

ENV_MAP = {
    "sandbox": plaid.Environment.Sandbox,
    "production": plaid.Environment.Production,
}


def load_config(profile_id=None):
    """The profile a link will attach the new bank to.

    Shaped like the flat config.json this used to read, so build_client()
    and the link handler are unchanged. With several profiles enabled,
    active() is a choice rather than a lookup, so the link flow pins the
    id it picked and passes it back here for the exchange -- resolving it
    twice could mint the token against one profile and exchange it
    against another."""
    profiles.ensure_migrated()
    active = profiles.resolve(profile_id) if profile_id else profiles.active()
    if active is None:
        sys.exit(
            "No Plaid profile configured. Run setup first, or copy "
            "config.example.json to config.json and fill in your Plaid "
            "client_id/secret/environment."
        )
    return active


def save_config(config):
    """Persist a newly linked item against its profile. Credentials are
    never written from here -- see profiles.save_resolved()."""
    profiles.save_resolved(config)


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


# Plaid's "OAuth Multi-item" Sandbox profile -- the busiest of the eight
# starter profiles, so a sandbox link exercises the OAuth hand-off and an
# institution carrying several accounts rather than the one-bank happy
# path. Prefilled only in sandbox: in production this field would be a
# real user's phone number, and a fake one there changes what Link shows.
# The matching one-time code for every sandbox profile is 123456.
SANDBOX_PROFILE_PHONE = "+14155550138"


def new_link_token(client, environment=None):
    """A short-lived token authorising one run of the Link widget.

    Also the cheapest call that proves a client_id/secret pair actually
    works, which is how setup validates credentials before saving them."""
    # Two arrays, and the split is the whole point.
    #
    # products is what every linked Item must support, and Link enforces it
    # against the accounts the user ticks as well as the institution. Asking
    # for liabilities there refused a bank with only a checking account --
    # "None of your accounts are liability accounts" -- and asking for auth
    # instead refused a bank with only a credit card, since auth is
    # depository-only. Neither is something this app needs: it reads balances
    # and credit card due dates, nothing else.
    #
    # identity is the one product every account type supports (depository,
    # credit, loan, investment), so it is the required slot purely to satisfy
    # "at least one product" -- the API rejects an empty array, and rejects
    # balance outright because balance comes free with any other product.
    # Nothing here ever calls /identity/get.
    #
    # liabilities moves to required_if_supported_products: initialized when
    # the institution and the ticked accounts support it, skipped without an
    # error when they don't. So a credit card still arrives with its due
    # date, a checking-only bank links, and a mixed bank gets both.
    #
    # An Item linked without liabilities answers /liabilities/get with
    # PRODUCTS_NOT_SUPPORTED, which fetch_data already treats as "no due
    # dates here" rather than a failure. Products cannot be added to an
    # existing Item by changing this, so Items linked before now keep
    # whatever they were created with.
    user = LinkTokenCreateRequestUser(client_user_id="local-user")
    if environment == "sandbox":
        user.phone_number = SANDBOX_PROFILE_PHONE

    req = LinkTokenCreateRequest(
        products=[Products("identity")],
        required_if_supported_products=[Products("liabilities")],
        client_name="Personal Balance Dashboard",
        country_codes=[CountryCode("US")],
        language="en",
        user=user,
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
    # Read before the append, because items_created() falls back to
    # len(items) for profiles written before the counter existed. Counting
    # after the append let that fallback see the bank being linked right
    # now, and the +1 below counted it a second time -- every profile
    # reported one Item more than it had ever spent, from its first link
    # onward, and the Trial warning fired a bank early.
    spent_before = config.get("items_created", 0)
    config.setdefault("items", []).append(item)
    # Counts Items ever created, not Items currently linked. Plaid's Trial
    # plan caps a team at 10 Items for its lifetime and /item/remove does
    # not give a slot back, so the number that matters to a user on Trial
    # is this one -- len(items) would under-report every bank they have
    # linked and later removed.
    #
    # Sandbox Items are unlimited and spend nothing, so they must not count.
    # That matters beyond the display: a Plaid team has one client_id across
    # both environments, so a sandbox link that incremented this would
    # follow the client_id into production and claim Items never really used.
    if config.get("environment") != "sandbox":
        config["items_created"] = max(spent_before, len(config["items"]) - 1) + 1
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


# The profile the open Link session belongs to. Set when the token is
# minted and read back by the exchange: the token is tied to one Plaid
# team, and the public_token it produces is only redeemable by that team.
_link_target_id = None


@app.route("/create_link_token", methods=["POST"])
def create_link_token():
    global _link_target_id
    config = load_config()
    _link_target_id = config.get("id")
    return jsonify(
        link_token=new_link_token(
            build_client(config), config.get("environment")
        )
    )


@app.route("/exchange_public_token", methods=["POST"])
def exchange_public_token():
    body = request.get_json()
    # Re-read rather than holding a module-level copy: the widget may have
    # rewritten config.json (fetch_data backfills institution ids) while
    # this page was open, and clobbering that is avoidable. By id, though,
    # not by asking active() again: enabling a profile elsewhere between
    # the two calls would exchange this token against the wrong team,
    # Plaid would refuse it, and the Item that Link already created would
    # be spent with its access token thrown away.
    config = load_config(_link_target_id)
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
