"""
Plaid profiles: the credential sets this app can draw from.

A profile is one Plaid client ID in one environment, with its own secret,
its own linked items, its own Item counter, its own cached balances and
its own layout. Several can be enabled at once, which is what lets work
and personal accounts share a widget, and "switch to sandbox" is just
enabling a sandbox profile instead of a production one.

Two files, joined only in memory:

  config.json   { "profiles": { "<id>": { "environment", "nickname",
                                          "enabled", "items",
                                          "items_created" } },
                  "retired_profile_ids": [ ... ],
                  "port", "fetch_interval_minutes" }

  secrets.json  { "<id>": { "client_id", "secret" } }

The split is so config.json can be read, pasted into a bug report or
diffed without leaking a Plaid secret. It also rules out the obvious
shortcut of mirroring the live credentials at the top of config.json for
old readers: a mirror cannot carry a secret that lives in another file,
so every reader goes through resolve() instead.

Nothing here talks to Plaid. resolve() returns dicts shaped exactly like
the flat config that build_client() has always taken, which is what keeps
fetch_data.py and plaid_link_setup.py from having to care how many
profiles exist.
"""

import datetime
import json
import os
import secrets as _secrets
import shutil
import sys
import time

from paths import (
    APPEARANCE_PATH,
    BACKUPS_DIR,
    CONFIG_PATH,
    CONFIG_SNAPSHOT_PATH,
    DATA_PATH,
    MERGED_LAYOUT_PATH,
    SECRETS_PATH,
    ensure_data_dir,
    profile_data_path,
    profile_layout_path,
    profile_snapshot_path,
)

ENVIRONMENTS = ("production", "sandbox")

# Six bytes, so twelve hex characters. Long enough that a fresh id
# colliding with a retired one is not something to think about, and short
# enough to read in a filename. Never shown in the UI -- the switcher
# shows a nickname or the last 4 of the client ID.
_ID_BYTES = 6


# --------------------------------------------------------------- store io

def _read_json(path, fallback):
    """Missing and unreadable are the same answer here: callers get the
    fallback either way. A corrupt config must not take the widget down
    with a traceback at import time."""
    if not os.path.exists(path):
        return fallback
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return fallback


def _write_json(path, payload):
    """Write via a temp file in the same folder, then replace.

    os.replace is atomic on Windows as well as POSIX, so a crash or a
    full disk mid-write leaves the previous file intact rather than a
    half-written one. That matters more here than anywhere else in the
    app: a truncated secrets.json loses access to every linked bank, and
    unlike data.json it cannot be rebuilt by fetching again."""
    ensure_data_dir()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, path)


def read_config():
    return _read_json(CONFIG_PATH, {}) or {}


def read_secrets():
    return _read_json(SECRETS_PATH, {}) or {}


def write_config(config):
    _write_json(CONFIG_PATH, config)


def write_secrets(store):
    _write_json(SECRETS_PATH, store)


def read_store():
    """Both files, for the common case of needing credentials with the
    profiles they belong to.

    Migrates first. Every endpoint that writes a profile goes through
    here, and one of them writing a "profiles" key to a config that is
    still flat would make needs_migration() false forever -- stranding
    the original credentials and their access tokens at the top of the
    file with no migration left to move them."""
    return ensure_migrated(), read_secrets()


# ------------------------------------------------------------ ids, labels

def new_id(config=None):
    """An id no profile has ever had in this install.

    Checked against retired ids as well as live ones, because reuse would
    silently hand a new profile the deleted one's cached balances, its
    layout file and any item still pointing at it. That would look like
    corruption rather than a bug, so it is worth the bookkeeping."""
    config = config if config is not None else read_config()
    taken = set(profiles(config)) | set(retired_ids(config))
    while True:
        candidate = _secrets.token_hex(_ID_BYTES)
        if candidate not in taken:
            return candidate


def retired_ids(config):
    """The retired list, or an empty one for anything that is not a list.

    Everything else in this module degrades rather than raises on a
    corrupt config; this was the gap. A string here would become a set of
    its characters, and retired ids would quietly stop being honoured --
    which is the one thing that lets an id be reused."""
    value = (config or {}).get("retired_profile_ids")
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def retire_id(config, profile_id):
    """Record an id as spent. Callers do this as part of deleting a
    profile; it is separate so a caller that fails partway cannot leave
    the id live while its files are gone."""
    retired = list(config.get("retired_profile_ids") or [])
    if profile_id not in retired:
        retired.append(profile_id)
    config["retired_profile_ids"] = retired
    return config


