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

Fetches for every enabled profile, one Plaid client each, and merges the
results into data.json in profile order. Each profile also keeps its own
data.<profile>.json, which is both what a disabled-then-enabled profile
redraws from and what a profile falls back to when this run could not
reach it.

Failure is per bank. A bank this run could not reach keeps its last
good balances, marked stale, and its error is recorded in data.json; the
banks either side of it update normally. That covers a whole profile
failing too -- a refused secret errors every one of its items -- without
treating it as a different kind of event: the accounts stay on screen
either way instead of vanishing as though the banks had been unlinked.
"""

import base64
import concurrent.futures
import datetime
import json
import os
import sys
import threading
import time
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

import profiles
from paths import (
    CACHE_DIR,
    CONFIG_SNAPSHOT_PATH,
    DATA_PATH,
    LOGOS_DIR,
    profile_data_path,
    profile_snapshot_path,
)

ENV_MAP = {
    "sandbox": plaid.Environment.Sandbox,
    "production": plaid.Environment.Production,
}


def save_config(config):
    """Write back what this run changed, which is only ever the items --
    a backfilled institution_id, say. Credentials are never written from
    here; see profiles.save_resolved()."""
    profiles.save_resolved(config)


def load_config_snapshot(profile_id=None):
    """A profile's state as of the end of its previous run, cached so this
    run can tell exactly what changed (new link, edited logo_url, etc.)
    instead of just "something is different".

    One per profile: the snapshot is diffed against that profile's items,
    and a single shared file would make every item in every other profile
    look new on the first run after a profile was enabled, forcing a
    pointless logo re-fetch for all of them.

    Falls back to the old shared snapshot when the profile has no file of
    its own yet, so the first run after migration diffs against something
    real rather than treating every existing bank as newly linked."""
    path = profile_snapshot_path(profile_id) if profile_id else CONFIG_SNAPSHOT_PATH
    if not os.path.exists(path) and os.path.exists(CONFIG_SNAPSHOT_PATH):
        path = CONFIG_SNAPSHOT_PATH
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


# Never written to the snapshot. The snapshot exists to diff logo
# fields between runs, so it needs the items and nothing else; the
# resolved profile it is handed also carries the Plaid client ID and
# secret, and writing those put a second, undeclared copy of them in
# cache/ -- outside secrets.json, outside the backups, and not something
# anyone would think to look for before sharing a cache folder.
_SNAPSHOT_SKIP = ("client_id", "secret")


def save_config_snapshot(config, profile_id=None):
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = profile_snapshot_path(profile_id) if profile_id else CONFIG_SNAPSHOT_PATH
    payload = {k: v for k, v in (config or {}).items() if k not in _SNAPSHOT_SKIP}
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


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
    # Raised rather than a KeyError, because with several profiles one bad
    # environment must cost that profile and not the whole run. Only
    # reachable by hand-editing config.json.
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
    #
    # Never fatal -- a card with no due date still beats no card at all --
    # but the code is printed rather than swallowed, because "no due dates
    # appeared" has several very different causes and silence makes them
    # indistinguishable. NO_LIABILITY_ACCOUNTS is the ordinary one (this
    # item is all checking/savings); PRODUCTS_NOT_SUPPORTED means the Item
    # was created without liabilities and needs re-linking to gain it, which
    # no amount of retrying here will fix.
    liabilities_by_account = {}
    try:
        liab_resp = client.liabilities_get(
            LiabilitiesGetRequest(access_token=access_token)
        )
        for credit in liab_resp["liabilities"]["credit"] or []:
            liabilities_by_account[credit["account_id"]] = credit
    except ApiException as e:
        body = json.loads(e.body) if e.body else {}
        code = body.get("error_code", "UNKNOWN")
        if code != "NO_LIABILITY_ACCOUNTS":
            label = item.get("institution_name", "unknown")
            print(
                f"[fetch_data] no liabilities for {label}: {code} "
                f"({body.get('error_message', e)}) -- credit cards will show "
                f"without a payment due date",
                file=sys.stderr,
            )

    for account in balance_resp["accounts"]:
        account_type = account["type"].value if hasattr(account["type"], "value") else str(account["type"])
        balances = account["balances"]
        entry = {
            "account_id": account["account_id"],
            # Which bank this came from. The per-bank fallback needs to
            # pick one item's accounts out of the cached set, and
            # account_id alone does not say who they belong to.
            "item_id": item.get("item_id"),
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
    except Exception as e:
        # Everything, not just ApiException. This runs in a thread pool
        # whose results are consumed by pool.map, so anything else --
        # urllib's socket errors when the network drops mid-run, a
        # KeyError on a response shape Plaid changed -- came back out of
        # map() and aborted the whole run, losing the banks that had
        # already succeeded and leaving data.json as it was. Per-bank
        # failure is the point of this function; the kind of failure is
        # not something the caller can act on differently.
        error_body = {}
        if isinstance(e, ApiException):
            try:
                error_body = json.loads(e.body) if e.body else {}
            except ValueError:
                error_body = {}
        error = {
            "institution_name": label,
            "item_id": item.get("item_id"),
            "error_code": error_body.get("error_code")
            or ("UNKNOWN" if isinstance(e, ApiException) else type(e).__name__),
            "error_message": error_body.get("error_message")
            or (str(e) or "Could not reach Plaid"),
        }
        print(f"[fetch_data] Error fetching {label}: "
              f"{error['error_code']}", file=sys.stderr)
        return [], error, False


# Serializes main(). There are two callers inside the widget process now --
# the tray's Reload (via the server's /refresh) and the interval timer that
# replaced the Task Scheduler job -- and they can land at the same moment.
# Both end by rewriting config.json and data.json wholesale, so overlapping
# runs could interleave those writes, and the loser's fetch would be wasted
# work anyway. Not reentrant: nothing calls main() from inside main().
_fetch_lock = threading.Lock()

# The in-process lock above only serializes callers inside one process.
# Setup runs in its own process and needs to know whether a fetch is
# running right now, because changing which profiles are enabled while
# one is in flight would have it write the old set's numbers into
# data.json after the switch. A file is the only channel the two
# processes share.
FETCH_LOCK_PATH = os.path.join(CACHE_DIR, "fetch.lock")

# A fetch that dies without cleaning up must not block switching forever.
# Comfortably longer than any real run: the work is a couple of Plaid
# calls per bank, concurrently.
STALE_LOCK_SECONDS = 600


def fetch_in_progress():
    """Whether a fetch is running, as far as another process can tell.

    Stale locks are ignored rather than cleaned up here -- the reader has
    no business deleting the writer's file, and the next real fetch
    overwrites it anyway."""
    try:
        age = time.time() - os.path.getmtime(FETCH_LOCK_PATH)
    except OSError:
        return False
    return age < STALE_LOCK_SECONDS


def _claim_lock():
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(FETCH_LOCK_PATH, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()} {datetime.datetime.now().isoformat()}")
    except OSError as e:
        # Not fatal: the worst case is that setup does not wait for this
        # fetch, which is the behaviour it had before the lock existed.
        print(f"[fetch_data] could not write {FETCH_LOCK_PATH}: {e}", file=sys.stderr)


def _touch_lock():
    """Push the lock's timestamp forward.

    Called between profiles. The staleness check is on mtime, so with
    enough profiles -- each one a sequential pass of its own -- a running
    fetch could age past STALE_LOCK_SECONDS and setup would decide it had
    died and start changing which profiles are enabled underneath it.
    Touching it keeps "stale" meaning dead rather than slow."""
    try:
        os.utime(FETCH_LOCK_PATH, None)
    except OSError:
        pass


def _release_lock():
    try:
        os.remove(FETCH_LOCK_PATH)
    except OSError:
        pass


def main():
    with _fetch_lock:
        _claim_lock()
        try:
            return _main()
        finally:
            _release_lock()


def fetch_profile(profile):
    """One profile's full fetch: every item, concurrently.

    Returns (accounts, errors, config_changed). Raises nothing -- a
    profile whose credentials Plaid refuses comes back with an error per
    item and no accounts, which is what lets one bad profile cost only
    itself."""
    profile_id = profile["id"]
    items = profile.get("items") or []

    old_snapshot = load_config_snapshot(profile_id)
    snapshot_items_by_key = {
        (it.get("item_id") or it.get("access_token")): it
        for it in old_snapshot.get("items", [])
    }

    try:
        client = build_client(profile)
    except ValueError as e:
        # A profile with an environment the Plaid SDK does not know. Only
        # reachable by hand-editing config.json, but it must not take the
        # other profiles' fetch down with it.
        print(f"[fetch_data] skipping profile {profile_id}: {e}", file=sys.stderr)
        return [], [{
            "institution_name": profiles.label(profile),
            "item_id": None,
            "error_code": "BAD_ENVIRONMENT",
            "error_message": str(e),
        }], False

    accounts_out = []
    errors = []
    changed_any = False
    stale_any = False

    cached = read_profile_cache(profile_id) or {}
    cached_by_item = {}
    for account in cached.get("accounts") or []:
        cached_by_item.setdefault(account.get("item_id"), []).append(account)
    cached_at = cached.get("last_updated")

    def work(item):
        force_logo = logo_fields_changed(item, snapshot_items_by_key)
        return process_item(client, item, force_logo)

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(items))) as pool:
        results = list(pool.map(work, items))

    for item, (accounts, error, changed) in zip(items, results):
        if error:
            errors.append(error)
        if changed:
            changed_any = True
        if accounts:
            for account in accounts:
                account["stale"] = False
            accounts_out.extend(accounts)
            continue
        if not error:
            # Reached, and it has nothing: an Item whose accounts were all
            # closed. Not a failure, and not something to paper over with
            # old numbers.
            continue
        # This bank alone could not be reached. Its last good balances
        # stay on the widget, marked stale, so one expired login does not
        # make those cards disappear as though the bank had been
        # unlinked. Per bank rather than per profile: the other banks on
        # the same profile have already refreshed by here, and falling
        # back for all of them would throw away numbers that are good.
        fallback = cached_by_item.get(item.get("item_id")) or []
        for account in fallback:
            account["stale"] = True
            # When the numbers are actually from, as opposed to when this
            # run happened. Carried forward once set, so a bank that has
            # been failing for a week does not look like it refreshed an
            # hour ago.
            account.setdefault("as_of", cached_at)
        if fallback:
            stale_any = True
            accounts_out.extend(fallback)

    if changed_any:
        # Only the items, and only this profile's. A backfilled
        # institution_id is the sole thing a fetch ever writes back.
        save_config(profile)

    # Next to the read above, because the two are one mechanism: this is
    # what the per-bank fallback falls back to. Written even when some
    # banks fell back, because their accounts are in accounts_out too --
    # the cache has to keep carrying them or a second consecutive failure
    # would have nothing left to show. Skipped for a profile with no
    # items at all: there is nothing to fetch, and an empty write would
    # discard the balances a re-enabled profile redraws from.
    if items:
        write_profile_cache(profile_id, accounts_out, errors)

    # Snapshot reflects this run's final state (post-backfill), so the
    # next run's diff is against what actually ended up on disk.
    save_config_snapshot(profile, profile_id)
    return accounts_out, errors, changed_any, stale_any


def stamp(accounts, profile, label):
    """Record which profile each account came from.

    Needed for three things: telling two accounts apart when the same
    bank is linked in two profiles, letting the widget caption a row when
    more than one profile is enabled, and keying the merged layout
    order."""
    for account in accounts:
        account["profile"] = profile["id"]
        account["profile_label"] = label
    return accounts


def read_profile_cache(profile_id):
    """The last good fetch for one profile.

    What a profile falls back to when this run could not reach it, so one
    expired secret shows stale numbers and a warning rather than making
    those accounts vanish."""
    path = profile_data_path(profile_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_profile_cache(profile_id, accounts, errors):
    path = profile_data_path(profile_id)
    payload = {
        "last_updated": datetime.datetime.now().isoformat(),
        "accounts": accounts,
        "errors": errors,
    }
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def _main():
    profiles.ensure_migrated()
    config, secrets_store = profiles.read_store()
    active = profiles.enabled(config, secrets_store)
    if not active:
        # SystemExit rather than a return: serve.py's /refresh already
        # catches it, and an empty data.json would blank a widget that was
        # drawing perfectly good accounts a second ago.
        sys.exit(
            "No Plaid profile is enabled. Open setup and enable one, or "
            "add credentials if this is a fresh install."
        )

    # Only caption rows when there is something to disambiguate. With one
    # profile the widget should look exactly as it always has.
    show_labels = len(active) > 1

    all_accounts = []
    errors = []
    profile_states = []

    for profile in active:
        _touch_lock()
        label = profiles.label(profile)
        accounts, profile_errors, _changed, stale = fetch_profile(profile)

        if stale:
            print(
                f"[fetch_data] {label}: some banks could not refresh; "
                f"showing their last good balances",
                file=sys.stderr,
            )

        all_accounts.extend(stamp(accounts, profile, label if show_labels else None))
        errors.extend(profile_errors)
        profile_states.append({
            "id": profile["id"],
            "label": label,
            "environment": profile.get("environment"),
            # Nothing came back at all, from any of its banks.
            "ok": not (bool(profile.get("items")) and not accounts),
            "stale": stale,
            "accounts": len(accounts),
        })

    data = {
        "last_updated": datetime.datetime.now().isoformat(),
        "accounts": all_accounts,
        "errors": errors,
        # What the dashboard needs to decide whether to caption rows at
        # all, and to warn by name about a profile that could not refresh.
        "profiles": profile_states,
        "show_profile_labels": show_labels,
    }

    # Same temp-then-replace as the per-profile caches: the dashboard
    # polls this file, and a half-written one is a visible flash of
    # nothing.
    tmp = f"{DATA_PATH}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, DATA_PATH)

    def note(state):
        if state["ok"]:
            return " (stale)" if state["stale"] else ""
        # Failed with something to fall back on, versus failed with
        # nothing: the first shows old numbers, the second shows none, and
        # the log should not call both the same thing.
        return " (stale)" if state["stale"] else " (failed)"

    summary = ", ".join(
        f"{s['label']}: {s['accounts']}{note(s)}" for s in profile_states
    )
    print(
        f"Wrote {len(all_accounts)} accounts, {len(errors)} errors to "
        f"data.json [{summary}]"
    )


if __name__ == "__main__":
    main()
