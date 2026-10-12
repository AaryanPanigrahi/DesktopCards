# Personal Balance Dashboard

A small pinned desktop widget showing live bank/credit card balances, built
on Plaid for real account data.

## What it does

- Pulls balances for every linked bank/credit card account via Plaid.
- Shows checking/savings accounts (logo, bank name, last 4 digits, balance)
  and credit cards (same, plus a usage bar and next payment due date).
  Logos come straight from Plaid (or the bank's own favicon as a fallback).
- Colors the numbers by how much attention they deserve: credit card
  utilization ramps grey → yellow → deep red as it climbs, and a
  checking/savings balance that's nearly spent or overdrawn turns yellow or
  red (see Warning colors below).
- Sits pinned to a fixed spot on screen as a frameless, always-on window
  with no taskbar icon, and stays visible through Win+D ("show desktop").
- Refreshes itself automatically — no manual resizing or repositioning
  needed as accounts are added or removed.
- A system tray icon gives you "Reload" (re-pull from Plaid right now),
  "Restart" (relaunch the whole widget — for settings that only apply at
  startup, like `survive_show_desktop`), and "Quit" — the widget itself
  can't be clicked while pinned through Win+D (see Why below), so this is
  the manual control surface.
- The time-since-last-refresh display also counts down the minutes left
  until the next scheduled refresh.

## Pieces

| File | Role |
|---|---|
| `plaid_link_setup.py` | One-time (per bank) setup — opens Plaid's real Link widget in a browser so you log into your bank directly; saves the resulting access token. |
| `fetch_data.py` | Pulls balances + credit card details from Plaid for every linked account, writes `data.json`. The only script that talks to Plaid. Runs on an interval timer inside the widget, not continuously. |
| `serve.py` | Minimal local web server so the dashboard page can read its JSON files. Serves only an explicit allowlist of files — `config.json` (which holds the Plaid secret and access tokens) is never reachable over HTTP. Also expands `appearance.json` into what the page needs on every request. Normally hosted as a thread inside `widget.py`; still runnable on its own for debugging the page. |
| `widget.py` | Launches the pinned window itself: hosts the server, opens a frameless WebView2 window at the configured position/size, hides it from the taskbar, pins it behind the desktop icons so it survives Win+D, clips it to rounded corners, runs the interval fetch, and runs the system tray icon (Reload/Restart/Quit). |
| `dashboard.html` / `dashboard.js` | The widget's UI and all its behavior (rendering, auto-refresh, auto-resize). |
| `paths.py` | Where every file lives. Splits read-only bundled assets from writable per-user data, which a packaged build requires and a source checkout collapses back into one folder. |
| `platform_win.py` | Every Windows-specific call in one file — rounded-corner clipping, taskbar hiding, desktop pinning, the accent-colour registry read. Isolated so another platform means writing a sibling, not unpicking this one. |
| `accent_color.py` | Reads your current Windows accent color and writes it into `appearance.json`. |
| `desktopcards.py` | The entry point. Every mode is a flag on this one file (`--serve`, `--fetch`, `--link`, `--accent`, `--setup`, or nothing for the widget). |
| `setup_app.py` | First-run setup and the management view, as a local web page — credentials, Plaid Link, a verifying fetch, install and shortcuts. |
| `logo_cache.py` | Caches the derived copies of `logo.png` (tray PNG, shortcut `.ico`) in `cache/`. |
| `build.py` / `DesktopCards.spec` / `make_icon.py` | Produce `dist/DesktopCards.exe` — see Building the executable. |
| `appearance.json` | Appearance, window placement, and layout/ordering settings, all in one file (see below). **Gitignored** — it holds your personal monitor position, same reasoning as `config.json`. |
| `appearance.example.json` | A placeholder-position template of `appearance.json`, for the same sharing reason as `config.example.json`. |
| `cache/logos/` | Cached institution logos (one PNG per bank). Gitignored, like `config.json`. |
| `cache/config_snapshot.json`, `cache/appearance_snapshot.json` | `config.json`/`appearance.json` as of the end of the last run — how `fetch_data.py`/`widget.py` tell exactly what changed (see How a refresh actually happens). Gitignored. |
| `config.json` | Your Plaid credentials, access tokens, and the settings below. **Gitignored** — never committed. |
| `config.example.json` | A credential-free template of `config.json`, meant for sharing this tool with someone else: they copy it to `config.json` and fill in their own values rather than yours. |

## Configuration (`config.json`)

| Key | Meaning |
|---|---|
| `client_id`, `secret`, `environment` | Your Plaid API credentials and environment (`sandbox` or `production`). |
| `items` | The linked banks' access tokens, populated automatically by `plaid_link_setup.py`. Each one also carries `use_custom_logo`/`logo_url` — see Logos below. |
| `port` | Local port `serve.py` runs on. |
| `fetch_interval_minutes` | How often to re-pull from Plaid. Read at launch by the widget's own timer, so a change applies on the next Restart — there's no schedule registered with Windows to re-apply. |

## Appearance, window, and layout (`appearance.json`)

Kept separate from `config.json` — nothing here is a credential, but it is
personal to your own screen setup, so it's gitignored the same way (see
`appearance.example.json` for the sharable, placeholder-position version).