def profiles(config=None):
    """The profiles mapping, never None. Insertion order is the order the
    UI lists them and the order their accounts merge in."""
    config = config if config is not None else read_config()
    found = config.get("profiles")
    return found if isinstance(found, dict) else {}


def client_id_tail(client_id):
    """The last 4 of a client ID, which is how two profiles on different
    Plaid teams are told apart at a glance."""
    return (client_id or "")[-4:]


def auto_label(profile):
    tail = client_id_tail(profile.get("client_id"))
    env = profile.get("environment") or "?"
    return f"{env} ·{tail}" if tail else env


def label(profile):
    """What the UI calls this profile.

    A nickname when there is one, because "work" reads better on the
    widget than four hex digits; the auto label otherwise, which is
    always available and never needs typing."""
    nickname = (profile.get("nickname") or "").strip()
    if nickname:
        return nickname
    return auto_label(profile)


# -------------------------------------------------------------- resolving

def resolve(profile_id, config=None, secrets_store=None):
    """One profile, credentials included, shaped for build_client().

    Returns None when the id is unknown or has no saved credentials -- a
    profile without a secret cannot be fetched for, and callers treat
    that the same as it not being there.

    The returned dict deliberately carries the same keys the flat
    config.json used to: environment, client_id, secret, items,
    items_created. That is what lets build_client(), fetch_logo() and the
    link flow stay as they were."""
    config = config if config is not None else read_config()
    secrets_store = secrets_store if secrets_store is not None else read_secrets()
    profile = profiles(config).get(profile_id)
    if not profile:
        return None
    creds = secrets_store.get(profile_id) or {}
    if not creds.get("client_id") or not creds.get("secret"):
        return None
    return {
        "id": profile_id,
        "environment": profile.get("environment"),
        "client_id": creds["client_id"],
        "secret": creds["secret"],
        "nickname": profile.get("nickname"),
        "enabled": bool(profile.get("enabled")),
        "items": profile.get("items") or [],
        "items_created": items_created(profile),
    }


def enabled_ids(config=None):
    config = config if config is not None else read_config()
    return [pid for pid, p in profiles(config).items() if p.get("enabled")]


def enabled(config=None, secrets_store=None):
    """Every enabled profile that can actually be used, in list order.

    Profiles missing credentials are dropped rather than raising: that is
    the state a half-finished "add profile" leaves behind, and it should
    cost you the one profile, not the whole fetch."""
    config = config if config is not None else read_config()
    secrets_store = secrets_store if secrets_store is not None else read_secrets()
    out = []
    for pid in enabled_ids(config):
        resolved = resolve(pid, config, secrets_store)
        if resolved:
            out.append(resolved)
    return out


def sole_enabled(config=None, secrets_store=None):
    """The one enabled profile, or None if there is anything other than
    exactly one. Callers that genuinely cannot handle a merge -- the link
    flow picking a default target, say -- use this to decide whether they
    need to ask."""
    found = enabled(config, secrets_store)
    return found[0] if len(found) == 1 else None


def active(config=None, secrets_store=None):
    """The profile a single-profile caller should act on.

    The sole enabled one, or failing that the first usable profile in
    list order. This exists so the parts of the app that genuinely only
    ever deal with one credential set -- the link flow, a manual fetch --
    have something to ask for without reimplementing the choice. Anything
    that draws balances uses enabled() and merges instead."""
    config = config if config is not None else read_config()
    secrets_store = secrets_store if secrets_store is not None else read_secrets()
    found = enabled(config, secrets_store)
    if found:
        return found[0]
    for pid in profiles(config):
        resolved = resolve(pid, config, secrets_store)
        if resolved:
            return resolved
    return None


