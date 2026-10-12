"""
Pulls balances (and credit card liability details) for every linked account
and writes the result to data.json. Meant to run on a schedule (Windows Task
Scheduler, interval set by fetch_interval_minutes in config.json) -- it does
one pass of work and exits, no loop.
2 Plaid API calls per linked item each run (Balance + Liabilities), plus
a one-time institution lookup for its logo the first time that
institution's never been cached before (new link), and a one-time
item_get the first time an item is missing an institution_id (items
linked before logo support existed) -- this is the only script that ever
talks to Plaid.

If one linked item fails (e.g. needs re-auth), its error is recorded in
data.json and the others still get fetched normally.
"""

import base64
import concurrent.futures
import datetime
import json
import os
import sys
import urllib.request

import plaid
from plaid.api import plaid_api
from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest
from plaid.model.liabilities_get_request import LiabilitiesGetRequest
from plaid.model.institutions_get_by_id_request import InstitutionsGetByIdRequest
from plaid.model.institutions_get_by_id_request_options import (
    InstitutionsGetByIdRequestOptions,
)
from plaid.model.item_get_request import ItemGetRequest
from plaid.model.country_code import CountryCode
from plaid.exceptions import ApiException

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
DATA_PATH = os.path.join(os.path.dirname(__file__), "data.json")
CACHE_DIR = os.path.join(os.path.dirname(__file__), "cache")
LOGOS_DIR = os.path.join(CACHE_DIR, "logos")
CONFIG_SNAPSHOT_PATH = os.path.join(CACHE_DIR, "config_snapshot.json")

ENV_MAP = {
    "sandbox": plaid.Environment.Sandbox,
    "production": plaid.Environment.Production,
}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        sys.exit(
            "config.json not found. Copy config.example.json to config.json "
            "and fill in your Plaid credentials first."
        )
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def save_config(config):
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def load_config_snapshot():
    """The config.json state as of the end of the previous run, cached so
    this run can tell exactly what changed (new link, edited logo_url,
    etc.) instead of just "something in config.json is different"."""
    if not os.path.exists(CONFIG_SNAPSHOT_PATH):
        return {}
    with open(CONFIG_SNAPSHOT_PATH, "r") as f:
        return json.load(f)


def save_config_snapshot(config):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(CONFIG_SNAPSHOT_PATH, "w") as f:
        json.dump(config, f, indent=2)


def logo_fields_changed(item, snapshot_items_by_key):
    """True if this item's logo-relevant fields differ from last run's
    snapshot, or the item wasn't there at all (a brand new link) -- either
    way, fetch_logo() should bypass its normal cache-skip and fetch fresh
    instead of silently keeping a now-stale (or simply absent) logo."""
    key = item.get("item_id") or item.get("access_token")
    old = snapshot_items_by_key.get(key)
    if old is None:
        return True
    return any(
        item.get(f) != old.get(f)
        for f in ("institution_id", "use_custom_logo", "logo_url")
    )


def backfill_institution_id(client, item):
    """Items linked before logo support existed have no institution_id on
    file. Looks it up once via item_get and writes it back to config.json so
    this only ever costs one extra Plaid call per item, not one every run."""
    if item.get("institution_id"):
        return False
    try:
        resp = client.item_get(ItemGetRequest(access_token=item["access_token"]))
        item["institution_id"] = resp["item"]["institution_id"]
        return True
    except ApiException:
        return False


def build_client(config):
    plaid_config = plaid.Configuration(
        host=ENV_MAP[config["environment"]],
        api_key={"clientId": config["client_id"], "secret": config["secret"]},
    )
    return plaid_api.PlaidApi(plaid.ApiClient(plaid_config))


def fetch_logo(client, institution_id, use_custom_logo=False, logo_url=None, force=False):
    """Caches the institution's logo to cache/logos/<institution_id>.png.
    The default (non-custom) path only ever costs a Plaid/favicon lookup
    ONCE per institution -- once something's cached, every later run
    (scheduled or manual Reload) just reuses it with no network call at
    all, instead of re-fetching a logo that was never going to change.
    force=True (set in main() when this item's logo-relevant config.json
    fields changed since the last run's snapshot) bypasses that skip, so
    editing logo_url or linking a new bank actually takes effect instead
    of being silently ignored because *something* was already cached.
    use_custom_logo + logo_url is otherwise the one path that always tries
    fresh regardless, for when Plaid's logo is wrong/missing and the
    favicon fallback isn't good enough either. Either way, if a fetch
    attempt fails (bad URL, no connection), this falls back to whatever's
    already cached rather than blanking the logo out over a transient
    failure; if nothing's ever been cached, the caller falls back to a
    letter avatar."""
    if not institution_id:
        return None

    os.makedirs(LOGOS_DIR, exist_ok=True)
    rel_path = f"cache/logos/{institution_id}.png"
    abs_path = os.path.join(LOGOS_DIR, f"{institution_id}.png")

    if use_custom_logo and logo_url:
        try:
            urllib.request.urlretrieve(logo_url, abs_path)
            return rel_path
        except Exception:
            pass
        return rel_path if os.path.exists(abs_path) else None

    if os.path.exists(abs_path) and not force:
        return rel_path

    url_hint = None
    try:
        resp = client.institutions_get_by_id(
            InstitutionsGetByIdRequest(
                institution_id=institution_id,
                country_codes=[CountryCode("US")],
                options=InstitutionsGetByIdRequestOptions(
                    include_optional_metadata=True
                ),
            )
        )
        inst = resp["institution"]
        logo_b64 = inst.get("logo")
        if logo_b64:
            with open(abs_path, "wb") as f:
                f.write(base64.b64decode(logo_b64))
            return rel_path
        url_hint = inst.get("url")
    except ApiException:
        pass

    if url_hint:
        domain = url_hint.split("//")[-1].split("/")[0].replace("www.", "")
        try:
            urllib.request.urlretrieve(
                f"https://www.google.com/s2/favicons?domain={domain}&sz=128", abs_path
            )
            return rel_path
        except Exception:
            pass

    return rel_path if os.path.exists(abs_path) else None


