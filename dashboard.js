const POLL_MS = 5 * 60 * 1000; // re-read data.json every 5 min; no network calls of our own

// There's no in-page reload control: the window is reparented onto the
// desktop's WorkerW layer to survive Win+D, and that layer doesn't receive
// normal mouse input (the same reason Rainmeter/Wallpaper Engine skins
// need a system-wide hook to be clickable at all). Manual refresh instead
// lives in the system tray icon (widget.py), which calls POST /refresh
// and then tells this page to reload.

function fmtMoney(n, decimals = 1) {
  if (n === null || n === undefined) return '–';
  return n.toLocaleString('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

function fmtDate(iso) {
  if (!iso) return null;
  const d = new Date(iso + 'T00:00:00');
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function fmtMask(mask) {
  return mask ? `&bull;&bull;${mask}` : '';
}

// One warning palette for both card types: a credit card at 27-35%
// utilization and a debit card running low read as the same degree of
// "watch this", so they share a yellow rather than being two near-misses.
// Kept as [h, s, l] components so the ramp below can interpolate between
// them -- hardcoding its endpoints separately meant a change to the
// yellow left a visible step at the 35% boundary.
//
// Two yellows, same hue, different intensity: small 13px text needs the
// muted one to stay easy on the eye, while the bar is a solid block of
// color where that same muting just reads as dull, so it keeps the fully
// saturated original.
// Two sets of the same four colors, because these are the one part of the
// palette CSS variables can't carry: they're computed per-card in JS and
// written as inline styles. Same hues in both, only lifted in lightness
// and eased off in saturation for the dark theme -- a 38%-lightness deep
// red that reads as urgent against white is nearly invisible on a dark
// card, and a fully saturated yellow on dark glows. Keeping the hues
// identical is what makes the two themes feel like the same widget.
// All four are [h, s, l] arrays, including grey -- it used to be a finished
// string, but since every one of them now has to come back out of hsl()
// carrying the current alpha, having one that skipped that step meant the
// one color below 27% stayed opaque while the rest went translucent.
const WARN_PALETTES = {
  light: {
    grey:       [220, 9, 46],
    yellow:     [45, 55, 50],
    barYellow:  [45, 90, 48],
    red:        [0, 80, 38],
  },
  dark: {
    grey:       [220, 9, 62],
    yellow:     [45, 68, 60],
    barYellow:  [45, 85, 55],
    red:        [0, 72, 54],
  },
};

// Swapped wholesale by applyTheme; every reader goes through warn() so a
// theme change needs no second place to remember.
let currentWarn = WARN_PALETTES.light;
const warn = () => currentWarn;

// These colors are written as inline styles, so they can't ride on the
// stylesheet's --alpha-* tokens the way every other color does (see
// dashboard.html). They're percent readouts and bar fills, so they take the
// accent tier -- the same one the bars' track and the card numbers use, so
// a bar's fill and the track behind it fade together. Set by
// applyTranslucency; 1 until then, which emits plain hsl() exactly as
// before.
let warnAlpha = 1;

const hsl = ([h, s, l]) =>
  warnAlpha >= 1
    ? `hsl(${h}, ${s}%, ${l}%)`
    : `hsl(${h} ${s}% ${l}% / ${warnAlpha})`;

// How hard the yellow->red ramp front-loads its move toward red. Below 1
// the hue leaves yellow fast and spends most of the range in the urgent
// end, so the middle of the bar looks like a problem instead of a tame
// amber; a straight linear ramp was still orange-ish at 85%.
const URGENCY_EXP = 0.5;

// Grey below 27%, flat yellow 27-35%, then a yellow->deep-red ramp as it
// climbs from 35% toward (and past) 100% -- utilization can exceed 100%,
// but the color maxes out at the deep red there rather than going further,
// since there's nowhere redder to telegraph. The percent text and the
// bar run the same ramp off their own yellow, so they always agree on
// how bad things are even though they disagree on intensity.
function utilRamp(pct, yellow) {
  if (pct < 27) return hsl(warn().grey);
  if (pct < 35) return hsl(yellow);
  const t = Math.min(1, (pct - 35) / (100 - 35)) ** URGENCY_EXP;
  const red = warn().red;
  return hsl(yellow.map((from, i) => from + (red[i] - from) * t));
}

const utilColor = pct => utilRamp(pct, warn().yellow);
const barColor = pct => utilRamp(pct, warn().barYellow);

// Section boundaries on the usage bar, now matching utilColor's own
// thresholds (27/35) instead of the doc's 33, so a ridge lands exactly
// where the color changes.
const UTIL_BINS = [27, 35, 66];

// Only the ridges you've already crossed, plus the one immediately ahead:
// at 10% you see 27; at 30% you see 27 and 35. Showing all of them at
// once turned the bar into a ruler, where the point is to mark the line
// you're about to cross.
function visibleRidges(pct) {
  const next = UTIL_BINS.find(b => b > pct);
  const shown = UTIL_BINS.filter(b => b <= pct);
  if (next !== undefined) shown.push(next);
  return shown;
}

// The bar is cut into separately-rounded sections at the visible ridges
// only, so the sections appear as you reach them -- the stretch beyond
// the upcoming ridge stays one unbroken block rather than being
// pre-divided by boundaries you haven't earned yet.
function utilBar(rawPct) {
  const pct = Math.max(0, rawPct ?? 0);
  const color = barColor(pct);
  const edges = [0, ...visibleRidges(pct), 100];
  let segs = '';
  for (let i = 0; i < edges.length - 1; i++) {
    const start = edges[i];
    const span = edges[i + 1] - start;
    // Sections grow in proportion to the range they cover, from a zero
    // basis, so the gaps between them come out of the sections rather
    // than skewing their relative widths.
    const filled = Math.max(0, Math.min(1, (pct - start) / span)) * 100;
    const fill = filled > 0
      ? `<div class="fill" style="width:${filled.toFixed(1)}%;background-color:${color}"></div>`
      : '';
    segs += `<div class="range-seg" style="flex-grow:${span}">${fill}</div>`;
  }
  return `<div class="range">${segs}</div>`;
}

// A debit balance that's nearly spent, or overdrawn past a small buffer.
// Anything healthier keeps the card's normal text color.
const LOW_BALANCE = 10;
const OVERDRAWN_BALANCE = -25;

function balanceColor(balance) {
  if (balance === null || balance === undefined) return null;
  if (balance < OVERDRAWN_BALANCE) return hsl(warn().red);
  if (balance <= LOW_BALANCE) return hsl(warn().yellow);
  return null;
}

// Logo + issuing bank name, shared by both card types. Falls back to a
// letter avatar when fetch_data.py couldn't find a logo (cached to logos/,
// refreshed every scheduled run) for that institution. dueText (credit
// cards only) adds last-4 inline plus the due date pushed to the far
// right; without it (debit cards), the caller puts last-4 on its own line
// below instead, to keep the header row itself narrow.
function cardHead(acc, dueText) {
  const initial = (acc.institution_name || '?').charAt(0).toUpperCase();
  const logo = acc.logo
    ? `<img class="card-logo" src="${acc.logo}" alt="">`
    : `<span class="card-logo-fallback">${initial}</span>`;
  const mask = dueText ? `<span class="card-mask">${fmtMask(acc.mask)}</span>` : '';
  const due = dueText ? `<span class="due-inline">Due ${dueText}</span>` : '';
  return `
    <div class="card-head">
      ${logo}
      <span class="bank-name">${acc.institution_name}</span>
      ${mask}${due}
    </div>
  `;
}

// Cards are sized for amounts up to 9999.9; every digit past that needs
// room the default layout doesn't have. The per-digit cost is roughly one
// digit's width at that element's own font size (plus slack for the extra
// thousands separator a longer number picks up), which is why these
// differ: a credit card's balance is set much larger than the credit line
// printed beside it, and larger again than a two-up debit balance.
const FITS_DIGITS = 4;
const DEBIT_PX_PER_DIGIT = 16;
const CREDIT_BALANCE_PX_PER_DIGIT = 20;
const CREDIT_LIMIT_PX_PER_DIGIT = 8;
const DEFAULT_NEW_ROW_DIGITS = 6;

function intDigits(n) {
  return Math.floor(Math.abs(n ?? 0)).toString().length;
}

function digitsOver(n) {
  return Math.max(0, intDigits(n) - FITS_DIGITS);
}

// Extra width this balance needs past the default two-up box, in px.
function debitExtraPx(acc) {
  return digitsOver(acc.balance) * DEBIT_PX_PER_DIGIT;
}

// Credit cards span the full width, so they have no box of their own to
// widen -- their overflow goes straight to the window, which they then
// fill. Balance and credit line are measured separately since they're set
// at different sizes.
function creditExtraPx(acc) {
  return digitsOver(acc.balance) * CREDIT_BALANCE_PX_PER_DIGIT
    + (acc.limit ? digitsOver(acc.limit) * CREDIT_LIMIT_PX_PER_DIGIT : 0);
}

// Same idea as a long word in a line of text: one that's only slightly too
// long stays put and takes the slack, one that's far too long drops onto a
// line of its own. Pairing resumes after it, so a card that breaks out
// doesn't reshuffle the cards around it.
function layoutDebitRows(accounts, newRowDigits) {
  const rows = [];
  let pending = null;
  const flushPending = () => {
    if (pending) rows.push({ cards: [pending], full: false });
    pending = null;
  };
  for (const acc of accounts) {
    if (intDigits(acc.balance) >= newRowDigits) {
      flushPending();
      rows.push({ cards: [acc], full: true });
    } else if (pending) {
      rows.push({ cards: [pending, acc], full: false });
      pending = null;
    } else {
      pending = acc;
    }
  }
  flushPending();
  return rows;
}

// Must track dashboard.html's --card-gap and #cards' horizontal padding.
const CARD_GAP = 8;
const CARDS_SIDE_PADDING = 24;

// One debit box's share of a row nothing has overflowed. Must track
// --debit-base in dashboard.html, including its shadow-border term: these
// widths are written inline (renderDepository), so they, not the CSS, are
// what actually sizes a debit box -- leaving the border out here would end
// the row short of the credit card above it by exactly that much.
function debitBaseWidth() {
  return (BASE_WIDTH - CARDS_SIDE_PADDING + shadowBorder * 2 - CARD_GAP) / 2;
}

// Final width for every box in one row: what it needs on its own, plus a
// slice of whatever the credit cards widened the window by. The slice is
// proportional to how much of the row that box already occupies, so a box
// that's wider to begin with takes more of the slack and a narrower one
// takes less -- the row still ends flush with the credit card above it,
// without the boxes inside it all being forced to the same size.
function distributeRowWidths(cards, creditExtra) {
  const needs = cards.map(acc => debitBaseWidth() + debitExtraPx(acc));
  const total = needs.reduce((sum, n) => sum + n, 0);
  return needs.map(n => n + creditExtra * (n / total));
}

function renderDepository(acc, width) {
  const initial = (acc.institution_name || '?').charAt(0).toUpperCase();
  const logo = acc.logo
    ? `<img class="card-logo debit-logo" src="${acc.logo}" alt="">`
    : `<span class="card-logo-fallback debit-logo">${initial}</span>`;
  const grow = width ? ` style="flex:0 0 ${width.toFixed(2)}px"` : '';
  const warn = balanceColor(acc.balance);
  const warnStyle = warn ? ` style="color:${warn}"` : '';
  return `
    <div class="card"${grow}>
      <div class="card-head debit-head">
        ${logo}
        <div class="debit-info">
          <span class="bank-name">${acc.institution_name}</span>
          <span class="card-mask-line">${fmtMask(acc.mask)}</span>
        </div>
      </div>
      <div class="balance"${warnStyle}>${fmtMoney(acc.balance)}</div>
    </div>
  `;
}

function renderCredit(acc) {
  const due = fmtDate(acc.next_payment_due_date);
  const rawPct = acc.limit ? Math.round((acc.balance / acc.limit) * 1000) / 10 : null;
  const util = rawPct !== null
    ? `<span class="util-pct" style="color:${utilColor(Math.max(0, rawPct))}">${rawPct}%</span>`
    : '';
  return `
    <div class="card credit-card">
      ${cardHead(acc, due)}
      <div class="balance-row">
        <div class="balance">${fmtMoney(acc.balance)}${acc.limit ? ' <span class="of-limit">/ ' + fmtMoney(acc.limit, 0) + '</span>' : ''}</div>
        ${util}
      </div>
      ${utilBar(rawPct)}
    </div>
  `;
}

function renderError(err) {
  return `
    <div class="card error-card">
      <div class="card-head">
        <span class="bank-name">${err.institution_name}</span>
      </div>
      <div class="balance msg">${err.error_message || 'Needs attention'}</div>
    </div>
  `;
}

// Default fallback if appearance.json's layout is missing or fails to load.
// Keyed by account type -- that's the one thing a section can't just be
// "title and order" about, since it's what decides which accounts land in
// it at all. Keeping it as the object key (not a separate "type"/"id"
// field alongside title/order) means the layout someone hand-edits only
// ever has the two things they'd actually want to change.
const DEFAULT_LAYOUT = {
  depository: { title: 'Checking & Savings', order: [] },
  credit: { title: 'Credit Cards', order: [] },
};
const SECTION_TYPES = Object.keys(DEFAULT_LAYOUT);

// appearance.json identifies accounts by "Bank Name,last4" instead of Plaid's
// opaque account_id, since someone hand-editing the ordering has no way to
// know which account_id is which card. Plain comma, not a middle dot --
// easier to type by hand.
function orderKey(acc) {
  return acc.mask ? `${acc.institution_name},${acc.mask}` : `${acc.institution_name} ${acc.name}`;
}

function applyOrder(accounts, orderKeys) {
  const pos = new Map((orderKeys || []).map((key, i) => [key, i]));
  const known = accounts.filter(a => pos.has(orderKey(a)))
    .sort((a, b) => pos.get(orderKey(a)) - pos.get(orderKey(b)));
  const unknown = accounts.filter(a => !pos.has(orderKey(a)));
  return [...known, ...unknown];
}

// appearance.json (accent, scale, extra_spacing, layout, ...) is a standalone
// file -- separate from config.json's Plaid credentials -- so it's safe
// for serve.py to expand and send straight to the browser. Layout used to
// be its own layout.json/fetch; it's just one more thing in this same
// response now, one file instead of two. Edits take effect on the next
// load with no sync script to re-run. Falls back to the page's own
// stylesheet defaults (and DEFAULT_LAYOUT) if appearance.json is missing.
let fetchIntervalMinutes = null;
let currentLayout = DEFAULT_LAYOUT;

// The hard offset shadow, off unless appearance.json asks for it.
// Defaults match the original reference exactly -- a CSS sample by
// micaelgomestavares on Uiverse.io -- at 10px offset, 3px border,
// #323232, so "enabled": true alone gives the look it was taken from,
// with each piece still overridable.
// No default color here on purpose: the stylesheet owns that, because it's
// the one piece of the shadow that has to differ per theme (#323232 on
// light, near-black on dark -- see :root[data-theme="dark"] in
// dashboard.html). An inline style written here would beat the theme block
// and pin the light value in both themes.
const SHADOW_DEFAULTS = { offset: 10, border: 3 };

// How much wider the shadow makes the window. fitWindowToContent computes
// the window width from BASE_WIDTH rather than measuring the page, so this
// has to be kept in step with --shadow-grow in dashboard.html by hand --
// growing one without the other either clips the shadow off the right
// edge of the window or leaves a strip of bare desktop-colored window
// beside the cards.
let shadowGrow = 0;

// The border alone, which debitBaseWidth needs separately: of the two
// things the window grows by, the offset is whitespace the shadow paints
// into and the border is width the cards themselves get back.
let shadowBorder = 0;

function applyShadow(shadow) {
  const on = !!(shadow && shadow.enabled);
  const { offset, border, color } = { ...SHADOW_DEFAULTS, ...(shadow || {}) };
  // Off means zero *lengths*, not an unset color: a 0 0 hard shadow hides
  // exactly behind the opaque card and a 0px border draws nothing, so one
  // .card ruleset covers both states (see dashboard.html).
  const px = on ? { offset, border } : { offset: 0, border: 0 };
  // Exposed to CSS as well as applied: the dark theme wants a slightly
  // darker card when there's no border framing it (see dashboard.html).
  document.documentElement.dataset.shadow = on ? 'on' : 'off';
  const root = document.documentElement.style;
  root.setProperty('--shadow-offset', `${px.offset}px`);
  root.setProperty('--shadow-border', `${px.border}px`);
  // Only when asked for explicitly -- otherwise leave the variable unset so
  // the per-theme stylesheet default applies (see SHADOW_DEFAULTS).
  // The -raw name, not --shadow-color itself: that one is derived from this
  // by the translucency layer in dashboard.html, and writing it here would
  // overwrite the derivation with a flat opaque color.
  if (color) root.setProperty('--shadow-color-raw', color);
  else root.removeProperty('--shadow-color-raw');
  shadowGrow = px.offset + px.border * 2;
  shadowBorder = px.border;
}

// The whole light/dark swap is one attribute on <html>: every color the
// stylesheet paints comes from a --surface-*/--text-* variable, and
// [data-theme="dark"] in dashboard.html redefines exactly those (see
// README "Dark theme"). The JS-computed warning colors are the only ones
// that can't ride along on a CSS variable, so they swap here too.
// --accent is deliberately left alone: it's your live Windows accent
// color, and recoloring it would be second-guessing a choice you already
// made in Windows itself.
function applyTheme(dark) {
  const on = !!dark;
  document.documentElement.dataset.theme = on ? 'dark' : 'light';
  currentWarn = on ? WARN_PALETTES.dark : WARN_PALETTES.light;
}

// How much of the way to fully opaque each tier claws back from the one
// opacity number in appearance.json. 0 means "take the opacity as given",
// 1 means "ignore it, stay solid". The page backdrop is the thing actually
// being made see-through, so it takes the number at face value; the card
// faces give up most of it, since a card has to still read as a slab
// sitting on the wallpaper rather than as a tint of it; and the accent and
// text tiers give up only a little, because legibility against an
// arbitrary wallpaper is the one thing translucency mustn't cost. Hence
// "much slower rate" rather than "not at all" -- at opacity 0.6 the text
// is still at 0.94, which reads as solid but does pick up a trace of
// whatever is behind it, so it belongs to the same surface as everything
// else instead of floating above it.
const TRANSLUCENCY_TIERS = {
  page:    0,
  surface: 0.45,
  accent:  0.80,
  text:    0.88,
};

// Below this, an alpha is close enough to solid that the blending it costs
// buys nothing visible. It's a guard against a hand-typed 0.999, not a
// tuning knob.
const TRANSLUCENCY_EPSILON = 0.002;

// How far toward near-black the section headings and the clock are pulled
// once the page behind them has gone completely. They're the only text not
// sitting on a card, so they're the only text that ends up reading against
// the wallpaper (see --text-faint in dashboard.html). Scaled by how much
// opacity was actually given up, so at 0.9 it's a nudge and at 0.15 it's
// most of the way there. Short of 1 on purpose -- a pure black heading
// against pale text on the cards below looks like a different widget's
// label pasted on top.
const HEADING_DARKEN_MAX = 0.7;

// One input, four alphas -- or the whole thing switched off. Off isn't a
// cosmetic default here: it selects [data-translucent="off"] in
// dashboard.html, which drops all twelve color-mix() calls out of the
// token chain, and it's what tells widget.py's transparent window not to
// be one. So "the flag is false" and "the opacity is 1" both route to it,
// since a fully opaque translucency is just translucency's cost with none
// of its effect.
function applyTranslucency(enabled, opacity) {
  // Clamped rather than trusted: this is a hand-edited JSON file, and an
  // opacity of 0 would render an invisible widget with no way to click
  // anything to fix it (the page is pinned to the desktop layer and takes
  // no mouse input), so the floor is low enough to be dramatic and high
  // enough to still find.
  const raw = Number(opacity ?? 1);
  const o = Math.min(1, Math.max(0.15, Number.isFinite(raw) ? raw : 1));
  const on = !!enabled && o < 1 - TRANSLUCENCY_EPSILON;

  document.documentElement.dataset.translucent = on ? 'on' : 'off';
  const root = document.documentElement.style;

  if (!on) {
    // Removed, not set to 1: these are inline styles on <html>, so leaving
    // them behind would beat the stylesheet's own 1s for the rest of the
    // session and keep a stale value alive across a tray Reload that
    // turned the flag off.
    for (const tier of Object.keys(TRANSLUCENCY_TIERS)) {
      root.removeProperty(`--alpha-${tier}`);
    }
    root.removeProperty('--heading-darken');
    warnAlpha = 1;  // makes hsl() emit its plain three-argument form
    return;
  }

  const alpha = frac => o + (1 - o) * frac;
  for (const [tier, frac] of Object.entries(TRANSLUCENCY_TIERS)) {
    root.setProperty(`--alpha-${tier}`, alpha(frac));
  }
  root.setProperty('--heading-darken', (1 - o) * HEADING_DARKEN_MAX);
  // The inline-styled warning colors, which no CSS variable reaches.
  warnAlpha = alpha(TRANSLUCENCY_TIERS.accent);
}

async function applyAppearance() {
  try {
    const res = await fetch('appearance.json', { cache: 'no-store' });
    if (!res.ok) return;
    const {
      accent, scale, spacing, corner_radius, shadow, dark_theme,
      translucent_background, opacity, fetch_interval_minutes, layout,
    } = await res.json();
    applyShadow(shadow);
    applyTheme(dark_theme);
    // After applyTheme, which swaps the warning palette this reads the
    // alpha for -- order doesn't matter for the CSS variables, but warnAlpha
    // is plain state and may as well be written last.
    applyTranslucency(translucent_background, opacity);
    // --accent-raw, not --accent: the latter is derived from it (see the
    // translucency block in dashboard.html).
    if (accent) document.documentElement.style.setProperty('--accent-raw', accent);
    if (scale) {
      document.documentElement.style.setProperty('--scale', scale);
      currentScale = scale;
    }
    if (corner_radius !== undefined) {
      document.documentElement.style.setProperty('--corner-radius', `${corner_radius}px`);
    }
    if (fetch_interval_minutes) fetchIntervalMinutes = fetch_interval_minutes;
    if (layout) currentLayout = layout;
    const shell = document.getElementById('shell');
    if (shell && spacing) {
      shell.style.margin = `${spacing.top}px ${spacing.right}px ${spacing.bottom}px ${spacing.left}px`;
    }
  } catch (e) {
    // no appearance.json yet -- stylesheet default stands
  }
}

// Minutes left until fetch_data.py's next scheduled run, counting down
// from fetch_interval_minutes. Only recomputed when load() runs (every 5
// min, same as POLL_MS), so it steps in ~5-minute increments rather than
// ticking every second -- there's nothing finer-grained to show anyway,
// since that's the only cadence data.json itself can actually change on.
function minutesUntilNextFetch(lastUpdatedIso) {
  if (!lastUpdatedIso || !fetchIntervalMinutes) return null;
  const last = new Date(lastUpdatedIso).getTime();
  const next = last + fetchIntervalMinutes * 60 * 1000;
  // Not clamped at 0 -- formatCountdown treats anything under 1 (including
  // negative, if the scheduled run is running late) the same way, as "<1m".
  return Math.round((next - Date.now()) / 60000);
}

// Below 5 minutes left, the exact number is misleading anyway (it only
// steps every ~5 min, same as the rest of this countdown) -- but only
// worth hiding behind "<5m" when the configured interval itself is long
// enough for that floor to matter. At a short interval (e.g. testing at
// 1 minute) the whole countdown lives under 5 already, so showing the
// real number stays meaningful.
function formatCountdown(mins) {
  // 0 (or less, if the scheduled run is running late) never reads as a
  // flat "0m" -- the countdown is a lower bound, not a promise it'll
  // land exactly on time.
  if (mins < 1) return '<1m';
  if (fetchIntervalMinutes > 10 && mins < 5) return '<5m';
  return `${mins}m`;
}

// Exposed for widget.py's tray "Reload" to call via evaluate_js -- load()
// alone only re-reads data.json (balances), never appearance.json, which is why
// a manual reload picked up fresh balances but not accent/scale/spacing
// changes. corner_radius is handled separately, window-side, in widget.py
// itself (it clips the native window, not something this page can do).
async function reloadAll() {
  await applyAppearance();
  await load();
}

async function load() {
  try {
    const data = await fetch('data.json', { cache: 'no-store' }).then(r => {
      if (!r.ok) throw new Error('no data.json yet');
      return r.json();
    });
    const layout = currentLayout;

    let updatedText = '';
    if (data.last_updated) {
      const d = new Date(data.last_updated);
      const timeStr = d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
      const mins = minutesUntilNextFetch(data.last_updated);
      // middle dot, not a colon -- keeps it visually distinct from the
      // colon already inside the time itself (e.g. "10:42 PM").
      updatedText = mins !== null ? `${formatCountdown(mins)} · ${timeStr}` : timeStr;
    }

    const cardsEl = document.getElementById('cards');
    const accounts = data.accounts || [];
    const errors = data.errors || [];

    if (accounts.length === 0 && errors.length === 0) {
      cardsEl.innerHTML = '<div class="empty">No accounts yet. Run plaid_link_setup.py to link a bank, then fetch_data.py.</div>';
      return;
    }

    const coveredTypes = new Set(SECTION_TYPES);
    let html = '';
    let updatedPlaced = false;

    // Both overflows are measured before anything renders, because a debit
    // row's widths depend on what the credit cards did to the window, and
    // the credit section renders after the debit one.
    const depositoryDef = layout.depository || DEFAULT_LAYOUT.depository;
    const newRowDigits = depositoryDef.new_row_digit_threshold || DEFAULT_NEW_ROW_DIGITS;
    const widest = (list, measure) => list.reduce((max, a) => Math.max(max, measure(a)), 0);
    const creditExtra = widest(accounts.filter(a => a.type === 'credit'), creditExtraPx);
    // Cards that break onto a row of their own are left out: they span the
    // whole row already, so they never widen a box.
    const debitExtra = widest(
      accounts.filter(a => a.type === 'depository' && intDigits(a.balance) < newRowDigits),
      debitExtraPx,
    );
    const extraWidth = creditExtra + debitExtra;

    for (const type of SECTION_TYPES) {
      const sectionDef = layout[type] || DEFAULT_LAYOUT[type];
      const inSection = accounts.filter(a => a.type === type);
      if (inSection.length === 0) continue;
      const ordered = applyOrder(inSection, sectionDef.order);
      // Lands on whichever section renders first -- same (zoomed) row as
      // its title, as a real flex sibling rather than a viewport overlay.
      const updatedSpan = !updatedPlaced && updatedText
        ? `<span id="updated">${updatedText}</span>`
        : '';
      updatedPlaced = updatedPlaced || !!updatedText;
      html += `<div class="section"><div class="section-title"><span>${sectionDef.title}</span>${updatedSpan}</div>`;
      // Depository cards are short enough content-wise (logo/name/balance,
      // no bar or due date) to fit two per row instead of wasting the right
      // half of the window on whitespace.
      if (type === 'depository') {
        html += '<div class="card-grid">';
        for (const row of layoutDebitRows(ordered, newRowDigits)) {
          html += `<div class="debit-row${row.full ? ' debit-row-full' : ''}">`;
          // A full-row card spans the row on its own (CSS), so it needs no
          // width of its own here.
          const widths = row.full ? [] : distributeRowWidths(row.cards, creditExtra);
          row.cards.forEach((acc, i) => {
            html += renderDepository(acc, widths[i]);
          });
          html += '</div>';
        }
        html += '</div>';
      } else {
        for (const acc of ordered) {
          html += acc.type === 'credit' ? renderCredit(acc) : renderDepository(acc);
        }
      }
      html += `</div>`;
    }

    const leftover = accounts.filter(a => !coveredTypes.has(a.type));
    if (leftover.length) {
      html += `<div class="section"><div class="section-title">Other</div>`;
      for (const acc of leftover) html += renderDepository(acc);
      html += `</div>`;
    }

    if (errors.length) {
      html += `<div class="section"><div class="section-title">Needs Attention</div>`;
      for (const err of errors) html += renderError(err);
      html += `</div>`;
    }

    cardsEl.innerHTML = html;
    document.documentElement.style.setProperty('--extra-width', `${extraWidth}px`);
    currentExtraWidth = extraWidth;
    fitWindowToContent();
  } catch (e) {
    document.getElementById('cards').innerHTML =
      '<div class="empty">Couldn\'t load data.json yet. Run fetch_data.py.</div>';
  }
}

// Shrinks the --app window to fit the actual rendered content instead of
// sitting at a fixed size with leftover whitespace. CSS caps body at
// max-height: 700px, so this never grows past that even with many accounts.
const MAX_WINDOW_HEIGHT = 700;

// Must match #cards' own width in dashboard.html -- the design-width
// anchor that --scale (zoom) is applied against. currentExtraWidth is
// whatever an oversized debit balance added to it on the last render.
const BASE_WIDTH = 325;
let currentScale = 1;
let currentExtraWidth = 0;

function fitWindowToContent() {
  requestAnimationFrame(() => {
    // Width is computed directly from the known scale, not measured --
    // scrollWidth/offsetWidth kept reporting the window's *current* width
    // instead of the zoomed content's true (possibly narrower) width, the
    // same kind of floor that documentElement.scrollHeight has for height
    // (which is why that one reads body instead -- but body's width
    // apparently hits an analogous floor that body's height doesn't).
    const contentWidth = Math.round((BASE_WIDTH + currentExtraWidth + shadowGrow) * currentScale);
    const contentHeight = document.body.scrollHeight;

    if (window.pywebview) {
      // pywebview's frameless window has no browser chrome to account for --
      // the exposed API resizes the actual OS window directly, on both axes
      // so a scale change grows/shrinks the whole window uniformly instead
      // of just the content inside a fixed-size box.
      window.pywebview.api.resize_window(contentWidth, Math.min(contentHeight, MAX_WINDOW_HEIGHT));
      return;
    }

    // Fallback for testing in a plain browser tab (resizeTo needs the
    // chrome frame's overhead added back in).
    const frameOverhead = window.outerHeight - window.innerHeight;
    const target = Math.min(contentHeight + frameOverhead, MAX_WINDOW_HEIGHT);
    try {
      window.resizeTo(contentWidth, target);
    } catch (e) {
      // not resizable in this context -- ignore
    }
  });
}

// pywebview injects window.pywebview slightly after page load (firing
// 'pywebviewready'), not synchronously -- wait for it so the first resize
// call doesn't race ahead of the bridge existing. The timeout lets this
// resolve harmlessly when testing in a plain browser tab instead.
function waitForPywebviewReady() {
  return new Promise(resolve => {
    if (window.pywebview) return resolve();
    window.addEventListener('pywebviewready', resolve, { once: true });
    setTimeout(resolve, 1000);
  });
}

// Scale/spacing must be applied before the first size measurement, or the
// initial window size is computed against the un-scaled layout.
async function init() {
  await waitForPywebviewReady();
  await applyAppearance();
  await load();
  setInterval(load, POLL_MS);
}
init();