def save_items(profile_id, items, items_created=None):
    """Write a profile's linked banks back to config.json.

    Takes the id rather than a whole config so two processes writing
    different profiles cannot clobber each other's work: the file is
    re-read here, one profile is touched, and the rest is left as found.
    That is as far as this goes towards locking -- a fetch and a link
    running at the same instant can still race, which is why the UI waits
    for an in-flight fetch before changing which profiles are enabled."""
    config = read_config()
    profile = profiles(config).get(profile_id)
    if profile is None:
        print(
            f"[profiles] refusing to save items for unknown profile {profile_id}",
            file=sys.stderr,
        )
        return config
    profile["items"] = items
    if items_created is not None:
        profile["items_created"] = items_created
    write_config(config)
    return config


def save_resolved(resolved):
    """Persist the mutable parts of a dict that came out of resolve().

    The readers hand their resolved profile around as if it were the old
    flat config, mutating items in place; this is the write that used to
    be save_config(). Credentials are deliberately not written back --
    they are only ever changed deliberately, through the credentials
    endpoint, never as a side effect of a fetch."""
    if not resolved or not resolved.get("id"):
        return None
    return save_items(
        resolved["id"],
        resolved.get("items") or [],
        resolved.get("items_created"),
    )


def any_resolvable(config=None, secrets_store=None):
    config = config if config is not None else read_config()
    secrets_store = secrets_store if secrets_store is not None else read_secrets()
    return any(resolve(pid, config, secrets_store) for pid in profiles(config))


def configured(config=None, secrets_store=None):
    """True once there is at least one usable profile. Replaces the old
    "config.json has a client_id and a secret" test that decided whether
    setup still needs to run."""
    return any_resolvable(config, secrets_store)


# --------------------------------------------------------- item accounting

def items_created(profile):
    """How many Plaid Items this profile has ever created.

    Same reasoning as the single-config version it replaces: profiles
    written before the counter existed have no record of banks they
    linked and later removed, so they fall back to the number still
    linked. That under-reports rather than over-reports, which is the
    right way round -- it never tells someone they are out of Items when
    they are not."""
    profile = profile or {}
    return max(
        profile.get("items_created", 0),
        len(profile.get("items") or []),
    )


def trial_items_used(config, secrets_store, client_id):
    """Items spent against one Plaid team's Trial allowance.

    The cap of 10 belongs to the team, so it follows the client ID rather
    than the profile: two production profiles on the same client ID
    really do share one allowance, and showing either one's own count
    would under-report how much is left. Sandbox profiles are free and
    never counted, whatever client ID they are on."""
    total = 0
    for pid, profile in profiles(config).items():
        if profile.get("environment") == "sandbox":
            continue
        creds = secrets_store.get(pid) or {}
        if creds.get("client_id") == client_id:
            total += items_created(profile)
    return total


# ------------------------------------------------------------- environment

def sandbox_conflict(config, enabling=None):
    """Whether the enabled set would mix sandbox with production.

    Sandbox balances are invented, so summing them next to real ones is a
    mistake that is hard to spot after the fact. Production profiles
    combine freely; a sandbox profile runs on its own. Pass `enabling` to
    test a set that includes a profile not yet marked enabled."""
    ids = set(enabled_ids(config))
    if enabling:
        ids.add(enabling)
    known = profiles(config)
    envs = {(known.get(pid) or {}).get("environment") for pid in ids}
    # Two sandbox profiles conflict as well: "runs on its own" means
    # alone, not merely unmixed. Comparing the number of environments
    # read a pair of sandboxes as agreement.
    return "sandbox" in envs and len(ids) > 1


# ------------------------------------------------------------------ layout

def read_layout(profile_id):
    """That profile's account order and hidden lists, or {} if it has
    never had any. Shaped exactly like the block that used to live under
    appearance.json's "layout" key, so the page and the dashboard are
    unchanged by the move."""
    if not profile_id:
        return {}
    return _read_json(profile_layout_path(profile_id), {}) or {}


def write_layout(profile_id, layout):
    if not profile_id:
        return
    _write_json(profile_layout_path(profile_id), layout or {})


def layout_key(profile_id, key):
    """An order key tied to the profile that owns the account.

    Always namespaced, whether one profile is drawing or five. The same
    bank linked in two profiles produces the same "Bank,last4", so a
    plain key would move or hide both cards at once. Namespacing
    unconditionally also means the keys do not change shape when a
    profile is switched on or off, which is what used to silently
    discard a hand-sorted order at the one-to-two boundary."""
    return f"{profile_id}:{key}"