def fetch_item(client, item, force_logo_refresh=False):
    access_token = item["access_token"]
    accounts_out = []
    logo = fetch_logo(
        client,
        item.get("institution_id"),
        item.get("use_custom_logo", False),
        item.get("logo_url"),
        force=force_logo_refresh,
    )

    balance_resp = client.accounts_balance_get(
        AccountsBalanceGetRequest(access_token=access_token)
    )

    # Liabilities call only tells us about credit accounts; depository
    # accounts don't have liabilities data and that's expected.
    liabilities_by_account = {}
    try:
        liab_resp = client.liabilities_get(
            LiabilitiesGetRequest(access_token=access_token)
        )
        for credit in liab_resp["liabilities"]["credit"] or []:
            liabilities_by_account[credit["account_id"]] = credit
    except ApiException:
        pass  # item may have no credit accounts at all; not an error

    for account in balance_resp["accounts"]:
        account_type = account["type"].value if hasattr(account["type"], "value") else str(account["type"])
        balances = account["balances"]
        entry = {
            "account_id": account["account_id"],
            "institution_name": item.get("institution_name", "Unknown"),
            "logo": logo,
            "name": account["name"],
            "mask": account["mask"],
            "type": account_type,
            "balance": balances["current"],
        }

        if account_type == "credit":
            liab = liabilities_by_account.get(account["account_id"])
            limit = balances["limit"]
            entry["limit"] = limit
            due_date = liab.get("next_payment_due_date") if liab else None
            entry["next_payment_due_date"] = (
                due_date.isoformat() if due_date else None
            )

        accounts_out.append(entry)

    return accounts_out


def process_item(client, item, force_logo_refresh=False):
    """One item's full fetch (backfill + balance/liabilities/logo), used as
    the unit of work for the thread pool in main() -- each item is an
    independent sequence of network calls to Plaid, so running them
    concurrently rather than one-after-another is a straight latency win
    with no change in what gets fetched."""
    label = item.get("institution_name", item.get("item_id", "unknown"))
    try:
        changed = backfill_institution_id(client, item)
        # A freshly-backfilled institution_id (None -> a real value) counts
        # as a change too -- the item's logo has never been fetched yet.
        accounts = fetch_item(client, item, force_logo_refresh or changed)
        return accounts, None, changed
    except ApiException as e:
        error_body = json.loads(e.body) if e.body else {}
        error = {
            "institution_name": label,
            "item_id": item.get("item_id"),
            "error_code": error_body.get("error_code", "UNKNOWN"),
            "error_message": error_body.get("error_message", "Plaid API error"),
        }
        print(f"[fetch_data] Error fetching {label}: "
              f"{error_body.get('error_code', e)}", file=sys.stderr)
        return [], error, False


def main():
    config = load_config()
    client = build_client(config)
    items = config.get("items", [])

    old_snapshot = load_config_snapshot()
    snapshot_items_by_key = {
        (it.get("item_id") or it.get("access_token")): it
        for it in old_snapshot.get("items", [])
    }

    all_accounts = []
    errors = []
    config_changed = False

    def work(item):
        force_logo = logo_fields_changed(item, snapshot_items_by_key)
        return process_item(client, item, force_logo)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(items))) as pool:
        for accounts, error, changed in pool.map(work, items):
            all_accounts.extend(accounts)
            if error:
                errors.append(error)
            if changed:
                config_changed = True

    if config_changed:
        save_config(config)

    # Snapshot reflects this run's final state (post-backfill), so the
    # *next* run's diff is against what actually ended up on disk.
    save_config_snapshot(config)

    data = {
        "last_updated": datetime.datetime.now().isoformat(),
        "accounts": all_accounts,
        "errors": errors,
    }

    with open(DATA_PATH, "w") as f:
        json.dump(data, f, indent=2)

    print(f"Wrote {len(all_accounts)} accounts, {len(errors)} errors to data.json")


if __name__ == "__main__":
    main()
