# Personal Balance Dashboard — Build Spec

## What this is

A small desktop widget that shows all my bank balances and credit cards in one
glance, sitting pinned in the top-left corner of my screen. It launches
automatically when Windows starts and refreshes itself every few hours — it is
not meant to be a full budgeting app, just a quiet "what's my money doing"
panel. It should be built so more cards/widgets can be added later without a
rewrite.

**Data source:** Plaid (Balance + Liabilities products only — no
Transactions). Real bank data, not Plaid's sandbox/fake data.

**What it shows, per account:**
- **Debit / checking / savings accounts:** account name, current balance.
  Nothing else — no due date, no percentage.
- **Credit cards:** account name, current balance, total credit limit, a
  "% used" indicator shown as a small badge/ring next to the balance, and the
  next payment due date.

No transaction history or spending breakdowns in this version.

## How it should work

1. **`fetch_data.py`** — a script that calls Plaid's Balance and Liabilities
   endpoints for every linked account, and writes the results to a local
   `data.json` (balances, limits, % used, due dates, last-updated timestamp).
   Scheduled to run every 3 hours via Windows Task Scheduler. It does nothing
   the rest of the time — no polling loop, no background process.
2. **`plaid_link_setup.py`** — a one-time (or "run again to add another bank")
   script that opens Plaid's real hosted Link widget in the browser so I log
   into my bank directly (OAuth where supported). It exchanges the result for
   an access token and saves it locally. My bank credentials never touch this
   script or Claude.
3. **`serve.py`** — a minimal local HTTP server that serves the dashboard
   folder on `localhost` (needed so the page's JavaScript can `fetch()`
   `data.json` without CORS errors). Effectively idle when not being viewed.
4. **`dashboard.html`** — the actual widget UI. Built as independent cards (one
   per account) in a layout that makes it easy to drop in new, unrelated
   widget cards later. Reads `data.json` on load and re-polls it every few
   minutes in case it changed, but does no network calls of its own — all the
   real work already happened in `fetch_data.py`.
5. **Autostart + positioning** — at Windows login, `serve.py` should start and
   a chromeless browser window should open automatically, pinned to the
   **top-left corner** of my screen, sized to **480x700**, roughly:
   ```
   chrome.exe --app=http://localhost:<port> --window-position=20,20 --window-size=480,700
   ```
   (Adjust the exact port/size as needed.) My monitor is **2560x1440** — use
   that to sanity-check the position stays on-screen. No "always on top"
   behavior needed; it should behave like a normal window otherwise. Launch
   mechanism: a shortcut in the Windows Startup folder is preferred over Task
   Scheduler for this piece, since it's simpler to set up and edit by hand.
6. **Config:** a single `config.json` (gitignored, copied from
   `config.example.json`) holding Plaid client_id/secret, environment
   (sandbox vs production), and the saved per-bank access tokens.

## Security

- Never put bank credentials in any script — only use Plaid's hosted Link flow.
- Store Plaid secrets and access tokens only in local `config.json`, which
  must be excluded from git (`.gitignore`) and never logged or printed in full.
- No transactions data is pulled or stored — balances and limits only.

## Build notes / how to proceed

- Use the `plaid-python` SDK for the Plaid calls and `Flask` (or Python's
  built-in `http.server`) for the lightweight local server — keep
  dependencies minimal.
- Keep `fetch_data.py` resilient: if Plaid errors for one linked account
  (e.g., a re-auth is needed), it should still write data for the others and
  flag the broken one in `data.json` rather than crashing silently.
- Design the dashboard cards cleanly (use a real design/dataviz pass for the
  % used indicator — not a default-looking progress bar) but keep the whole
  thing a single self-contained HTML/CSS/JS file plus the generated
  `data.json`, no build step/framework needed.
- **Ask me directly, before writing code, about anything underspecified** —
  especially: my actual Plaid account status (new vs. existing, sandbox vs.
  trial/production), how many banks/cards I'm linking and their names, exact
  Chrome/Edge executable path on my machine, which port to run the local
  server on, and anything else needed to make this runnable rather than
  theoretical. Don't guess silently on details that affect whether it
  actually works on my machine.