def read_merged_layout():
    """The one layout block: section settings, the global order and the
    hidden lists. Keys are namespaced by profile id."""
    return _read_json(MERGED_LAYOUT_PATH, {}) or {}


def write_merged_layout(layout):
    _write_json(MERGED_LAYOUT_PATH, layout or {})


def drop_layout_keys(profile_id):
    """Take a deleted profile's keys out of the shared layout.

    Its id is retired and never comes back, so every key carrying that
    prefix names a card nothing can ever draw. Left behind they are not
    merely untidy: the keys of a profile that is not currently drawing
    are deliberately kept, so a deleted profile's would have sat in the
    order and the hidden list forever, growing by one profile every time
    one was removed. Section settings are left alone -- a title belongs
    to the section, not to whoever happened to save it."""
    layout = read_merged_layout()
    if not layout:
        return
    prefix = f"{profile_id}:"
    changed = False
    for section, block in layout.items():
        if not isinstance(block, dict):
            continue
        for key in ("order", "hidden"):
            keys = block.get(key)
            if not isinstance(keys, list):
                continue
            kept = [k for k in keys if not str(k).startswith(prefix)]
            if len(kept) != len(keys):
                block[key] = kept
                changed = True
    if changed:
        write_merged_layout(layout)


def _adopt_per_profile_layout(config=None):
    """The shared layout built from the per-profile files it replaced.

    Installs written while the order lived in layout.<id>.json have plain
    keys there. They are namespaced on the way out rather than rewritten
    on a read: a read that writes is a read that can fail, and the next
    save persists this anyway. Every profile is read, not just the
    enabled ones, so a switched-off profile's hidden accounts survive."""
    config = config if config is not None else read_config()
    out = {}
    for pid in profiles(config):
        own = read_layout(pid)
        for section, block in (own or {}).items():
            if not isinstance(block, dict):
                out.setdefault(section, block)
                continue
            block = dict(block)
            into = out.setdefault(section, {})
            # Section settings -- title, row rules -- describe the
            # section rather than the profile, so the first profile to
            # name one wins and the rest are ignored. Merging them would
            # mean picking between two titles for one heading.
            for key, value in block.items():
                if key not in ("order", "hidden"):
                    into.setdefault(key, value)
            for key in ("order", "hidden"):
                into[key] = (into.get(key) or []) + [
                    layout_key(pid, k) for k in (block.get(key) or [])
                ]
    return out


def shared_layout(config=None, secrets_store=None):
    """The layout every reader uses, however many profiles are enabled.

    Falls back to the per-profile files for an install that has not saved
    since the order went global."""
    layout = read_merged_layout()
    if layout:
        return layout
    return _adopt_per_profile_layout(config)


def merge_order(full, visible, drawable=None):
    """A reordered visible list folded back into the global order.

    The page only shows accounts belonging to enabled profiles, so a
    saved order has to be merged into the full list rather than replace
    it -- replacing would delete the entries of every profile switched
    off. Each key that is not currently drawable is anchored to the
    drawable key in front of it and travels with it, which is what gives
    the wanted rule for a move across a hidden entry: the hidden one ends
    up at the bottom of the movement rather than stranded in the middle
    of it."""
    full = list(full or [])
    visible = list(visible or [])
    shown = set(visible) if drawable is None else set(drawable)

    # Anchor every non-drawable key to the drawable key preceding it.
    leading, anchored, anchor = [], {}, None
    for key in full:
        if key in shown:
            anchor = key
        elif anchor is None:
            leading.append(key)
        else:
            anchored.setdefault(anchor, []).append(key)

    out = list(leading)
    for key in visible:
        out.append(key)
        out.extend(anchored.pop(key, []))
    # Anchored to a key this save dropped altogether: kept, at the end,
    # rather than losing a switched-off profile's entry with it.
    for leftover in anchored.values():
        out.extend(leftover)

    seen, deduped = set(), []
    for key in out:
        if key not in seen:
            seen.add(key)
            deduped.append(key)
    return deduped