| Key | Meaning |
|---|---|
| `accent` | The dashboard's highlight color. Kept here for reference; regenerated by `accent_color.py` from your current Windows accent color. |
| `corner_radius` | How rounded the widget's outer corners are, in pixels. `0` = square. `widget.py` clips the actual OS window to this rounded shape (not just the page's own CSS), so the desktop shows through the corners instead of a square box around them. |
| `scale` | Uniform size multiplier for the whole dashboard (text, padding, icons together). `1.0` = default size. |
| `extra_spacing` | Breathing room applied to all four sides equally. Used when `custom_spacing` is `false`. Negative values pull content in tighter than the default. |
| `custom_spacing` | `false` (default) uses `extra_spacing` on all sides. `true` switches to the per-side `spacing` object instead. |
| `spacing.left` / `.top` / `.right` / `.bottom` | Independent breathing room per side, only used when `custom_spacing` is `true`. Negative values allowed. |
| `shadow.enabled` | Turns on the hard offset drop shadow behind every card (plus a matching border around it). `false` leaves the cards flat. See Card shadow below. |
| `shadow.offset` | How far the shadow is thrown down and to the right, in pixels. There's no blur — it's a solid block, so this is the whole of its size. |
| `shadow.border` | Thickness of the outline drawn around each card in the same color, in pixels. `0` gives the shadow without the outline. |
| `shadow.color` | Color of both the shadow and that border. Leave it out and it follows the theme — near-black on light, a darker near-black on dark. Setting it pins one color across both themes. |
| `dark_theme` | `true` swaps the whole palette for the dark one. See Dark theme below. |
| `translucent_background` | `true` lets the desktop wallpaper show through the widget. See Translucency below. |
| `opacity` | How opaque the widget is, `0`–`1`, when `translucent_background` is on. Not applied uniformly — the page backdrop takes this value in full and the cards, bars and text give up progressively less of theirs, so text stays readable. `1` switches the whole effect off (see Translucency), and anything below `0.15` is clamped to it. |
| `window.x` / `.y` | Top-left corner of the widget, in screen pixels — pick a spot on whichever monitor you want it on. |
| `window.survive_show_desktop` | Keeps the widget visible through Win+D ("show desktop"). |
| `layout.depository.title` / `layout.credit.title` | The section header shown above each group of cards. |
| `layout.depository.order` / `layout.credit.order` | Which cards appear (and in what order) within that section — a list of `"Bank Name,last4"` strings (e.g. `"Chase,0000"`), matched against what's actually on the dashboard. Accounts not listed just get appended at the end. |
| `layout.depository.new_row_digit_threshold` | How many digits a checking/savings balance needs before that card drops onto a row of its own instead of sitting two-up (default `6`, i.e. $100,000+). See Oversized balances below. |

