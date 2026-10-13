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
- Refreshes itself automatically – no manual resizing or repositioning
  needed as accounts are added or removed.
- A system tray icon gives you **Reload** (re-pull from Plaid right now),
  **Modify** (add or remove a bank, and edit every appearance setting from
  a page instead of by hand), **Restart** (relaunch the whole widget – for
  settings that only apply at startup, like `survive_show_desktop`), and
  **Quit**. The widget itself can't be clicked
  while pinned through Win+D (see Why below), so this is the manual control
  surface.
- The time-since-last-refresh display also counts down the minutes left
  until the next scheduled refresh.

## Getting started

You need your own free [Plaid](https://dashboard.plaid.com) developer
account – this uses your credentials, not someone else's. Setup walks you
through it.

**From a packaged build** (what you'd hand to someone else):

1. Run `DesktopCards-Setup.exe`. It opens a page in your browser.
2. Follow the five steps: Plaid credentials, link your banks, a test fetch,
   install, launch.

It installs itself to `%LOCALAPPDATA%\DesktopCards\` with a Start-menu
shortcut, and optionally starts with Windows. Unsigned, so the first run
trips SmartScreen – *More info → Run anyway*. See
[Setup, and adding accounts later](#setup-and-adding-accounts-later).

**From source:**

```
pip install -r requirements.txt
python source/desktopcards.py --setup     # first time: credentials + banks
pythonw source/desktopcards.py            # run the widget
```

`pythonw` rather than `python` so there's no console window behind the
widget. `desktopcards.py` is the only entry point – every mode is a flag on
it (`--serve`, `--fetch`, `--link`, `--accent`, `--setup`, or nothing for
the widget).

To build the exe yourself: `python source/build.py` (see
[Building the executable](#building-the-executable)).

## Where your files live

The repo is laid out by what a file is *for*; a frozen build flattens most of
that away:

| | From source | Packaged build |
|---|---|---|
| **Code and the page** (read-only) | `source/` | inside the exe, unpacked to a temp dir per launch |
| **The examples** (`config.example.json`, `appearance.example.json`) | `config/` | bundled in the exe |
| **Your data** (`config.json`, `appearance.json`, `data.json`, `cache/`, `desktopcards.log`) | `config/` | `%LOCALAPPDATA%\DesktopCards\` |
| **Docs** | `Docs/` | not shipped |
| **`logo.png`, `dist/`, the generated `.ico`** | repo root | n/a |

`paths.py` owns every one of those, which is why nothing else derives a path
from `__file__`: under a frozen build that lands in the unpack directory and
makes each launch look like a fresh install. Its four constants are
`ASSET_DIR` (code and page), `DATA_DIR` (your data), `EXAMPLES_DIR` (the
templates) and `REPO_ROOT` (the things that genuinely live at the top). From
source the examples sit in the same folder as the live files they're
templates for, which is the clearest place to find one to copy by hand; in the
bundle they're flattened in with everything else, which is the only reason
`EXAMPLES_DIR` exists separately at all.

The split is not optional for a packaged build: anything written into that
temp directory is deleted when the app exits.

`%LOCALAPPDATA%\DesktopCards\` is also where the installed `DesktopCards.exe`
and `Uninstall.exe` live, so the app, its uninstaller and its data are one
folder – which is what lets uninstalling be "delete this folder" (see
Uninstalling).

**The gotcha worth knowing:** if you're running the packaged build, editing
`config/appearance.json` in the repo does nothing – the exe reads the copy in
`%LOCALAPPDATA%\DesktopCards\`. Edit that one, then tray → Restart. Easier
still, use the Modify page, which always writes wherever the running app
actually reads (see Settings without editing JSON).

## Uninstalling

`Uninstall.exe`, written into `%LOCALAPPDATA%\DesktopCards\` beside the app
by the install step, and listed in **Settings → Apps** so the Uninstall button
there works. From a checkout there is no installed file to run, so it's
`python source/desktopcards.py --uninstall` instead.

It asks first, in a Windows task dialog – the kind Windows' own uninstallers
use, and the only native dialog that can carry a checkbox. **Keep my settings
and linked accounts** is ticked by default:

| | Ticked (default) | Cleared |
|---|---|---|
| `config.json` (Plaid keys, one access token per bank) | kept | deleted |
| `appearance.json`, a replaced `logo.png` | kept | deleted |
| `data.json`, `cache/`, `desktopcards.log` | deleted | deleted |
| `%LOCALAPPDATA%\DesktopCards\` itself | stays, holding what was kept | removed |

`data.json` goes either way: it's the last fetch's balances, regenerated
within a minute of the next launch, and stale balances are the one thing
nobody wants left behind. Either way your banks stay linked at Plaid –
removing an Item there is irreversible and isn't something an uninstaller
should decide (same reasoning as removing one bank from the Accounts tab).

Then, in this order, because the order is the only thing that makes it work:

1. **The widget is asked to quit**, through `POST /quit` on its own server –
   not killed. Its quit path stops the tray icon, releases the single-instance
   lock and lets its writes finish; a kill leaves that lock behind to block
   the next install. Any setup server gets `POST /api/quit`. `taskkill` is the
   backstop, used only if something is still answering afterwards.
2. **Shortcuts go** – the Start-menu entry and the Startup one. Both are just
   files, which is why `install_autostart` uses the Startup folder rather than
   a `Run` registry key.
3. **The Add/Remove entry goes**, from `HKCU`. Nothing here needs
   administrator: shortcuts live in `%APPDATA%`, the app and its data in
   `%LOCALAPPDATA%`, and the uninstall entry in the per-user hive.
4. **Data goes**, minus whatever the checkbox saved.
5. **The two exes go last, from outside the process.** Windows holds an
   executing image open, so the uninstaller can't delete the uninstaller. A
   detached `cmd` waits for this process to exit, then deletes both and
   `rmdir`s the folder – without `/s`, so a folder still holding kept
   settings is simply left alone.

It's the same binary as the app, a third time: the filename picks the mode.
That does mean one trap worth knowing about, since `Uninstall.exe` contains
the string `install` – `looks_like_uninstall_build()` is checked *before*
`looks_like_setup_build()`, or double-clicking the uninstaller would reinstall
the app instead.

## Pieces

Everything in the first group lives in `source/`, the JSON in `config/`, and
the docs in `Docs/`; only `logo.png` and the build output sit at the repo
root. Paths below are given bare for readability.

| File | Role |
|---|---|
| `plaid_link_setup.py` | One-time (per bank) setup – opens Plaid's real Link widget in a browser so you log into your bank directly; saves the resulting access token. |
| `fetch_data.py` | Pulls balances + credit card details from Plaid for every linked account, writes `data.json`. The only script that talks to Plaid. Runs on an interval timer inside the widget, not continuously. |
| `serve.py` | Minimal local web server so the dashboard page can read its JSON files. Serves only an explicit allowlist of files – `config.json` (which holds the Plaid secret and access tokens) is never reachable over HTTP. Loopback-bound and behind `net_guard`. Also expands `appearance.json` into what the page needs on every request. Normally hosted as a thread inside `widget.py`; still runnable on its own for debugging the page, where `POST /restart` and `POST /quit` correctly report that there is no widget here to act on. |
| `widget.py` | Launches the pinned window itself: hosts the server, opens a frameless WebView2 window at the configured position/size, hides it from the taskbar, pins it behind the desktop icons so it survives Win+D, clips it to rounded corners, runs the interval fetch, and runs the system tray icon (Reload / Modify / Restart / Quit). |
| `dashboard.html` / `dashboard.js` | The widget's UI and all its behavior (rendering, auto-refresh, auto-resize). |
| `paths.py` | Where every file lives. Splits read-only bundled assets from writable per-user data, which a packaged build requires and a source checkout collapses back into one folder. |
| `platform_win.py` | Every Windows-specific call in one file – rounded-corner clipping, taskbar hiding, desktop pinning, the accent-colour registry read, shortcuts, and the uninstaller's three OS pieces: the task dialog, the Add/Remove Programs entry, and the deferred self-delete. Isolated so another platform means writing a sibling, not unpicking this one. |
| `net_guard.py` | The one definition of "did this request really come from this machine?", shared by all three servers. Loopback binding keeps them off the network; this is what keeps the local browser from being used as a way in. See Who can reach these servers. |
| `accent_color.py` | Reads your current Windows accent color and writes it into `appearance.json`. |
| `desktopcards.py` | The entry point. Every mode is a flag on this one file (`--serve`, `--fetch`, `--link`, `--accent`, `--setup`, `--uninstall`, or nothing for the widget). Two modes are also selected by filename, for the copies people double-click rather than type. |
| `setup_app.py` | First-run setup and the management view, as a local web page – credentials, Plaid Link, a verifying fetch, install and shortcuts, plus the tabbed Accounts/Appearance settings editor (see Settings without editing JSON). |
| `uninstall.py` | Removes the app: closes a running widget, deletes the shortcuts, the Add/Remove entry and (unless you keep them) your settings, then deletes the installed exes and itself from outside the process. Reached as `Uninstall.exe`, from Settings > Apps, or with `--uninstall` (see Uninstalling). |
| `logo_cache.py` | Caches the derived copies of `logo.png` (tray PNG, shortcut `.ico`) in `cache/`. |
| `build.py` / `DesktopCards.spec` / `make_icon.py` | Produce `dist/DesktopCards.exe` – see Building the executable. |
| `logo.png` | The single source of truth for the app's mark – exe icon, Start-menu shortcut, and tray icon all derive from it. At the repo root rather than in `source/`, because it's the project's own identity rather than part of the dashboard. |
| `mac_port.md` | What a macOS port would actually involve. Planning notes, not a commitment. |
| `appearance.json` | Appearance, window placement, and layout/ordering settings, all in one file (see below). **Gitignored** – it holds your personal monitor position, same reasoning as `config.json`. |
| `appearance.example.json` | A placeholder-position template of `appearance.json`, for the same sharing reason as `config.example.json`. |
| `cache/logos/` | Cached institution logos (one PNG per bank). Gitignored, like `config.json`. |
| `cache/config_snapshot.json`, `cache/appearance_snapshot.json` | `config.json`/`appearance.json` as of the end of the last run – how `fetch_data.py`/`widget.py` tell exactly what changed (see How a refresh actually happens). Gitignored. |
| `config.json` | Your Plaid credentials, access tokens, and the settings below. **Gitignored** – never committed. |
| `config.example.json` | A credential-free template of `config.json`, meant for sharing this tool with someone else: they copy it to `config.json` and fill in their own values rather than yours. |

## Configuration (`config.json`)

| Key | Meaning |
|---|---|
| `client_id`, `secret`, `environment` | Your Plaid API credentials and environment (`sandbox` or `production`). |
| `items` | The linked banks' access tokens, populated automatically by `plaid_link_setup.py`. Each one also carries `use_custom_logo`/`logo_url` – see Logos below. |
| `port` | Local port `serve.py` runs on. |
| `fetch_interval_minutes` | How often to re-pull from Plaid. Read at launch by the widget's own timer, so a change applies on the next Restart – there's no schedule registered with Windows to re-apply. |

## Appearance, window, and layout (`appearance.json`)

Kept separate from `config.json` – nothing here is a credential, but it is
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
| `shadow.offset` | How far the shadow is thrown down and to the right, in pixels. There's no blur – it's a solid block, so this is the whole of its size. |
| `shadow.border` | Thickness of the outline drawn around each card in the same color, in pixels. `0` gives the shadow without the outline. |
| `shadow.color` | Color of both the shadow and that border. Leave it out and it follows the theme – near-black on light, a darker near-black on dark. Setting it pins one color across both themes. |
| `shadow_ui.enabled` | Turns on the matching hard shadow for the **settings page's own** controls – its tabs, boxes, tickboxes, rows and buttons, not the widget's cards. `false` (or every distance `0`) leaves the page flat. There is no form field for this one: see Shadows on the settings page below. |
| `shadow_ui.offset` | How far those shadows are thrown, in pixels. Either a block of per-group distances – `tabs`, `inputs`, `checkboxes`, `rows`, `buttons` – or a single number for all of them. Clamped to `0`–`10`. Unlike the cards there's no border term: these controls have their own. |
| `shadow_ui.color` | Color of the page's shadows. Leave it out (or `null`) and each theme picks its own – a near-black on dark, a slate on light, since near-black on white reads as a hole rather than a shadow. |
| `dark_theme` | `true` swaps the whole palette for the dark one, and is the shipped default. See Dark theme below. |
| `translucent_background` | `true` lets the desktop wallpaper show through the widget. See Translucency below. |
| `opacity` | How opaque the widget is, `0`–`1`, when `translucent_background` is on. Not applied uniformly – the page backdrop takes this value in full and the cards, bars and text give up progressively less of theirs, so text stays readable. `1` switches the whole effect off (see Translucency), and anything below `0.15` is clamped to it. |
| `window.x` / `.y` | Top-left corner of the widget, in screen pixels – pick a spot on whichever monitor you want it on. |
| `window.survive_show_desktop` | Keeps the widget visible through Win+D ("show desktop"). |
| `layout.depository.title` / `layout.credit.title` | The section header shown above each group of cards. |
| `layout.depository.order` / `layout.credit.order` | Which cards appear (and in what order) within that section – a list of `"Bank Name,last4"` strings (e.g. `"Chase,0000"`), matched against what's actually on the dashboard. Accounts not listed just get appended at the end. Maintained for you by the Layout tab. |
| `layout.depository.hidden` / `layout.credit.hidden` | Cards to leave off the widget entirely, same key format. The account stays linked and keeps its place in `order`; it just isn't drawn. Set by the tickboxes in the Modify page's Accounts tab. |
| `layout.depository.new_row_digit_threshold` | How many digits a checking/savings balance needs before that card drops onto a row of its own instead of sitting two-up (default `6`, i.e. $100,000+). See Oversized balances below. |
| `layout.depository.new_row_name_length_threshold` | How many characters an institution name can have before that card drops onto a row of its own instead of sitting two-up (default `12`). See Oversized balances below. |

Width and the max-height safety ceiling aren't configuration – they're fixed
constants in `widget.py` (`WINDOW_WIDTH`, `MAX_HEIGHT`). There's no
"natural" width to derive (text just reflows to whatever width it's given),
and the actual height always comes from the page measuring its own
rendered content, not from a tuned number. `extra_spacing`/`spacing` above
is what controls breathing room. The two things that *do* move the width
are an oversized balance needing more room and `shadow` needing somewhere
to paint – both described below.

## Oversized balances

Checking/savings cards sit two per row, in a box sized for balances up to
$9,999.9. Bigger numbers are handled the same way a long word is handled in
a line of text – slightly too long and it stays on the line, far too long
and it drops to its own line:

- **5 digits** ($10,000–$99,999.9): that card's box widens in place and
  keeps its row. Its row partner stays the width it already was, and the
  window (and therefore the credit cards) grows by the same amount, so the
  credit card and a full debit row stay the same width as each other.
- **`new_row_digit_threshold` digits or more** (6+ by default, i.e.
  $100,000+): that card drops onto a row of its own, as wide as a credit
  card. It stays where it was in the order – the cards around it just pair
  up again after it, rather than being reshuffled.

Raising `new_row_digit_threshold` makes cards widen in place for longer
before breaking out; lowering it makes them break out sooner. This applies
to the default ordering and to a hand-written `layout.depository.order`
alike.

A long institution name hits the same wall, but skips the "widen in place"
step: there's no digit-style per-character cost to size a box against free
text, so a name over `new_row_name_length_threshold` characters (default
`12`) goes straight to a row of its own instead. Without this, the name is
clipped with an ellipsis inside a box still sized for a short one – fine
for "Chase", unreadable for a longer institution name. Raising the
threshold lets longer names stay two-up (and get ellipsized); lowering it
sends more names to their own row.

Editing `appearance.json` takes effect on the dashboard's next load for
appearance – `serve.py` reads it fresh on every request, so there's no sync
script to re-run. Window placement is read once at launch, so changes there
need `widget.py` restarted.

## Card shadow

With `shadow.enabled`, each card gets a solid, un-blurred shadow thrown down
and to the right, plus an outline in the same color – the neo-brutalist
treatment from the Uiverse source card this UI started from (see References).
Defaults reproduce that card exactly: a 10px offset, a 3px border, `#323232`.
Smaller values (4px/2px) read as a subtler lift rather than a graphic slab.

**The window grows to fit it.** A shadow is painted outside the card's own
box, so without that the right-hand column of it would simply have been cut
off at the window edge. The widget widens by `offset + border × 2` – the
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
than unshadowed – the one squeeze that can't be given back without widening
the window further.

The gaps between cards grow by the offset too, otherwise each card would be
painted over the shadow of the one above it. The one exception is the
horizontal gap between two side-by-side checking/savings cards, which is
pinned at 8px because `distributeRowWidths` does its arithmetic against it –
so at a large offset the right-hand card covers the far end of its
neighbour's shadow. That reads as the shadow tucking underneath it, which is
what the style does anyway.

### Shadows on the settings page

`shadow_ui` is the same trick turned on the settings page itself: a solid,
un-blurred shadow at a 45° offset, so the page you configure the widget from
casts the same kind of light the widget does. It is a separate key from
`shadow` because it describes a different surface – changing the cards'
shadow has never had anything to do with the page's own chrome.

**It is deliberately file-only.** There is no row for it in the Appearance
tab, and that is the point: a control that restyles the control you are
holding is a toy rather than a setting. Edit `appearance.json` and reload the
page. Nothing is lost by saving from the browser meanwhile – the Appearance
tab preserves keys it has no fields for, and the save endpoint also takes
`shadow_ui` from disk rather than from the request, so a tab left open since
before your edit can't write a stale copy back over it (the same guard
`layout` gets).

**One distance per group of controls**, because they are nothing like the same
size: 3px under a full-width text box is a hairline, and the same 3px under a
17px tickbox is a third of the control. The groups are `tabs`, `inputs`
(text, number, select and the colour wells), `checkboxes`, `rows` (the Layout
tab's draggable cards and the Accounts tab's rows) and `buttons`. A new
control joins a group rather than earning a key of its own.

**What casts, and when:**

| Group | At rest | While you're using it |
|---|---|---|
| Tabs | – | The selected tab casts, with no fade: selecting a tab is a jump between panels, and a shadow easing in behind it reads as the page still settling after you've moved. |
| Boxes and colour wells | Raised | Drops to the page over 0.45s when you click into it. |
| Tickboxes | – | Casts while ticked, and while focused from the keyboard. |
| Rows | Raised | Drops the same way – when a Layout card is picked up to drag, or when an Accounts row is ticked. |
| Buttons | – | Grows from nothing to full in 0.32s on hover, holds, then runs back down when the pointer leaves. Quicker than the rows on purpose: a hover is answering you, and anything you can outrun with the pointer feels like lag. Dropdown menu items opt out – a menu row sits inside something that already casts. |

Three places the geometry had to bend, all for the same reason: a shadow is
painted *outside* the element, onto whatever is next to it.

- **The selected tab's shadow is clipped at the rule it sits on.** That tab's
  bottom edge is deliberately missing so it merges into the panel below, and
  a shadow running past the line would paint the seam back in as a dark bar.
- **Tickboxes reserve a strip** the width of their own offset, and are given a
  stacking position. The label text is both closer and drawn later, which was
  clipping the corner off the shadow.
- **Accounts rows cast from their cells, not the row.** A `<tr>` in a
  separate-border table has no box of its own to cast one, and middle cells
  cast straight down only – sideways, each would land on its own neighbour.

The page is inert at a `0` distance: every rule spends its distance twice and
a `0 0` shadow with no blur paints nothing, so an absent `shadow_ui`, a
disabled one and an all-zero one are the same page. `index()` sends the
distances as inline custom properties on `<html>`, next to the accent it
already sends, and a non-hex colour or an unparseable distance falls back to
the stylesheet's own rather than reaching the attribute.

## Dark theme

`"dark_theme": true` and the whole widget swaps palette. It's one flag, not a
set of colors, because every color the stylesheet paints already came from a
`--surface-*` / `--text-*` variable – so the theme is a single
`:root[data-theme="dark"]` block in `dashboard.html` that redefines those
variables, and no rule below it needs a dark variant. `dashboard.js` only
sets the attribute.

**Dark is the shipped default**, so `data-theme="dark"` is seeded in
`dashboard.html`'s markup alongside `data-shadow` and `data-translucent`. The
stylesheet's own `:root` is still the light palette, and without the seed the
window painted light for the moment before `dashboard.js` read
`appearance.json` and corrected it – a flash on every launch. `dashboard.js`
still owns the attribute after that and sets it to `light` when the file says
so. The setup page does the same thing from the other direction: dark is its
base `:root`, so its fallback with no `appearance.json` matches what a first
run will actually get.

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
  `--surface-0` per theme. (Translucency makes this moot – a transparent
  window has no fill at all. See Translucency.)
- **The shadow color**, which is why `SHADOW_DEFAULTS` in `dashboard.js` has
  no color in it: an inline style written from JS beats the theme block and
  would pin the light value in both themes, so the stylesheet owns that one.

Some deliberate choices in the palette itself:

- **Not black.** The cards sit one step lighter than the page behind them,
  the same figure/ground step that white-on-grey gives in the light theme –
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
  so that they look equally bright. The page behind is untouched – it's the
  card face that gains the brightness, not the whole widget. Light theme
  needs none of this: on white, a borderless card reads as paper, which is
  the point.

Unlike window placement, this takes effect on the dashboard's next load – but
`page_background` above is read at launch, so switching themes is worth a
`widget.py` restart to avoid a light flash on the next one.

## Translucency

`"translucent_background": true` plus an `"opacity"` between `0` and `1` lets
the desktop wallpaper show through the widget. It takes both halves to work,
and they fail in opposite directions if only one is done:

- **The window** has to stop painting a background, or the page's `rgba`
  colors have nothing to blend against but the window's own fill – the
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
  it take a color in any format – these are hex, and `--accent-raw` is
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
arbitrary wallpaper is the one thing translucency mustn't cost – but they do
give up some, so they belong to the same surface as everything else instead of
floating above it.

Two tier assignments are deliberate and non-obvious: the **shadow** follows the
card rather than the page, because it's an edge belonging to the card that cast
it and at page alpha it washed out while the card above it still read as solid;
and the **bar's track** is on the accent tier rather than the surface one, so
the whole progress bar fades as a single object instead of its background
dropping out from under its fill.

Three things can't ride on a CSS variable:

- **The warning colors**, same as in the dark theme – computed per card and
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
given up, capped at `HEADING_DARKEN_MAX = 0.7` – a pure black heading above
pale card text looks like a different widget's label pasted on top. Both
themes get this: in the dark theme the page is barely there at low opacity
anyway, so the heading's backdrop is the wallpaper either way.

### Off means off

`translucent_background: false` **or** `opacity: 1` switches the whole thing
off, in both files, and they have to agree – disagreeing would blend the page
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
desktop layer and takes no mouse input – an opacity of `0` would render it
invisible with nothing left to click to undo it. A non-numeric value falls
back to fully opaque rather than guessing.

### A hazard worth knowing about

A transparent window on WorkerW has no background brush to erase with, and
when it shrinks, the thing that would have to repaint underneath is the
desktop, which never finds out it should – so the previous frame's pixels can
be left stranded outside the window's new bounds. `erase_vacated_area` in
`widget.py` handles that: `resize_window` captures the window rect *before*
resizing, then `RedrawWindow`s that region on the parent with
`RDW_ERASE | RDW_UPDATENOW`. It only runs when translucent, since an opaque
window gets this from DWM for free, and it no-ops when `survive_show_desktop`
is off because an unparented window doesn't have the problem.

Honest caveat: this was written for a ghost-text symptom that turned out to
have a completely different cause (stray text outside an HTML comment), so
while the mechanism is real, the code is **unverified against an actual
occurrence**. `SetWindowRgn` is worth noting here too – clipping the window
stops *it* painting outside its bounds, which is what would let such pixels
end up orphaned rather than overwritten.

Like the theme, the page side applies on the next dashboard load, but the
window side is set at `create_window` time – so toggling translucency needs a
`widget.py` restart (the tray's **Restart**), not just a Reload.

## Warning colors

All of it is computed in `dashboard.js` from the balances themselves –
nothing about severity is stored in `data.json`, so there's no stale
percentage to go out of sync with a freshly-fetched balance.

**Credit utilization** (the `%` to the right of the balance, and the usage
bar) shares one ramp:

| Utilization | Color |
|---|---|
| Below 27% | Grey – nothing to look at |
| 27–35% | Yellow |
| 35% → 100% | Yellow ramping to deep red |
| Above 100% | Stays deep red |

The ramp deliberately front-loads its move toward red (`URGENCY_EXP`), so
the middle of the range reads as a problem rather than a comfortable
amber. Utilization can exceed 100%, and the color simply holds at its
reddest there – there's nowhere redder to escalate to.

The bar and the `%` run the same ramp, so they can't disagree about
severity, but off two different yellows (`barYellow` vs `yellow`): a solid
block of color and 13px text need different intensities of the same hue to
look right. All four colors here come in a light and a dark version – same
hues, different lightness (see Dark theme) – and all four are `[h, s, l]`
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
-$25 – the same yellow as a credit card's first warning band, since
"watch this" means the same thing on either card.

## Logos

For each linked item, `fetch_data.py` tries, in order: your own URL
(`logo_url`, only if `use_custom_logo` is `true` on that item in
`config.json`) → Plaid's own copy of the institution's logo → the bank's
public favicon via Google's favicon service off its own website. The
default (non-custom) lookup only actually runs once per institution,
though – once something's cached in `cache/logos/`, every later run just
reuses it, unless `institution_id`/`use_custom_logo`/`logo_url` actually
changed for that item since the last run (a new link counts too). If
every option fails on a run that does need a fresh fetch (bad URL, no
connection), it keeps whatever's already cached instead of blanking the
logo out over a transient failure. If nothing has ever been cached, the
dashboard falls back to a plain initial-letter avatar in your accent
color.

## Who can reach these servers

Three HTTP servers run here: the dashboard (5000), the setup/management page
(8009) and the standalone Link flow (8008). Between them they serve your
balances and, in Link's case, exchange a Plaid token. Worth being precise
about who can talk to them.

### Not the network

Every one binds **loopback only** – `serve.py` and the two Flask apps all pass
the `LOOPBACK` address explicitly. A socket bound to loopback is refused by the
kernel for any non-local source, so nothing on your Wi-Fi and nothing behind
your public IP can reach them. That holds with no firewall rule, and it would
hold even if you deliberately forwarded a port at the router. Verified with
`Get-NetTCPConnection`: the listening addresses are `127.0.0.1`, never
`0.0.0.0`.

An explicit bind in all three matters more than it looks. Flask's default
*happens* to be loopback, and `plaid_link_setup.py` used to rely on that – "it
defaults to safe" is not something worth leaving implicit in the one file whose
form carries a Plaid secret and an access token.

### The browser, though, is already here

Loopback does nothing about the browser running on this machine, and two
attacks follow. Both arrive from a page you merely *visited*, with nothing
executing locally:

| | What it does |
|---|---|
| **DNS rebinding** | `attacker.com` is published with a one-second TTL, then re-resolved to `127.0.0.1`. The attacker's own JavaScript fetches `http://attacker.com:5000/data.json`, which the browser now treats as same-origin, so it can **read** the reply. Loopback is no defence: the connection genuinely is local. |
| **CSRF** | A cross-origin page POSTs to `/refresh`, `/restart`, `/quit` or `/api/quit`. It can't read the response and doesn't need to in order to force a Plaid pull or kill a server. |

The rebinding one was live and measured, not theoretical:

```
GET http://127.0.0.1:5000/data.json   with  Host: evil.example.com
→ HTTP 200, 1539 bytes of balance data
```

`net_guard.py` closes both with two rules:

- **The Host must name this machine.** That's what gives rebinding away: the
  connection is local but the header still says `attacker.com`. Loopback
  addresses are *parsed* rather than matched against a list, so `127.0.0.1`,
  `127.0.0.2`, `::1` and `0:0:0:0:0:0:0:1` are all recognised for what they
  are, alongside the name `localhost`. Pinning it to one literal would refuse
  a request from this very machine the moment something used another valid way
  of saying the same thing. The port compared against is the one the server
  actually bound, never a hardcoded number.
- **Anything the browser labels cross-site is refused**, via `Sec-Fetch-Site`,
  with `Origin` as the fallback for browsers too old to send it.

One deliberate exemption: a cross-site **GET navigation** is allowed. That's
how an OAuth hand-back from a bank returns, and how clicking a localhost link
in another app gets here; the response becomes a page at our own origin rather
than data handed to someone else's script. Cross-site POSTs and every
cross-site fetch, XHR and subresource are still refused.

A request with neither an `Origin` nor a `Sec-Fetch-Site` header is allowed
through. That's an ordinary non-browser client – this app's own `urllib` calls
among them.

### What this deliberately does not defend

**Another process running as you.** It could read `config.json` off the disk
instead, so a check at the HTTP layer would be theatre. The attacks above are
different in kind: they need nothing more than a tab being open. If you want
the stronger property anyway, a per-run token in the URL is the shape it would
take – it was considered and rejected as cost without a matching threat.

The Plaid secret and access tokens are never served regardless: `config.json`
isn't on `serve.py`'s allowlist (404), and `/api/state` omits the credentials
deliberately.

### Why `127.0.0.1` and not `localhost`

Every internal call dials the literal, from `paths.LOOPBACK`. The name is the
trap: `getaddrinfo("localhost")` returns `::1` **before** `127.0.0.1` on
Windows, and since these servers are IPv4-only, a client using the name pays a
dead IPv6 connection attempt first. Measured on a machine with IPv6 disabled:

| Request | Time |
|---|---|
| `GET http://localhost:5000/appearance.json` | 2.08s |
| `GET http://127.0.0.1:5000/appearance.json` | 0.02s |

A hundredfold, and not merely slow. `widget_is_running()` allows **one
second**, and it was spending 1.01s – the whole budget – before falling back to
IPv4. A fraction slower and it reports no widget while one is plainly running,
at which point setup's Launch button tries to start a second instead of
restarting the first, and the single-instance lock kills it silently. The
tray's Reload was also burning ~2s before it so much as spoke to Plaid.

`net_guard` accepts either spelling, so this is purely about who we dial, never
about what we accept.

## How a refresh actually happens

Only `fetch_data.py` calls Plaid: on the widget's interval timer (every
`fetch_interval_minutes`), or on demand via the tray's "Reload". It makes 2
Plaid calls per linked bank (balance + liabilities), fetched for all your
banks at once rather than one after another, plus one extra (one-time)
logo lookup the first time a given institution has never been cached
before – after that, logos are never re-fetched, just reused. The widget's
own page just re-reads `data.json` every 5 minutes and whenever it loads –
no Plaid traffic from the UI itself, ever. The tray's "Reload" calls
`fetch_data.main()` directly inside `serve.py`'s already-running process
rather than launching a brand new Python process for it, which used to
cost more than the actual Plaid calls did.

Balances always get refreshed on "Reload" – that part's never skipped.
What *is* skipped when nothing's actually different: `cache/config_snapshot.json`
and `cache/appearance_snapshot.json` hold each file's state as of the last run,
so `fetch_data.py` only redoes a logo lookup for an item whose
`institution_id`/`use_custom_logo`/`logo_url` actually changed (or that's
brand new), and `widget.py` only re-clips/repositions the native window
when `appearance.json` actually differs from last time – both are otherwise
silent no-ops instead of redundant work on every single click.

### The interval timer

`fetch_loop` in `widget.py` re-pulls from Plaid every
`fetch_interval_minutes`, for the life of the widget. This used to be a
Windows Task Scheduler job registered by a `setup_schedule.py` script. The
job worked, but it was a separate artifact registered with the OS, pointing
at a script path, outliving any particular install and firing whether or not
the widget was running – writing a `data.json` nobody was reading.

The trade is that **balances only refresh while the widget runs.** What makes
that acceptable is the catch-up: if `data.json` is already older than one
interval, because the machine was off or the widget was closed, the timer
fetches immediately on launch rather than waiting out a full interval while
showing stale numbers.

A failed fetch (no network, a Plaid rate limit, an item needing re-auth) is
logged and skipped, never fatal – the loop has to survive to try again.

### Restart and the port

The tray's **Restart** is a full process relaunch, for the settings that are
only read once at startup (`survive_show_desktop`, `translucent_background`,
`page_background`, `fetch_interval_minutes`).

The server is a daemon thread inside the widget process, so there's nothing
to shut down first and nothing that can outlive its widget – an earlier
version spawned `serve.py` as a subprocess, which could be orphaned and sit
on the port through every later restart.

The replacement process still has to get the port back, though.
`serve.py` sets `allow_reuse_address = False` deliberately, so a second
instance can't quietly bind on top of a live one – which also means sockets
from the previous instance sitting in `TIME_WAIT` make the bind fail
outright, and Restart lands well inside that window. This used to be papered
over with a flat `time.sleep(1)`, which made the failure silent and baffling:
the server died, the sleep expired anyway, and the window came up pointed at
a port with nothing behind it, so the widget just disappeared with no error
anywhere (it runs under `pythonw`, so there's no console to print to).

`serve.build_server()` retries the bind instead, waiting half a second
between attempts – `TIME_WAIT` is a timer, not a contended lock, so waiting
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
python source/build.py
```

Produces two files in `dist/` at the repo root, both about 31.7 MB. A third
name exists but isn't built here: the install step copies the same binary to
`Uninstall.exe` in `%LOCALAPPDATA%\DesktopCards\` (see Uninstalling). The build
runs with the repo root as its working directory so `dist/` and `build/` land
there rather than inside `source/`, and `DesktopCards.spec` therefore resolves
every one of its inputs from `SPECPATH` (its own folder) instead of the
working directory – `dashboard.html` and `dashboard.js` from `source/`,
`logo.png` and the generated `.ico` from the root, and the two
`*.example.json` from `config/`. They all get flattened into one folder inside
the bundle, which is why `paths.py` has to say separately where each of them
comes from when running from the repo.

| File | What it's for |
|---|---|
| `DesktopCards.exe` | The widget. |
| `DesktopCards-Setup.exe` | First-run setup. |
| `Uninstall.exe` | The uninstaller. Not in `dist/` – written at install time, beside the installed app. |

They are **the same binary under three names.** `desktopcards.py` checks its
own filename: a name containing "uninstall" uninstalls, one containing
"setup" or "install" runs setup, anything else runs the widget. So there's
one build, one thing to download, and a new user can't click the wrong file
first – the launcher doesn't exist until setup has made it. An explicit flag
(`--serve`, `--fetch`, `--link`, `--accent`, `--setup`, `--uninstall`) always
wins over the name, so no copy of it is ever a dead end.

The order of those two filename checks is load-bearing, and the one trap in
the scheme: `Uninstall.exe` contains the string `install`, so uninstall is
tested first. Reversed, double-clicking the uninstaller would reinstall the
app.

Everything is bundled: Python, plaid-python, Pillow, pystray, Flask, and
pywebview with its .NET interop. The only thing not carried is the WebView2
runtime, which is a built-in Edge component on Windows 11.

### One entry point

Three places used to spawn Python by name – `pythonw.exe serve.py`,
`pythonw.exe widget.py`, and a Task Scheduler command line. All three break
in a frozen build for the same reason: `sys.executable` *is* the app, it
takes no script argument, and there are no `.py` files on disk to point at.
`desktopcards.py` replaces them with subcommands, so `sys.executable` plus a
flag covers every case, from source and frozen alike.

Two of those modes are also selected by filename, for the builds someone
double-clicks rather than types: a name containing `setup` or `install` runs
setup, and one containing `uninstall` runs the uninstaller. An explicit flag
always wins, so no copy of the exe is ever a dead end.

### No console, so output goes to a file

The widget is a desktop ornament and can't have a console window behind it.
But PyInstaller sets `sys.stdout` and `sys.stderr` to `None` in a windowed
build rather than to something inert, so every `print()` in the codebase
would raise `AttributeError` on `None.write` – harmless progress messages
becoming crashes, in the build where they're hardest to see.

`redirect_output_to_log()` points both at
`%LOCALAPPDATA%\DesktopCards\desktopcards.log`. That also buys the one thing
this app otherwise can't produce: a record of what went wrong. pywebview's
known-worst failure mode is coming up as a blank window with no traceback
anywhere and no tray icon either – and the tray is the only control surface,
since the desktop layer the widget is pinned to receives no mouse input. The
log is the difference between diagnosing that and guessing.

It also means **setup can't be a terminal prompt.** It runs in the browser,
which is where the Plaid Link flow already lives.

### The icon

`logo.png` is the single source of truth for the mark. `make_icon.py`
generates `DesktopCards.ico` from it on every build – seven embedded sizes
from 16px to 256px, because Windows picks whichever fits the context it's
drawing in rather than scaling one badly.

The tray icon loads `logo.png` directly at runtime and downsizes it to 64px,
since the tray slot is 16–32px and the source is 1024px square. It falls
back to a drawn accent-coloured circle if the file can't be read – not dead
code, because a spec file that forgot to bundle `logo.png` would otherwise
leave the app with no tray icon, and therefore no way to control it.

**Replacing the logo.** From source, edit `logo.png` and it takes effect
immediately. In the packaged build the bundled copy is sealed inside the exe,
so dropping a `logo.png` into `%LOCALAPPDATA%\DesktopCards\` overrides it –
that path wins whenever it exists, and the tray resolves it per call rather
than at import. The log says which one it used.

The **exe's own icon is different** and can't work that way: Windows reads it
from a resource compiled into the executable, so changing it requires a
rebuild. `python source/build.py` regenerates the `.ico` from `logo.png` first, so a
rebuild is all it takes.

### Hidden imports

`desktopcards.py` imports each mode's dependencies inside its own branch, so
asking for `--fetch` doesn't load pywebview and the whole .NET interop layer.
That's good for startup cost and invisible to PyInstaller's static analysis –
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
  `build.py` checks first and says so plainly. It checks both names – they're
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
  than letting a bare traceback name a DLL and offer no advice – this is the
  single most likely thing to go wrong in a frozen build, so it's worth
  handling by name.

## Setup, and adding accounts later

Run `DesktopCards-Setup.exe` (or `--setup` from source). It opens a page in
your browser and walks through five steps:

1. **Plaid credentials.** Validated against the real API *before* anything is
   written – creating a link token is the cheapest call that proves a
   client_id/secret pair works, and wrong credentials are the single most
   likely setup mistake. Failing here, with the fields still on screen,
   beats saving bad credentials and looking like a broken app later.
2. **Link a bank.** Plaid's own hosted widget, so your bank password never
   passes through DesktopCards. Repeatable – each run adds another bank.
3. **Check it works.** One real fetch. This is the step worth insisting on:
   everything before it can succeed and still leave you looking at an empty
   widget, and this is where that becomes a message instead of a mystery.
4. **Install.** Copies the exe to `%LOCALAPPDATA%\DesktopCards\` and writes
   a Start-menu shortcut, optionally a Startup one.
5. **Launch.**

Run it again and it skips to a **management view** instead, which is two
tabs (see Settings without editing JSON below).

The page is served on localhost only, and `/api/state` deliberately carries
no `client_id` or `secret` – the page never needs them back, and not sending
them means they can't end up in a browser cache or a screenshot.

**Close setup** stops the local server. It used to look broken: the reply to
`/api/quit` was written by a process that then called `os._exit` immediately,
cutting the socket mid-write, so the page's `fetch` rejected with a network
error even though the server had stopped exactly as asked. The exit now waits
long enough for the response to land. The tab itself can't be closed
programmatically – browsers only allow that for a window a script opened, and
this one came from `webbrowser.open` – so the page says to close it instead of
pretending.

### The server goes when the page does

Setup is a server that exists only to back one browser tab, and a tab can be
closed, reloaded, navigated away from, or killed along with the whole browser.
None of that reaches the process. Left alone it sat there forever, and since
the tray's **Modify** started a fresh one on every click, "forever" meant one
orphaned process per click.

Three signals, because no one of them covers every way a tab can go:

| Signal | What it is |
|---|---|
| Heartbeat | the page `POST`s `/api/alive` every 25s while it's open |
| Close | `pagehide` fires `navigator.sendBeacon('/api/closing')` on the way out |
| Idle | nothing has contacted the server at all for five minutes |

The close signal is the interesting one, because **a reload fires exactly the
same event as a close** and the two are indistinguishable from the server's
side. So the beacon doesn't exit, it *arms* a four-second timer, and any
request arriving before it expires calls it off. A reloaded page's first
request lands well inside that window on localhost; a closed tab never sends
one. Four seconds is long enough for the round trip and too short to notice.

`pagehide` rather than `unload` because it's the event browsers actually
guarantee for a closing tab and it coexists with the back/forward cache, and
`sendBeacon` rather than `fetch` because a normal request is cancelled when
the page goes away, which is precisely the moment this one has to survive. A
page restored from the back/forward cache pings `/api/alive` on `pageshow`, so
it disarms itself.

Two cases that would otherwise be misread:

- **A Plaid OAuth login** navigates the page to the bank and back later. That
  fires `pagehide` and is emphatically not a close, so the beacon is
  suppressed while Plaid's widget is open.
- **A backgrounded tab.** Browsers throttle timers in hidden tabs hard –
  Chrome can drop one to a single wake per minute – so the idle timeout is
  minutes rather than seconds. A tab you merely switched away from must not
  look the same as one that's gone. The cost is that leaving the page open
  through a long sleep does end the server; reopen it from the tray.

The widget is never affected by any of this: setup runs as its own process
specifically so it can't take the widget down, and the shutdown logic lives
only in `setup_app.py`.

**The tray also stops creating duplicates.** Modify now checks whether a setup
server is already answering on its port and, if so, just points the browser at
it. That port lives in `paths.py` rather than `setup_app.py`, so `widget.py`
can ask without importing Flask into the widget process to find out. The old
behaviour was worse than one extra process: Werkzeug allows address reuse, so a
second server *stacked* on the same port rather than failing, and requests were
then split between them at random – a reload could land on an older copy of the
page, which is exactly as confusing as it sounds.

## Settings without editing JSON

The management view is three tabs – **Accounts**, **Layout**, **Appearance** –
because `appearance.json` is not a reasonable interface to hand someone:
everything in it is a real preference, and asking people to find a file and
match JSON syntax to change a corner radius means nobody changes anything.

The page wears the widget's own theme, light or dark, off the same
`dark_theme` flag and applied the same way (one attribute on `<html>`, every
color from a variable). Configuring a dark widget through a bright white page
was a jarring enough mismatch to be worth the second palette. It takes the
accent too, since that's the color the widget is already using.

**The theme is rendered into the markup, not applied by the script.** Flask
writes `data-theme` onto the `<html>` tag (and the accent as an inline custom
property, which outranks any selector) before the page is ever sent. Anything
the script does happens after the first paint, so applying it there meant the
page came up in the light palette and flipped a moment later: a flash on every
open. The attribute is the same one `applyPageTheme` sets, so nothing
downstream needs to know which of the two put it there. The accent is matched
against an anchored hex pattern first, because it goes straight into an HTML
attribute and `appearance.json` is hand-edited.

**And it only moves on save.** An earlier version previewed the theme live on
every field change, which sounded helpful and in practice flashed the entire
page on each click of a checkbox. A theme that changes before it's saved is
also claiming something that isn't true yet, so the save handler is now the
one place the page's own palette changes.

**Accounts** carries the linked banks (add, remove, redo shortcuts, launch or
restart the widget, start-with-Windows), and under it the individual accounts
the last fetch returned, grouped into the same Checking & Savings / Credit
Cards sections the widget draws. "Chase" on its own never said whether that
bank brought one checking account or three cards; this does.

**No balances anywhere in this view, deliberately.** It opens in an ordinary
browser tab, and a tab showing real account balances reads as a banking site –
which this isn't and shouldn't resemble. Bank name on the left, what the
account is on the right. `/api/state` doesn't merely omit them from the
markup, it never sends them; the one number it does send is a digit *count*,
which the Layout tab needs to say whether a balance is wide enough to break a
card onto its own row, and which is never rendered as money.

Each account has a tickbox for whether the widget draws it at all. Unticking
writes the key into `layout.<section>.hidden` and the card disappears on the
next refresh; the account stays linked and keeps its place in the order, so
ticking it back puts it where it was. Hidden accounts are filtered out before
the dashboard measures anything, so a hidden card can't widen the window to
make room for something that never gets drawn. If you hide everything, the
widget says so rather than claiming no accounts are linked – the two look
identical on screen and have completely different fixes.

One asymmetry worth knowing: `order` is pruned to the accounts that actually
exist, and `hidden` is not. A bank that errors on one fetch drops out of
`data.json`, and pruning `hidden` would quietly switch an account back on –
the worse of the two failures, so stale keys are left to sit there.

Its **Advanced** block is collapsed by default and holds the things that
aren't day-to-day: the Plaid environment, client ID and secret. Saving there
**unlinks every bank**, and the page warns before it does. That isn't
caution for its own sake – an access token is only valid for the client ID
and environment it was created under, so banks linked under the old ones
would error on every fetch rather than carry over. Both credential fields
start blank and blank means "keep the saved one", so switching environment
alone doesn't mean re-copying a secret out of the Plaid dashboard. They start
blank rather than pre-filled for the same reason `/api/state` omits them.

**Layout** is the card order, by drag. Each section is its own list, and
that's a constraint rather than an omission: which section a card lands in
comes from the Plaid account type, and the widget draws debit cards two-up and
credit cards full width with a usage bar, so a card can't meaningfully move
between them. Native HTML5 drag events, no library.

Every row carries a badge saying what the widget will do with it – `two-up`,
`own row: long name`, `own row: big balance`, `half row` for one that has no
partner left to pair with, `hidden` for one that's switched off. That badge is
computed by the same pairing walk `dashboard.js` does, over only the cards
that will actually be drawn, so it tells the truth rather than approximating
it. It's the answer to the question the row-breaking rules kept raising: *why
is that card on its own line?*

The section heading and the two row rules (`new_row_digit_threshold`,
`new_row_name_length_threshold`) sit with the list they change the shape of,
and changing one re-badges the rows immediately instead of waiting for a
reload. They used to be in the Appearance tab and are not duplicated there any
more – two controls writing one value means one of them is always showing
something stale.

Drops save immediately, matching the tickboxes, with **Undo last move** for
the one that went wrong. Two guards sit behind that:

- A posted order must be a permutation of what's on file. Anything else is a
  stale page naming accounts that have since gone, and it's refused with a
  409 rather than written.
- The Appearance tab holds a whole copy of `appearance.json`, so saving it
  could carry a stale `layout` back over a reorder. Two things prevent it: a
  layout change drops that cached copy so the tab re-reads, and the appearance
  save endpoint takes `layout` from disk rather than from the request. The
  Appearance tab has no layout fields, so it has no business writing them.

**Appearance** is every remaining key in `appearance.json`, in the order the
file has them, generated from one field spec rather than twenty hand-written
form rows.
Fields that only matter while another is on (per-side `spacing` under
`custom_spacing`, the `shadow` sizes under `shadow.enabled`) grey out rather
than disappear, since the value is still in the file and hiding it would read
as the setting having gone away. Anything `widget.py` reads once at launch
(`window.x`/`y`, `survive_show_desktop`, `translucent_background`) is marked
**restart**, which is also what the tab's **Restart Widget** button is for.

Three details worth knowing:

- **Saving preserves keys the editor doesn't know about.** The form collects
  onto the object it loaded rather than building a fresh one, so a hand-added
  key, or one from a later version, survives a save instead of being dropped.
- **Restore to Default loads the defaults into the form without saving.** One
  wrong click shouldn't erase a tuned window position and palette with no way
  back, and Save is right there. The defaults come from
  `appearance.example.json` – the same file a fresh install is seeded from,
  so there's one definition of "default" rather than a second copy in Python.
- **A blank shadow color deletes the key** instead of writing `""`, because
  absent is what means "follow the theme" (see Card shadow).

### The layout order now names your actual banks

`layout.depository.order` shipped with placeholder keys (`"Chase,0000"`) that
matched nothing on a real dashboard, so anyone opening the file to reorder
their cards found a list naming none of their banks and no way to learn the
correct strings. Opening the management view now rewrites those lists with the
real `"Bank Name,last4"` keys, read out of `data.json` and written back to
`appearance.json`, and the Appearance tab prints them under **Card order**.

Keys already in the list keep their position, so a hand-sorted order survives;
new accounts are appended and departed ones dropped. `order_key` in
`setup_app.py` has to match `orderKey` in `dashboard.js` exactly, or a
hand-written order silently matches nothing and every card falls through to
the appended tail. `balance_digits` has to agree with `intDigits` for the same
reason: it's what the Layout tab's row badges are computed from, and the
widget is the thing that actually does the breaking.

The file stays hand-editable – nothing here replaces that – but the Layout tab
is now the easier route.

### Why it's a web page

The packaged build is windowed, so there's no console to prompt in. And the
Plaid Link step has to happen in a browser regardless, because that's where
Plaid hosts it. Given one step is already a web page, all of them may as
well be.

### Adding a bank from the tray

The widget's tray menu has **Modify**, which opens that same management view.
It's named for what it now does: when it only added banks it was "Modify
Accounts", but the same page is where appearance lives too. That's the
everyday route – nobody should have to find a file in Downloads to add a
card, or a JSON file to change a color. It spawns a separate process on purpose, so
nothing about the setup page can take the widget down with it, and **the
widget keeps running throughout**: Link is on 8008/8009 while the dashboard
is on 5000, so there's no conflict. New banks appear on the next refresh.

Removing a bank drops its access token from `config.json` but does **not**
remove the Item at Plaid. That's deliberate – it's irreversible, and someone
re-adding the same bank a minute later shouldn't be punished for it.

### Launch when it's already running

Setup's **Launch widget** button becomes **Restart widget** when a widget is
already up, and does exactly what the tray's Restart does. Starting a second
copy isn't an option – the single-instance lock stops it, correctly – but
doing nothing is the wrong answer to a button press, and a restart is usually
what's wanted anyway, since the reason to press it after changing something
is to pick that change up.

It works by asking the running widget to relaunch itself: `POST /restart` on
the dashboard port, handled by the server thread living inside the widget
process. `widget.py` registers the hook once the tray and window exist; a
standalone `--serve` leaves it unset and correctly answers *"no widget in
this process"* rather than pretending. The response is sent **before** the
restart begins, since the restart tears down that very server.

### Asking it to quit

`POST /quit` is the same shape as `/restart` and exists for one caller: the
uninstaller, which has to get the widget out of the way before it can delete
the exe that widget is running from. It runs `quit_now()` – the tray's own
Quit – so the tray stops, the window closes, the single-instance lock is
released and the 2.5s `force_exit_soon` backstop still applies.

Asking rather than killing is the point. `taskkill` would leave the lock
held by a dead process, and the next install would then come up believing a
widget was already running. The uninstaller does keep a kill as a backstop,
but only for something still answering on the port after being asked.

### Spawning a replacement in a frozen build

Three separate bugs lived here, all invisible from source and all fixed:

- **Inherited unpack directory.** PyInstaller's bootloader tells child
  processes where it unpacked itself via `_PYI_*` environment variables.
  That's right for a child belonging to the same run, and wrong for a
  replacement meant to outlive us: the child skips extraction, uses our temp
  directory, and then we exit and the bootloader deletes it underneath.
  The symptom was `Cannot find Microsoft.Web.WebView2.Core.dll` – pointing
  nowhere near the cause. `clean_child_env()` strips them.
- **The working directory locked the temp folder.** `spawn_self` passed
  `cwd=PROJECT_DIR`, which frozen is `ASSET_DIR` – *this process's own
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
Quit that leaves the process alive is worse than a blunt exit – the
single-instance lock would then block the next launch.

### Only one widget at a time

The app is launched from a Start-menu shortcut, a Startup shortcut, and
setup's Launch button, so a second instance is an ordinary event rather than
a mistake. `claim_single_instance` takes a named mutex before anything
touches the port; a second instance logs *"Already running"* and exits 0.

Without it the second process got as far as binding port 5000, failed, and
died with an `OSError` traceback in a popup – the loud failure is correct in
itself, but being shown it for double-clicking a shortcut is not.

A named mutex rather than a lock file because the kernel releases it however
the process dies, so a crash can't leave a stale lock that blocks every
future launch. The tray's Restart releases it explicitly *before* spawning
the replacement, since the old process is still alive at the moment the new
one checks – otherwise Restart would quietly kill the widget instead of
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
every launch, so its mtime is always newer than anything cached – an mtime
check would rebuild both derivatives every single run, which is exactly what
the cache exists to avoid. Hashing one PNG costs far less than re-encoding
seven `.ico` sizes.

The widget rebuilds the `.ico` on startup if it's missing, so the shortcut
icons self-heal, and replacing `logo.png` updates them on the next launch.

## Why a few of the design choices are what they are

- **WebView2 instead of a Chrome window**: Chrome's `--app` mode windows
  always paint a small title bar themselves, independent of the OS window
  style – there's no supported way to remove it. Windows' built-in WebView2
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
  doesn't receive normal mouse input – the same reason Rainmeter/Wallpaper
  Engine skins need a system-wide mouse hook just to be clickable at all.
  A system tray icon is a normal window regardless of where the main
  widget is parented, so that's where manual Reload/Restart/Quit live
  instead.
- **Rounded corners via a window region, not just CSS**: `pywebview`'s
  `transparent` option only makes WebView2's own background transparent
  against the window's paint – it doesn't change the window's actual
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