def layout_with_settings(base, saved):
    """The saved layout, keeping section settings from appearance.json.

    Only the order and the hidden lists moved out to the profile layout;
    the titles and the row-break thresholds never did, and on a fresh
    install they are still only in appearance.json. Returning the saved
    block on its own retitled "Checking & Savings" to the hardcoded
    default the first time anything was saved. Order and hidden are not
    taken from the base: its keys are un-namespaced and name nothing."""
    base = base if isinstance(base, dict) else {}
    saved = saved if isinstance(saved, dict) else {}
    out = {}
    for section in set(base) | set(saved):
        from_saved = saved.get(section)
        from_base = base.get(section)
        if not isinstance(from_saved, dict) and not isinstance(from_base, dict):
            # "_comment", or anything else hand-written that is not a
            # section. Passed through as it is: turning it into {} made
            # it look like a section to every writer downstream, and the
            # first one to meet the string still on disk raised.
            out[section] = from_saved if section in saved else from_base
            continue
        settings = {
            k: v for k, v in (from_base if isinstance(from_base, dict) else {}).items()
            if k not in ("order", "hidden")
        }
        settings.update(from_saved if isinstance(from_saved, dict) else {})
        out[section] = settings
    return out


def widget_layout(config=None, secrets_store=None):
    """The layout the widget draws with.

    None rather than {} when there is nothing saved, so the caller can
    tell "no layout yet" from "saved, and deliberately empty" and only
    reach for appearance.json in the first case."""
    layout = shared_layout(config, secrets_store)
    return layout if layout else None


# ----------------------------------------------------------------- backups

def backup_path(name):
    """A free filename for a backup.

    Suffixed until it is unused. The stamp is only second-resolution and
    the label is not unique -- nicknames are never checked for
    uniqueness, and the fallback label is the environment plus the last 4
    of the client ID. Backing up every profile loops in one tick, so two
    profiles with the same label used to land on one filename and
    _write_json's os.replace kept only the second, losing the first
    profile's credentials and tokens for good."""
    stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name)
    base = os.path.join(BACKUPS_DIR, f"{stamp}-{safe}")
    candidate = f"{base}.json"
    n = 2
    while os.path.exists(candidate):
        candidate = f"{base}-{n}.json"
        n += 1
    return candidate


def write_backup(payload, name):
    """A restore point. Holds credentials and access tokens by design --
    see BACKUPS_DIR in paths.py for why, and .gitignore for the file
    patterns that keep it out of the repo."""
    os.makedirs(BACKUPS_DIR, exist_ok=True)
    path = backup_path(name)
    _write_json(path, payload)
    # Pruned after the write, not before, so the backup just taken is one
    # of the five kept rather than the first one considered for removal.
    prune_backups()
    return path


# How many automatic backups to keep. Five covers "I broke it just now"
# without letting a folder of access tokens grow forever. Manual ones and
# the pre-migration one are never pruned: someone asked for those, or
# they are the only way back from a one-way change.
MAX_AUTO_BACKUPS = 5
KEEP_FOREVER = ("manual", "pre-migration")


def _safe_backup_name(name):
    """A plain filename inside BACKUPS_DIR, or None.

    Backup names reach this from an HTTP request, so anything with a path
    separator in it is refused rather than cleaned -- the names this
    writes never contain one."""
    name = (name or "").strip()
    if not name or not name.endswith(".json"):
        return None
    if name != os.path.basename(name) or name.startswith("."):
        return None
    return name


def read_backup(name):
    """One backup's payload, or None if the name is bad or unreadable."""
    safe = _safe_backup_name(name)
    if not safe:
        return None
    payload = _read_json(os.path.join(BACKUPS_DIR, safe), None)
    return payload if isinstance(payload, dict) else None


def backup_profile_payload(payload):
    """The profile a backup can restore, whatever shape it is in.

    Per-profile backups carry "profile" and "credentials" directly. The
    pre-migration one carries the whole flat pre-profiles config instead,
    and it is the most valuable backup there is, so it gets read rather
    than listed as unrestorable."""
    payload = payload or {}
    profile = payload.get("profile")
    creds = payload.get("credentials")
    if isinstance(profile, dict):
        return dict(profile), dict(creds or {})
    config = payload.get("config")
    if isinstance(config, dict) and config.get("client_id"):
        return (
            {
                "environment": config.get("environment") or "production",
                "nickname": config.get("nickname") or "",
                "items": config.get("items") or [],
                "items_created": config.get("items_created", 0),
            },
            {
                "client_id": config.get("client_id") or "",
                "secret": config.get("secret") or "",
            },
        )
    return {}, {}


