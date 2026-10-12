const POLL_MS = 5 * 60 * 1000; // re-read data.json every 5 min; no network calls of our own

// There's no in-page reload control: the window is reparented onto the
// desktop's WorkerW layer to survive Win+D, and that layer doesn't receive
// normal mouse input (the same reason Rainmeter/Wallpaper Engine skins
// need a system-wide hook to be clickable at all). Manual refresh instead
// lives in the system tray icon (widget.py), which calls POST /refresh
// and then tells this page to reload.

function fmtMoney(n) {
  if (n === null || n === undefined) return '–';
  return n.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
}

function fmtDate(iso) {
  if (!iso) return null;
  const d = new Date(iso + 'T00:00:00');
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
}

function fmtMask(mask) {
  return mask ? `&bull;${mask}` : '';
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

function renderDepository(acc) {
  return `
    <div class="card">
      ${cardHead(acc)}
      <div class="card-mask-line">${fmtMask(acc.mask)}</div>
      <div class="balance">${fmtMoney(acc.balance)}</div>
    </div>
  `;
}

function renderCredit(acc) {
  const due = fmtDate(acc.next_payment_due_date);
  const pct = Math.max(0, Math.min(100, acc.percent_used ?? 0));
  return `
    <div class="card">
      ${cardHead(acc, due)}
      <div class="balance">${fmtMoney(acc.balance)}${acc.limit ? ' <span class="of-limit">/ ' + fmtMoney(acc.limit) + '</span>' : ''}</div>
      <div class="range"><div class="fill" style="width:${pct}%"></div></div>
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

// Default fallback if theme.json's layout is missing or fails to load.
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

// theme.json identifies accounts by "Bank Name,last4" instead of Plaid's
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

// theme.json (accent, scale, extra_spacing, layout, ...) is a standalone
// file -- separate from config.json's Plaid credentials -- so it's safe
// for serve.py to expand and send straight to the browser. Layout used to
// be its own layout.json/fetch; it's just one more thing in this same
// response now, one file instead of two. Edits take effect on the next
// load with no sync script to re-run. Falls back to the page's own
// stylesheet defaults (and DEFAULT_LAYOUT) if theme.json is missing.
let fetchIntervalMinutes = null;
let currentLayout = DEFAULT_LAYOUT;

async function applyTheme() {
  try {
    const res = await fetch('theme.json', { cache: 'no-store' });
    if (!res.ok) return;
    const { accent, scale, spacing, corner_radius, fetch_interval_minutes, layout } = await res.json();
    if (accent) document.documentElement.style.setProperty('--accent', accent);
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
    // no theme.json yet -- stylesheet default stands
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
// alone only re-reads data.json (balances), never theme.json, which is why
// a manual reload picked up fresh balances but not accent/scale/spacing
// changes. corner_radius is handled separately, window-side, in widget.py
// itself (it clips the native window, not something this page can do).
async function reloadAll() {
  await applyTheme();
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
        for (const acc of ordered) html += renderDepository(acc);
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
// anchor that --scale (zoom) is applied against.
const BASE_WIDTH = 400;
let currentScale = 1;

function fitWindowToContent() {
  requestAnimationFrame(() => {
    // Width is computed directly from the known scale, not measured --
    // scrollWidth/offsetWidth kept reporting the window's *current* width
    // instead of the zoomed content's true (possibly narrower) width, the
    // same kind of floor that documentElement.scrollHeight has for height
    // (which is why that one reads body instead -- but body's width
    // apparently hits an analogous floor that body's height doesn't).
    const contentWidth = Math.round(BASE_WIDTH * currentScale);
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
  await applyTheme();
  await load();
  setInterval(load, POLL_MS);
}
init();