Width and the max-height safety ceiling aren't configuration — they're fixed
constants in `widget.py` (`WINDOW_WIDTH`, `MAX_HEIGHT`). There's no
"natural" width to derive (text just reflows to whatever width it's given),
and the actual height always comes from the page measuring its own
rendered content, not from a tuned number. `extra_spacing`/`spacing` above
is what controls breathing room. The two things that *do* move the width
are an oversized balance needing more room and `shadow` needing somewhere
to paint — both described below.

## Oversized balances

Checking/savings cards sit two per row, in a box sized for balances up to
$9,999.9. Bigger numbers are handled the same way a long word is handled in
a line of text — slightly too long and it stays on the line, far too long
and it drops to its own line:

- **5 digits** ($10,000–$99,999.9): that card's box widens in place and
  keeps its row. Its row partner stays the width it already was, and the
  window (and therefore the credit cards) grows by the same amount, so the
  credit card and a full debit row stay the same width as each other.
- **`new_row_digit_threshold` digits or more** (6+ by default, i.e.
  $100,000+): that card drops onto a row of its own, as wide as a credit
  card. It stays where it was in the order — the cards around it just pair
  up again after it, rather than being reshuffled.

Raising `new_row_digit_threshold` makes cards widen in place for longer
before breaking out; lowering it makes them break out sooner. This applies
to the default ordering and to a hand-written `layout.depository.order`
alike.

Editing `appearance.json` takes effect on the dashboard's next load for
appearance — `serve.py` reads it fresh on every request, so there's no sync
script to re-run. Window placement is read once at launch, so changes there
need `widget.py` restarted.

## Card shadow

With `shadow.enabled`, each card gets a solid, un-blurred shadow thrown down
and to the right, plus an outline in the same color — the neo-brutalist
treatment from the Uiverse source card this UI started from (see References).
Defaults reproduce that card exactly: a 10px offset, a 3px border, `#323232`.
Smaller values (4px/2px) read as a subtler lift rather than a graphic slab.

**The window grows to fit it.** A shadow is painted outside the card's own
box, so without that the right-hand column of it would simply have been cut
off at the window edge. The widget widens by `offset + border × 2` — the
border counts twice because `box-sizing: border-box` otherwise takes it out
of the card's *inner* width and squeezes the balance text. That number lives
in two places that have to agree: `--shadow-grow` in `dashboard.html` (which
widens the page) and `shadowGrow` in `dashboard.js` (which asks `widget.py`
for a wider window, since that width is computed from `BASE_WIDTH` rather
than measured from the rendered page).

Those two halves of the growth go to different places, which matters more
than it sounds: the **offset** is whitespace for the shadow to paint into, so
it goes on `#cards`' right padding, while the **border** is width the cards
give back to their own insides, so it goes to the content box. Putting the
border on the padding too (as this first did) leaves a visibly wider gutter
on the right than the left. Done properly, the gutter is 12px on both sides
either way, and a credit card's inner width is identical shadowed or not.
`--debit-base` and `debitBaseWidth()` in `dashboard.js` both have to carry
that same border term, or a two-up checking/savings row ends short of the
credit card above it. Side-by-side debit boxes do end up 3px narrower inside
than unshadowed — the one squeeze that can't be given back without widening
the window further.

The gaps between cards grow by the offset too, otherwise each card would be
painted over the shadow of the one above it. The one exception is the
horizontal gap between two side-by-side checking/savings cards, which is
pinned at 8px because `distributeRowWidths` does its arithmetic against it —
so at a large offset the right-hand card covers the far end of its
neighbour's shadow. That reads as the shadow tucking underneath it, which is
what the style does anyway.

## Dark theme

`"dark_theme": true` and the whole widget swaps palette. It's one flag, not a
set of colors, because every color the stylesheet paints already came from a
`--surface-*` / `--text-*` variable — so the theme is a single
`:root[data-theme="dark"]` block in `dashboard.html` that redefines those
variables, and no rule below it needs a dark variant. `dashboard.js` only
sets the attribute.

Three things don't ride along on a CSS variable and are handled explicitly:

- **The warning colors** (`WARN_PALETTES` in `dashboard.js`) are computed per
  card and written as inline styles, so they can't come from the stylesheet.
  There are two palettes with *identical hues*, the dark one lifted in
  lightness and eased off in saturation: the deep red at 38% lightness that
  reads as urgent on white is nearly invisible on a dark card, and the
  fully-saturated yellow glows. Same hues in both is what keeps the two
  themes feeling like one widget.
- **The window's own fill** (`page_background` in `widget.py`) is the color
  shown for the moment before the page paints, and in the sliver between the
  page's rounded box and the window's clipped one. pywebview defaults it to
  white, which flashed a bright frame on every launch and Restart. It tracks
  `--surface-0` per theme. (Translucency makes this moot — a transparent
  window has no fill at all. See Translucency.)
- **The shadow color**, which is why `SHADOW_DEFAULTS` in `dashboard.js` has
  no color in it: an inline style written from JS beats the theme block and
  would pin the light value in both themes, so the stylesheet owns that one.

Some deliberate choices in the palette itself:

- **Not black.** The cards sit one step lighter than the page behind them,
  the same figure/ground step that white-on-grey gives in the light theme —
  that step is what makes them read as cards instead of regions of one dark
  sheet. A near-black page also makes the rounded corners dissolve into the
  wallpaper rather than float on it.
- **A cool blue-grey, not a neutral one**, to match the light theme's slate
  text, so switching themes doesn't also change the widget's temperature.
- **`--accent` is left alone.** It's your live Windows accent color;
  recoloring it per theme would be overriding a choice you already made in
  Windows itself.
- **`--text-primary` stops short of `#fff`**, or the big balance numbers
  bloom against the dark card.
- **The card darkens a step when the shadow is off** (`[data-shadow="off"]`,
  set by `applyShadow`). With the shadow on, its dark border wraps each card
  in a thin frame that visually holds the surface down; without the frame,
  the same grey reads as a brighter slab. The two states are different colors
  so that they look equally bright. The page behind is untouched — it's the
  card face that gains the brightness, not the whole widget. Light theme
  needs none of this: on white, a borderless card reads as paper, which is
  the point.

Unlike window placement, this takes effect on the dashboard's next load — but
`page_background` above is read at launch, so switching themes is worth a
`widget.py` restart to avoid a light flash on the next one.

## Translucency

`"translucent_background": true` plus an `"opacity"` between `0` and `1` lets
the desktop wallpaper show through the widget. It takes both halves to work,
and they fail in opposite directions if only one is done:

- **The window** has to stop painting a background, or the page's `rgba`
  colors have nothing to blend against but the window's own fill — the
  wallpaper is on the far side of the *window*, not the far side of the card.
  `widget.py` passes `transparent=True`, which pywebview's WinForms backend
  turns into `DefaultBackgroundColor = Color.Transparent` on the WebView2
  control. That gives genuine per-pixel alpha, not chroma-keying or a uniform
  window opacity, which is what makes a graded effect possible at all. Because
  the window is reparented onto WorkerW, what's behind it is the wallpaper.
- **The page** has to actually use alpha. Every color token in
  `dashboard.html` is now a `*-raw` name, and nothing downstream uses one
  directly: a derivation block turns each into the var the rules paint with,
  via `color-mix(in srgb, <raw> calc(var(--alpha-x) * 100%), transparent)`.
  That's what kept this to a token-layer change instead of an edit to all
  sixty-odd color usages, and `color-mix` against `transparent` is what lets
  it take a color in any format — these are hex, and `--accent-raw` is
  whatever `appearance.json` hands over.

### Four tiers, not one opacity

A single alpha applied uniformly reads as a faded screenshot rather than a
translucent panel, and the text becomes unreadable long before the effect
looks good. So one input drives four tiers, each giving back a fraction of
what was asked for (`TRANSLUCENCY_TIERS` in `dashboard.js`):

| tier | gives back | at `opacity: 0.8` |
| --- | --- | --- |
| page backdrop | nothing | 0.800 |
| card faces, shadow | 45% | 0.890 |
| accent, bars, bar track, logos | 80% | 0.960 |
| all text | 88% | 0.976 |

The page backdrop is the thing actually being made see-through, so it takes
the number at face value. Card faces give up most of theirs, since a card has
to still read as a slab sitting on the wallpaper rather than a tint of it. The
accent and text tiers give up only a little, because legibility against an
arbitrary wallpaper is the one thing translucency mustn't cost — but they do
give up some, so they belong to the same surface as everything else instead of
floating above it.

Two tier assignments are deliberate and non-obvious: the **shadow** follows the
card rather than the page, because it's an edge belonging to the card that cast
it and at page alpha it washed out while the card above it still read as solid;
and the **bar's track** is on the accent tier rather than the surface one, so
the whole progress bar fades as a single object instead of its background
dropping out from under its fill.

Three things can't ride on a CSS variable:

- **The warning colors**, same as in the dark theme — computed per card and
  written inline. They carry the accent tier's alpha through `hsl()` instead.
  `WARN_PALETTES.grey` became an `[h, s, l]` array like the other three for
  this: as a finished string it was the one color that would have stayed
  opaque while the rest went translucent.
- **The institution logos**, which are `<img>` elements rather than colors, so
  `.card-logo` takes the accent tier's alpha as an element `opacity`. Without
  it the logos stay fully solid while everything around them fades.
- **The section headings and the `updated` clock**, which get their own
  treatment below.

### The headings darken as it fades

Those two are the only text in the widget sitting on the page backdrop rather
than on a card, so at low opacity their real background is your wallpaper. A
faint grey that reads as a quiet label against a solid page turns into
unreadable haze over an arbitrary photo. So `--text-faint` is two nested
mixes: pulled toward near-black by `--heading-darken` *first*, then given the
text tier's alpha, so the darkening doesn't get diluted by it.
`--heading-darken` is `0` at full opacity and scales with how much opacity was
given up, capped at `HEADING_DARKEN_MAX = 0.7` — a pure black heading above
pale card text looks like a different widget's label pasted on top. Both
themes get this: in the dark theme the page is barely there at low opacity
anyway, so the heading's backdrop is the wallpaper either way.

### Off means off

`translucent_background: false` **or** `opacity: 1` switches the whole thing
off, in both files, and they have to agree — disagreeing would blend the page
into a window that isn't transparent (grey behind the cards) or vice versa. A
fully opaque translucency is translucency's cost with none of its effect, so
it's treated as off rather than honored literally.

Off is not just the alphas set to `1`. That would look identical but wouldn't
be free:

- In the page, a `:root[data-translucent="off"]` block maps every token
  straight through to its raw value, so none of the twelve `color-mix()` calls
  are in the chain at all. They're real work on each style recalc, and a
  `color-mix` result carries an alpha channel even at `1.000`, which keeps the
  card and page backgrounds on the compositor's blended path instead of
  letting it treat them as opaque fills. The attribute is seeded in the markup
  next to `data-shadow`, so the cheap path is also what you get before
  `dashboard.js` runs.
- In the window, `transparent=True` simply isn't passed. A transparent window
  is composited per-pixel against the desktop for its whole lifetime whether
  or not anything in the page is see-through.

On the off path `dashboard.js` *removes* the `--alpha-*` properties rather
than setting them to `1`: they're inline styles on `<html>`, so leaving them
behind would outrank the stylesheet's own values for the rest of the session,
and a tray Reload that turned the flag off would keep the old alphas alive.

The `0.15` floor on `opacity` exists because the widget is pinned to the
desktop layer and takes no mouse input — an opacity of `0` would render it
invisible with nothing left to click to undo it. A non-numeric value falls
back to fully opaque rather than guessing.

### A hazard worth knowing about

A transparent window on WorkerW has no background brush to erase with, and
when it shrinks, the thing that would have to repaint underneath is the
desktop, which never finds out it should — so the previous frame's pixels can
be left stranded outside the window's new bounds. `erase_vacated_area` in
`widget.py` handles that: `resize_window` captures the window rect *before*
resizing, then `RedrawWindow`s that region on the parent with
`RDW_ERASE | RDW_UPDATENOW`. It only runs when translucent, since an opaque
window gets this from DWM for free, and it no-ops when `survive_show_desktop`
is off because an unparented window doesn't have the problem.

Honest caveat: this was written for a ghost-text symptom that turned out to
have a completely different cause (stray text outside an HTML comment), so
while the mechanism is real, the code is **unverified against an actual
occurrence**. `SetWindowRgn` is worth noting here too — clipping the window
stops *it* painting outside its bounds, which is what would let such pixels
end up orphaned rather than overwritten.

Like the theme, the page side applies on the next dashboard load, but the
window side is set at `create_window` time — so toggling translucency needs a
`widget.py` restart (the tray's **Restart**), not just a Reload.

## Warning colors

All of it is computed in `dashboard.js` from the balances themselves —
nothing about severity is stored in `data.json`, so there's no stale
percentage to go out of sync with a freshly-fetched balance.

**Credit utilization** (the `%` to the right of the balance, and the usage
bar) shares one ramp:

| Utilization | Color |
|---|---|
| Below 27% | Grey — nothing to look at |
| 27–35% | Yellow |
| 35% → 100% | Yellow ramping to deep red |
| Above 100% | Stays deep red |

The ramp deliberately front-loads its move toward red (`URGENCY_EXP`), so
the middle of the range reads as a problem rather than a comfortable
amber. Utilization can exceed 100%, and the color simply holds at its
reddest there — there's nowhere redder to escalate to.

The bar and the `%` run the same ramp, so they can't disagree about
severity, but off two different yellows (`barYellow` vs `yellow`): a solid
block of color and 13px text need different intensities of the same hue to
look right. All four colors here come in a light and a dark version — same
hues, different lightness (see Dark theme) — and all four are `[h, s, l]`
arrays rather than finished color strings, so that `hsl()` can add the
translucency alpha on the way out (see Translucency).

**The usage bar's sections** are split at 27 / 35 / 66. The first two are
exactly where the color changes, so a divider lands on each change of
state; 66 marks the last third. Only the sections you've reached are
drawn: at 10% utilization you see one divider (the 27% line coming up), at
30% you see two (27 and 35). The stretch past the next divider stays one
unbroken block, so the bar marks the line you're about to cross instead of
laying out the whole ruler up front.

**Checking/savings balances** turn yellow at $10 or below, and red below
-$25 — the same yellow as a credit card's first warning band, since
"watch this" means the same thing on either card.

## Logos

For each linked item, `fetch_data.py` tries, in order: your own URL
(`logo_url`, only if `use_custom_logo` is `true` on that item in
`config.json`) → Plaid's own copy of the institution's logo → the bank's
public favicon via Google's favicon service off its own website. The
default (non-custom) lookup only actually runs once per institution,
though — once something's cached in `cache/logos/`, every later run just
reuses it, unless `institution_id`/`use_custom_logo`/`logo_url` actually
changed for that item since the last run (a new link counts too). If
every option fails on a run that does need a fresh fetch (bad URL, no
connection), it keeps whatever's already cached instead of blanking the
logo out over a transient failure. If nothing has ever been cached, the
dashboard falls back to a plain initial-letter avatar in your accent
color.

## How a refresh actually happens

Only `fetch_data.py` calls Plaid: on the widget's interval timer (every
`fetch_interval_minutes`), or on demand via the tray's "Reload". It makes 2
Plaid calls per linked bank (balance + liabilities), fetched for all your
banks at once rather than one after another, plus one extra (one-time)
logo lookup the first time a given institution has never been cached
before — after that, logos are never re-fetched, just reused. The widget's
own page just re-reads `data.json` every 5 minutes and whenever it loads —
no Plaid traffic from the UI itself, ever. The tray's "Reload" calls
`fetch_data.main()` directly inside `serve.py`'s already-running process
rather than launching a brand new Python process for it, which used to
cost more than the actual Plaid calls did.

Balances always get refreshed on "Reload" — that part's never skipped.
What *is* skipped when nothing's actually different: `cache/config_snapshot.json`
and `cache/appearance_snapshot.json` hold each file's state as of the last run,
so `fetch_data.py` only redoes a logo lookup for an item whose
`institution_id`/`use_custom_logo`/`logo_url` actually changed (or that's
brand new), and `widget.py` only re-clips/repositions the native window
when `appearance.json` actually differs from last time — both are otherwise
silent no-ops instead of redundant work on every single click.

### The interval timer

`fetch_loop` in `widget.py` re-pulls from Plaid every
`fetch_interval_minutes`, for the life of the widget. This used to be a
Windows Task Scheduler job registered by a `setup_schedule.py` script. The
job worked, but it was a separate artifact registered with the OS, pointing
at a script path, outliving any particular install and firing whether or not
the widget was running — writing a `data.json` nobody was reading.

The trade is that **balances only refresh while the widget runs.** What makes
that acceptable is the catch-up: if `data.json` is already older than one
interval, because the machine was off or the widget was closed, the timer
fetches immediately on launch rather than waiting out a full interval while
showing stale numbers.

A failed fetch (no network, a Plaid rate limit, an item needing re-auth) is
logged and skipped, never fatal — the loop has to survive to try again.

### Restart and the port

The tray's **Restart** is a full process relaunch, for the settings that are
only read once at startup (`survive_show_desktop`, `translucent_background`,
`page_background`, `fetch_interval_minutes`).

The server is a daemon thread inside the widget process, so there's nothing
to shut down first and nothing that can outlive its widget — an earlier
version spawned `serve.py` as a subprocess, which could be orphaned and sit
on the port through every later restart.

The replacement process still has to get the port back, though.
`serve.py` sets `allow_reuse_address = False` deliberately, so a second
instance can't quietly bind on top of a live one — which also means sockets
from the previous instance sitting in `TIME_WAIT` make the bind fail
outright, and Restart lands well inside that window. This used to be papered
over with a flat `time.sleep(1)`, which made the failure silent and baffling:
the server died, the sleep expired anyway, and the window came up pointed at
a port with nothing behind it, so the widget just disappeared with no error
anywhere (it runs under `pythonw`, so there's no console to print to).

`serve.build_server()` retries the bind instead, waiting half a second
between attempts — `TIME_WAIT` is a timer, not a contended lock, so waiting
it out is the fix and retrying faster wouldn't help. If the port never frees
up it raises, because that's a genuinely different problem (something else
owns it) and deserves to be reported rather than retried forever. It's bound
before the window is created, so that failure can't produce a window pointed
at nothing.

### One lock for two callers

The tray's "Reload" and the interval timer can both land on
`fetch_data.main()` at the same moment, and both end by rewriting
`config.json` and `data.json` wholesale. `fetch_data` holds a module-level
lock so the two serialize; the loser would have been wasted work anyway.

## Building the executable

```
python build.py
```

Produces two files in `dist/`, both 31.6 MB:

| File | What it's for |
|---|---|
| `DesktopCards.exe` | The widget. |
| `DesktopCards-Setup.exe` | First-run setup. |

They are **the same binary under two names.** `desktopcards.py` checks its
own filename: a name containing "setup" runs setup, anything else runs the
widget. So there's one build, one thing to download, and a new user can't
click the wrong file first — the launcher doesn't exist until setup has made
it. An explicit flag (`--serve`, `--fetch`, `--link`, `--accent`, `--setup`)
always wins over the name, so neither file is ever a dead end.

Everything is bundled: Python, plaid-python, Pillow, pystray, Flask, and
pywebview with its .NET interop. The only thing not carried is the WebView2
runtime, which is a built-in Edge component on Windows 11.

### One entry point

Three places used to spawn Python by name — `pythonw.exe serve.py`,
`pythonw.exe widget.py`, and a Task Scheduler command line. All three break
in a frozen build for the same reason: `sys.executable` *is* the app, it
takes no script argument, and there are no `.py` files on disk to point at.
`desktopcards.py` replaces them with subcommands, so `sys.executable` plus a
flag covers every case, from source and frozen alike.

### No console, so output goes to a file

The widget is a desktop ornament and can't have a console window behind it.
But PyInstaller sets `sys.stdout` and `sys.stderr` to `None` in a windowed
build rather than to something inert, so every `print()` in the codebase
would raise `AttributeError` on `None.write` — harmless progress messages
becoming crashes, in the build where they're hardest to see.

`redirect_output_to_log()` points both at
`%LOCALAPPDATA%\DesktopCards\desktopcards.log`. That also buys the one thing
this app otherwise can't produce: a record of what went wrong. pywebview's
known-worst failure mode is coming up as a blank window with no traceback
anywhere and no tray icon either — and the tray is the only control surface,
since the desktop layer the widget is pinned to receives no mouse input. The
log is the difference between diagnosing that and guessing.

It also means **setup can't be a terminal prompt.** It runs in the browser,
which is where the Plaid Link flow already lives.

### The icon

`logo.png` is the single source of truth for the mark. `make_icon.py`
generates `DesktopCards.ico` from it on every build — seven embedded sizes
from 16px to 256px, because Windows picks whichever fits the context it's
drawing in rather than scaling one badly.

The tray icon loads `logo.png` directly at runtime and downsizes it to 64px,
since the tray slot is 16–32px and the source is 1024px square. It falls
back to a drawn accent-coloured circle if the file can't be read — not dead
code, because a spec file that forgot to bundle `logo.png` would otherwise
leave the app with no tray icon, and therefore no way to control it.

**Replacing the logo.** From source, edit `logo.png` and it takes effect
immediately. In the packaged build the bundled copy is sealed inside the exe,
so dropping a `logo.png` into `%LOCALAPPDATA%\DesktopCards\` overrides it —
that path wins whenever it exists, and the tray resolves it per call rather
than at import. The log says which one it used.

The **exe's own icon is different** and can't work that way: Windows reads it
from a resource compiled into the executable, so changing it requires a
rebuild. `python build.py` regenerates the `.ico` from `logo.png` first, so a
rebuild is all it takes.

### Hidden imports

`desktopcards.py` imports each mode's dependencies inside its own branch, so
asking for `--fetch` doesn't load pywebview and the whole .NET interop layer.
That's good for startup cost and invisible to PyInstaller's static analysis —
a function-local import isn't one it can follow. The spec names them
explicitly, which is what keeps the non-default subcommands in the bundle.

The build also warns about missing `System`, `Microsoft.Web` and
`System.Drawing`. Those are .NET assemblies that pythonnet resolves at
runtime through its own import hook, not Python modules, so PyInstaller
can't see them statically. They're expected and harmless.

### Known rough edges

- **Unsigned**, so the first run trips SmartScreen: *Windows protected your
  PC* → More info → Run anyway. A code-signing certificate is the only real
  fix.
- Onefile costs a second or two of unpacking per launch, irrelevant for
  something launched once and left pinned.
- Rebuilding while the widget is running fails, because PyInstaller replaces
  the exe and Windows won't allow that for a file that's executing.
  `build.py` checks first and says so plainly. It checks both names — they're
  the same binary, and the setup copy is just as likely to be the one left
  open.
- **A hard kill leaves a temp directory behind.** A onefile exe unpacks
  itself to `%TEMP%\_MEIxxxxx` on every launch and deletes it on exit, but
  only on a *clean* exit. `Stop-Process -Force` (or a crash) skips the
  cleanup, and each orphan is a full ~35MB copy of the bundle. Quitting from
  the tray is clean; if you've been force-killing it during development,
  `Remove-Item $env:TEMP\_MEI* -Recurse -Force` reclaims the space.
- An **interrupted unpack** leaves the app present but missing its WebView2
  interop DLLs, which surfaces as `Cannot find
  Microsoft.Web.WebView2.Core.dll`. Relaunching fixes it, because the next
  run extracts again. `explain_webview_failure` says that in the log rather
  than letting a bare traceback name a DLL and offer no advice — this is the
  single most likely thing to go wrong in a frozen build, so it's worth
  handling by name.

## Setup, and adding accounts later

Run `DesktopCards-Setup.exe` (or `--setup` from source). It opens a page in
your browser and walks through five steps:

1. **Plaid credentials.** Validated against the real API *before* anything is
   written — creating a link token is the cheapest call that proves a
   client_id/secret pair works, and wrong credentials are the single most
   likely setup mistake. Failing here, with the fields still on screen,
   beats saving bad credentials and looking like a broken app later.
2. **Link a bank.** Plaid's own hosted widget, so your bank password never
   passes through DesktopCards. Repeatable — each run adds another bank.
3. **Check it works.** One real fetch. This is the step worth insisting on:
   everything before it can succeed and still leave you looking at an empty
   widget, and this is where that becomes a message instead of a mystery.
4. **Install.** Copies the exe to `%LOCALAPPDATA%\DesktopCards\` and writes
   a Start-menu shortcut, optionally a Startup one.
5. **Launch.**

Run it again and it skips to a **management view** instead: the linked banks,
with buttons to add or remove one and to redo the shortcuts.

The page is served on localhost only, and `/api/state` deliberately carries
no `client_id` or `secret` — the page never needs them back, and not sending
them means they can't end up in a browser cache or a screenshot.

### Why it's a web page

The packaged build is windowed, so there's no console to prompt in. And the
Plaid Link step has to happen in a browser regardless, because that's where
Plaid hosts it. Given one step is already a web page, all of them may as
well be.

### Adding a bank from the tray

The widget's tray menu has **Modify Accounts**, which opens that same
management view. That's the everyday route — nobody should have to find a
file in Downloads to add a card. It spawns a separate process on purpose, so
nothing about the setup page can take the widget down with it, and **the
widget keeps running throughout**: Link is on 8008/8009 while the dashboard
is on 5000, so there's no conflict. New banks appear on the next refresh.

Removing a bank drops its access token from `config.json` but does **not**
remove the Item at Plaid. That's deliberate — it's irreversible, and someone
re-adding the same bank a minute later shouldn't be punished for it.

### Launch when it's already running

Setup's **Launch widget** button becomes **Restart widget** when a widget is
already up, and does exactly what the tray's Restart does. Starting a second
copy isn't an option — the single-instance lock stops it, correctly — but
doing nothing is the wrong answer to a button press, and a restart is usually
what's wanted anyway, since the reason to press it after changing something
is to pick that change up.

It works by asking the running widget to relaunch itself: `POST /restart` on
the dashboard port, handled by the server thread living inside the widget
process. `widget.py` registers the hook once the tray and window exist; a
standalone `--serve` leaves it unset and correctly answers *"no widget in
this process"* rather than pretending. The response is sent **before** the
restart begins, since the restart tears down that very server.

### Spawning a replacement in a frozen build

Three separate bugs lived here, all invisible from source and all fixed:

- **Inherited unpack directory.** PyInstaller's bootloader tells child
  processes where it unpacked itself via `_PYI_*` environment variables.
  That's right for a child belonging to the same run, and wrong for a
  replacement meant to outlive us: the child skips extraction, uses our temp
  directory, and then we exit and the bootloader deletes it underneath.
  The symptom was `Cannot find Microsoft.Web.WebView2.Core.dll` — pointing
  nowhere near the cause. `clean_child_env()` strips them.
- **The working directory locked the temp folder.** `spawn_self` passed
  `cwd=PROJECT_DIR`, which frozen is `ASSET_DIR` — *this process's own
  `_MEIPASS`*. A process's current directory can't be deleted while it
  lives, so the old bootloader could never clean up and never exited:
  **one orphaned process and one undeletable ~35MB directory per restart.**
  The replacement now starts in the exe's own folder, which is stable.
- **Inherited handles.** The replacement is started detached
  (`DETACHED_PROCESS`, a new process group, `close_fds`) so nothing ties it
  to the process it's replacing.

Verified by restarting four times in a row and watching the counts hold
steady at two processes and one temp directory, with the server answering
each time.

`force_exit_soon()` is a backstop on both Restart and Quit: `window.destroy()`
is meant to end `webview.start()` and let `main()` return, and from source it
does. It's there in case pywebview's WinForms loop doesn't unwind, because a
Quit that leaves the process alive is worse than a blunt exit — the
single-instance lock would then block the next launch.

### Only one widget at a time

The app is launched from a Start-menu shortcut, a Startup shortcut, and
setup's Launch button, so a second instance is an ordinary event rather than
a mistake. `claim_single_instance` takes a named mutex before anything
touches the port; a second instance logs *"Already running"* and exits 0.

Without it the second process got as far as binding port 5000, failed, and
died with an `OSError` traceback in a popup — the loud failure is correct in
itself, but being shown it for double-clicking a shortcut is not.

A named mutex rather than a lock file because the kernel releases it however
the process dies, so a crash can't leave a stale lock that blocks every
future launch. The tray's Restart releases it explicitly *before* spawning
the replacement, since the old process is still alive at the moment the new
one checks — otherwise Restart would quietly kill the widget instead of
restarting it.

### Cached logo derivatives

`logo.png` is a 1024px square: the right thing to keep as the source, the
wrong thing to hand to anything that draws it. `logo_cache.py` writes two
derivatives into `cache/` and rebuilds them only when the logo actually
changes:

| File | Used for |
|---|---|
| `cache/logo_tray_64.png` | the tray icon |
| `cache/logo.ico` | the Start-menu and Startup shortcut icons |

Freshness is judged by **content hash, not modification time**. Under a
onefile build the bundled `logo.png` is extracted to a new temp directory on
every launch, so its mtime is always newer than anything cached — an mtime
check would rebuild both derivatives every single run, which is exactly what
the cache exists to avoid. Hashing one PNG costs far less than re-encoding
seven `.ico` sizes.

The widget rebuilds the `.ico` on startup if it's missing, so the shortcut
icons self-heal, and replacing `logo.png` updates them on the next launch.

## Why a few of the design choices are what they are

- **WebView2 instead of a Chrome window**: Chrome's `--app` mode windows
  always paint a small title bar themselves, independent of the OS window
  style — there's no supported way to remove it. Windows' built-in WebView2
  engine (driven here via `pywebview`) supports genuinely frameless windows
  natively, and has a lighter footprint than spinning up a full Chrome
  profile just to show one page.
- **Auto-resizing**: the window measures its own rendered content height
  after every refresh and resizes itself to match, capped at a maximum
  height. Adding or removing accounts, or changing `scale`/`extra_spacing`
  in `appearance.json`, never requires manually resizing the window again.
- **Accent color**: pulled live from the Windows registry rather than
  hardcoded, so the dashboard's highlight color follows whatever accent
  color you have set in Windows' own personalization settings.
- **Tray icon instead of an in-page button**: surviving Win+D means the
  window is reparented onto the desktop's own WorkerW layer, and that layer
  doesn't receive normal mouse input — the same reason Rainmeter/Wallpaper
  Engine skins need a system-wide mouse hook just to be clickable at all.
  A system tray icon is a normal window regardless of where the main
  widget is parented, so that's where manual Reload/Restart/Quit live
  instead.
- **Rounded corners via a window region, not just CSS**: `pywebview`'s
  `transparent` option only makes WebView2's own background transparent
  against the window's paint — it doesn't change the window's actual
  shape. Getting real rounded corners (desktop visible through them)
  needs the OS-level `SetWindowRgn` clip that `widget.py` applies instead.

## References

- The card layout (header row, balance, progress bar) is adapted from two
  community card designs on Uiverse.io: one for the debit/checking cards
  and one for the credit cards (which adds the usage bar). Their
  hardcoded colors become your Windows accent throughout, and the usage
  bar was reworked into the sectioned, severity-colored one described
  under Warning colors. Their hard offset shadow and border is the
  optional `shadow` setting (see Card shadow).
- Institution logos are fetched live via Plaid's institution lookup, with a
  fallback to the bank's own public favicon, and cached locally so they're
  only ever fetched once per institution, not on every `fetch_data.py` run.