def backup_view(name, payload):
    """What the Backups list shows. Never includes a secret or a token."""
    profile, creds = backup_profile_payload(payload)
    items = profile.get("items") or []
    tokens = sum(1 for item in items if item.get("access_token"))
    return {
        "name": name,
        "kind": payload.get("kind") or "backup",
        "taken": payload.get("taken") or "",
        "label": payload.get("label") or label({**profile, **creds}),
        "profile_id": payload.get("profile_id") or "",
        "environment": profile.get("environment") or "",
        "banks": len(items),
        "client_id_tail": client_id_tail(creds.get("client_id")),
        # Restorable means the backup holds everything a working profile
        # needs. A bank comes back only with its access token, so a
        # backup with credentials but no tokens still restores -- it just
        # restores an empty profile.
        "restorable": bool(creds.get("client_id") and creds.get("secret")),
        "banks_restorable": tokens,
        "kept": (payload.get("kind") or "") in KEEP_FOREVER,
    }


def list_backups():
    """Every readable backup, newest first.

    Filenames start with a sortable timestamp, so a reverse name sort is
    the order they were taken -- no need to trust mtime, which a copy or
    a restore from elsewhere would scramble."""
    try:
        names = sorted(os.listdir(BACKUPS_DIR), reverse=True)
    except OSError:
        return []
    out = []
    for name in names:
        if not name.endswith(".json"):
            continue
        payload = read_backup(name)
        if payload is None:
            continue
        out.append(backup_view(name, payload))
    return out


def delete_backup(name):
    safe = _safe_backup_name(name)
    if not safe:
        return False
    try:
        os.remove(os.path.join(BACKUPS_DIR, safe))
        return True
    except OSError:
        return False


def prune_backups():
    """Drop automatic backups past the newest MAX_AUTO_BACKUPS.

    Returns the names removed. Manual and pre-migration backups are
    skipped entirely, so a shelf of manual ones never pushes the
    automatic ones out and is never pushed out itself."""
    auto = [b for b in list_backups() if not b["kept"]]
    removed = []
    for backup in auto[MAX_AUTO_BACKUPS:]:
        if delete_backup(backup["name"]):
            removed.append(backup["name"])
    return removed


def find_by_credentials(client_id, environment, config=None, secrets_store=None):
    """The id of the profile holding these credentials, or None.

    A client ID in an environment is one Plaid team, and a team's Items
    and its Item allowance are shared. Two profiles pointing at the same
    team would double-count the allowance and draw every account twice,
    so adding and restoring both go through this."""
    config = config if config is not None else read_config()
    secrets_store = secrets_store if secrets_store is not None else read_secrets()
    if not client_id:
        return None
    for pid, profile in profiles(config).items():
        creds = secrets_store.get(pid) or {}
        if (
            creds.get("client_id") == client_id
            and profile.get("environment") == environment
        ):
            return pid
    return None


def _merge_items(existing, incoming):
    """Existing items, plus any from the backup that are not already
    there. Matched on item_id, which is Plaid's own identifier, so a bank
    linked once cannot come back as two cards. Existing entries win: they
    carry whatever logo choice has been made since the backup."""
    out = list(existing or [])
    known = {item.get("item_id") for item in out if item.get("item_id")}
    for item in incoming or []:
        if item.get("access_token") and item.get("item_id") not in known:
            out.append(item)
            known.add(item.get("item_id"))
    return out


