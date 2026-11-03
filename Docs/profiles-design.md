# Plaid profiles – design

Replaces the single set of Plaid credentials with named profiles. One or
more can be enabled at once, so work and personal accounts show on one
widget, and switching to sandbox is just "enable the sandbox profile".

Decided in design discussion, 3 Nov 2026, and built the same day. This is
the design as agreed; `Docs/README.md` documents what shipped, and where
the two disagree the code is right.

Three things changed on the way, in review:

- Section headings and row rules stayed in `appearance.json`, because they
  are settings for a section rather than for a profile. The widget merges
  them under the saved order.
- Order keys are namespaced always, and the per-profile layout files are
  gone (read once, then adopted). Switching between one and two enabled
  profiles changed the shape of every key, which silently threw a
  hand-sorted order away; hiding moved into the shared file for the same
  reason.
- A restore whose client ID and environment match a profile that is still
  here merges into it instead of creating a second one. One client ID in
  one environment is one Plaid team, and two profiles pointing at it would
  double-count the Item allowance and draw every account twice.

## Model

A profile is a client ID + environment pair with its own secret, linked
items, Item counter, cached balances and layout. Items record which
profile they came from, so a fetch groups items by profile, builds one
Plaid client per profile and merges the results.

Profile IDs are short opaque random strings, allocated at creation and
never reused. A deleted profile's ID must never be handed to a new one –
it would inherit the old cached data file and any orphaned item.

Profiles are labelled `<environment> ·<last 4 of client ID>`, or by an
optional nickname with the auto label shown beneath it.

Sandbox is exclusive. Production profiles combine freely; ticking a
sandbox profile unticks everything else and vice versa, enforced as the
checkbox behaves rather than as an error on save. Fake balances must never
sum into a real total.

## Files

    config.json          non-secret, safe to share or paste
      profiles: { <id>: { environment, nickname, enabled,
                          items, items_created } }
      port, fetch_interval_minutes
    secrets.json         credentials only
      { <id>: { client_id, secret } }
    appearance.json      global look – colours, fonts, sizes
    layout.<id>.json     legacy; adopted into the merged file on read
    layout.merged.json   the order and hidden list for every profile,
                         keys always namespaced "<id>:Bank,last4"
    data.json            merged live balances, path unchanged
    data.<id>.json       that profile's cached balances
    snapshot.<id>.json   that profile's logo-diff snapshot
    backups/             restore points, holds live credentials

Order and hiding both live in the merged file. Enabling or disabling a
profile only changes which of its keys are drawn, so a hand-sorted order
survives either way.

There is no top-level credential mirror. The file split forces it: a
mirror cannot include a secret that lives in another file. A shared
resolver joins config.json to secrets.json and returns the enabled
profiles; `fetch_data.py`, `plaid_link_setup.py` and `widget.py` all go
through it instead of reading `config["client_id"]`.

## Item counting

Plaid's Trial cap of 10 Items is per team, so it follows the client ID,
not the profile. Two production profiles sharing a client ID share one
allowance, and the UI sums them to show the true figure. Sandbox profiles
count separately and never roll into it. See
`.trail/learnings/2026-11-03-save-and-unlink-banks-is-local-aaryan.md`.

## Behaviour

Linking asks which profile to link into when more than one is enabled,
and skips the question when only one is.

Enabling or disabling waits for any in-flight fetch to finish, up to a
timeout, so a fetch cannot write the wrong profile's numbers into the
live data.json. Afterwards the widget restarts itself and a fetch runs in
the background.

If Plaid rejects a profile's secret as it is enabled, the page asks
whether to enable it anyway or stay as you are.

If one enabled profile fails mid-fetch, the healthy profiles update, the
failing one keeps its cached numbers, and the widget flags it by name.

Editing a profile's client ID or secret never unlinks its banks – a
different client ID is a different profile. Per-profile actions: edit
credentials, unlink all banks, delete, and reset for sandbox profiles
only (clears the test link count, unlinks its banks, drops its cached
data).

## Widget

With one profile enabled the widget is unchanged. With two or more, each
account shows its profile under the bank name, aligned with the bank name
rather than the logo, smaller and greyed.

The credit-card row at `source/dashboard.js:171` has the logo and
`bank-name` as siblings with no wrapper, so it needs one to take a second
line; the debit row at `:286` already has a wrapper. `nameNeedsOwnRow`
at `:220` sizes rows by name length, so row height needs revisiting.

## Backups

A backup holds the profile's client ID, secret and every item's access
token, so it can genuinely restore a bank. Because unlinking is local
only and nothing calls `/item/remove`, the Item stays alive on Plaid and
access tokens do not expire – a restore recovers the bank without
spending a new Item. The files hold live credentials, must stay
gitignored, and the UI says so.

Taken automatically before anything destructive, from a manual "Back up
now" button, and before an update or uninstall. Uninstall offers it
ticked; untick and confirm and it is deleted.

Restored from a Backups section in Advanced listing date, profile, bank
count and whether it is restorable. A restore recreates the profile under
a fresh ID, or merges into the live profile on the same client ID and
environment, and verifies the tokens with Plaid before committing.

Last 5 kept. Manual backups are never pruned.

## Migration

First run of the new version backs up the existing config.json to
backups/, then rewrites it as one enabled profile: secret moved to
secrets.json, cached data copied to data.<id>.json, layout lifted out of
appearance.json into layout.merged.json with keys namespaced, and
items_created carried over. Linked banks and the Trial count are
preserved. Silent, with the backup as the way back.

## Open

Appearance stays global, so with a sandbox profile enabled there is no
visual cue beyond the per-account profile label. Worth revisiting if
mistaking test balances for real ones turns out to be easy. The label does
at least name the profile, and sandbox runs alone, so every card on screen
carries it rather than one or two.