def restore_backup(payload, switch_on=False):
    """Write a backup's profile back as a new profile. Returns its id.

    Always a new id, never the one the backup came from: that id may
    still be in use, or retired, and either way reusing it would make two
    different profiles indistinguishable in old data files. The banks come
    back with it because their access tokens are still valid -- nothing
    here ever called /item/remove -- so this costs no Plaid Items.

    If a profile on the same client ID and environment is still here, the
    banks are merged into it and its id is returned instead: that is one
    Plaid team, and two profiles pointing at it would double-count the
    Item allowance and draw every account twice.

    Switched off by default. A restore that silently started drawing a
    second set of accounts onto the widget would be a surprise, and a
    sandbox restore would have to switch everything else off to do it."""
    profile, creds = backup_profile_payload(payload)
    if not creds.get("client_id") or not creds.get("secret"):
        raise ValueError("That backup has no saved credentials to restore.")

    config = read_config()
    environment = profile.get("environment") or "production"
    if environment not in ENVIRONMENTS:
        environment = "production"
    items = [item for item in (profile.get("items") or []) if item.get("access_token")]
    secrets_store = read_secrets()

    existing_id = find_by_credentials(
        creds["client_id"], environment, config, secrets_store
    )
    if existing_id:
        # The profile this backup came from is still here, so the banks go
        # back into it rather than into a copy. A second profile on the
        # same client ID would count the same real Items twice against the
        # Trial allowance and draw every account on the widget twice.
        live = profiles(config)[existing_id]
        live["items"] = _merge_items(live.get("items"), items)
        live["items_created"] = max(
            items_created(live),
            profile.get("items_created", 0),
            len(live["items"]),
        )
        write_config(config)
        return existing_id

    profile_id = new_id(config)
    if not isinstance(config.get("profiles"), dict):
        config["profiles"] = {}
    config["profiles"][profile_id] = {
        "environment": environment,
        "nickname": profile.get("nickname") or "",
        "enabled": bool(switch_on),
        "items": items,
        # Kept from the backup, because those Items are spent on Plaid
        # whatever happens locally. max() guards a backup written before
        # the counter existed.
        "items_created": max(profile.get("items_created", 0), len(items)),
    }


    secrets_store[profile_id] = {
        "client_id": creds["client_id"],
        "secret": creds["secret"],
    }
    # Secrets first, same order as migrate(): a crash between the two
    # leaves an orphaned secret, which is harmless, rather than a profile
    # listed with no way to read it.
    write_secrets(secrets_store)
    write_config(config)
    return profile_id


# --------------------------------------------------------------- migration

def needs_migration(config=None):
    """An install written before profiles existed: credentials flat at the
    top of config.json and no profiles mapping."""
    config = config if config is not None else read_config()
    return bool(config.get("client_id")) and not config.get("profiles")


def migrate(config=None):
    """Turn a flat pre-profiles config.json into one enabled profile.

    Backs the old config up first, then moves the secret out to
    secrets.json, the cached balances to data.<id>.json, the layout out
    of appearance.json to layout.<id>.json, and the snapshot alongside.
    The Item counter and every linked bank carry over untouched.

    Silent and automatic: there is no sensible alternative to migrating,
    and the backup is the way back if anything here is wrong. Idempotent,
    so running it on an already-migrated config does nothing."""
    config = config if config is not None else read_config()
    if not needs_migration(config):
        return config, None

    backup = write_backup(
        {
            "kind": "pre-migration",
            "taken": datetime.datetime.now().isoformat(timespec="seconds"),
            "config": config,
        },
        "pre-migration",
    )

    profile_id = new_id(config)
    environment = config.get("environment") or "production"
    items = config.get("items") or []

    secrets_store = read_secrets()
    secrets_store[profile_id] = {
        "client_id": config.get("client_id") or "",
        "secret": config.get("secret") or "",
    }

    migrated = {
        key: value
        for key, value in config.items()
        # The flat credential and item keys are what this migration
        # exists to move. Everything else at the top level -- port,
        # fetch_interval_minutes, anything added later -- is shared
        # across profiles and stays exactly where it is.
        if key not in ("client_id", "secret", "environment", "items", "items_created")
    }
    migrated["profiles"] = {
        profile_id: {
            "environment": environment,
            "nickname": None,
            "enabled": True,
            "items": items,
            "items_created": items_created(config),
        }
    }
    migrated.setdefault("retired_profile_ids", [])

    # Copied, not moved: until config.json names the new profile,
    # appearance.json's own copy is the only layout anything can find.
    # It is stripped below, once the migration is committed. Doing it
    # here meant a crash before write_config left the layout stranded in
    # a dead profile's file, with the retried migration finding nothing.
    _migrate_layout(profile_id)
    _copy_if_present(DATA_PATH, profile_data_path(profile_id))
    _copy_if_present(CONFIG_SNAPSHOT_PATH, profile_snapshot_path(profile_id))

    # secrets.json first: if the process dies between the two writes, the
    # old config.json is still intact and still has its own copy of the
    # credentials, so the install keeps working and the migration simply
    # runs again. The other order would leave a config with no reachable
    # secret.
    write_secrets(secrets_store)
    write_config(migrated)
    _drop_appearance_layout()
    print(f"[profiles] migrated to profile {profile_id}; backup at {backup}")
    return migrated, profile_id


def _migrate_layout(profile_id):
    """Lift appearance.json's layout block into the profile's own file.

    Keys are copied as they are. They only need namespacing in the merged
    order file, where several profiles share one list; a per-profile file
    cannot collide with anything."""
    appearance = _read_json(APPEARANCE_PATH, None)
    if not isinstance(appearance, dict):
        return
    layout = appearance.get("layout")
    if layout is None:
        return
    # Into the shared file, namespaced, since that is where the order
    # lives now. The per-profile file is written as well, so an older
    # build run against this install still finds a layout.
    _write_json(profile_layout_path(profile_id), layout)
    shared = {}
    for section, block in layout.items():
        # appearance.json's layout block ships with a "_comment" string
        # in it, and a hand-edited file can hold others. Copied through
        # untouched rather than treated as a section: dict() on a string
        # raised, and the exception came out of the migration -- the one
        # operation in here that must not fail, because it runs once on
        # an install that has no profiles yet.
        if not isinstance(block, dict):
            shared[section] = block
            continue
        block = dict(block)
        for key in ("order", "hidden"):
            block[key] = [layout_key(profile_id, k) for k in (block.get(key) or [])]
        shared[section] = block
    write_merged_layout(shared)


def _drop_appearance_layout():
    """Take the layout key out of appearance.json, once the migration it
    belongs to has been committed."""
    appearance = _read_json(APPEARANCE_PATH, None)
    if not isinstance(appearance, dict) or "layout" not in appearance:
        return
    appearance.pop("layout", None)
    _write_json(APPEARANCE_PATH, appearance)


def _copy_if_present(src, dest):
    if not os.path.exists(src):
        return
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(src, dest)
    except OSError as e:
        print(f"[profiles] could not copy {src} -> {dest}: {e}", file=sys.stderr)


_MIGRATION_LOCK_STALE_SECONDS = 120


def _migration_lock_path():
    return os.path.join(os.path.dirname(os.path.abspath(CONFIG_PATH)), "migrate.lock")


def _claim_migration_lock():
    """True if this process got the lock. A lock older than two minutes
    is treated as abandoned: a migration is two small writes, so one that
    old belongs to a process that died holding it."""
    ensure_data_dir()
    path = _migration_lock_path()
    try:
        if time.time() - os.path.getmtime(path) > _MIGRATION_LOCK_STALE_SECONDS:
            os.remove(path)
    except OSError:
        pass
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return False
    os.close(fd)
    return True


def _release_migration_lock():
    try:
        os.remove(_migration_lock_path())
    except OSError:
        pass


def ensure_migrated():
    """Called by anything that reads credentials, so whichever process
    starts first does the migration and the rest find it done.

    Locked across processes: the widget's fetch thread and the setup
    server both start at launch and both call this. Two migrations racing
    would mint two ids and have the second write_secrets replace the
    first, leaving config.json naming a profile whose credentials are not
    in secrets.json -- an install that drops back to the first-run wizard
    with its banks reachable only through the pre-migration backup."""
    config = read_config()
    if not needs_migration(config):
        return config

    if not _claim_migration_lock():
        # Someone else has it. Wait for them rather than race, and hand
        # back to the caller after a few seconds regardless: a stuck
        # migration must not hang the widget's launch.
        for _ in range(60):
            time.sleep(0.1)
            config = read_config()
            if not needs_migration(config):
                return config
        return config

    try:
        config = read_config()
        if needs_migration(config):
            config, _ = migrate(config)
    finally:
        _release_migration_lock()
    return config
