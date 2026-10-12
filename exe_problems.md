# Compiling DesktopCards into a single `.exe` — what stands in the way

Findings from reading every entry point against a PyInstaller onefile build.
Verdict: **viable, nothing architecturally blocking**, but two real refactors
come first. Parked for later.

---

## 1. Every path is relative to `__file__`

- `fetch_data.py:37-41` — config, data, cache, logos, config snapshot
- `widget.py:33-37` — config, appearance, cache, appearance snapshot
- `serve.py:38-40` — serving directory, config, appearance
- `accent_color.py:13-14` — appearance
- `setup_schedule.py:13` — config, and the path it hands Task Scheduler

Under a frozen onefile exe, `__file__` resolves inside `sys._MEIPASS`, a temp
directory extracted at launch and **deleted on exit**. So `config.json` (Plaid
secret + access tokens), `appearance.json`, `data.json` and `cache/logos/`
would all be written somewhere that evaporates — every launch would look like
a fresh install.

**Fix:** a path split that doesn't exist today.

- `ASSET_DIR` — bundled, read-only: `dashboard.html`, `dashboard.js`, `logo.png`.
  From `sys._MEIPASS` when frozen, the project dir otherwise.
- `DATA_DIR` — stable, writable, per-user: `config.json`, `appearance.json`,
  `data.json`, `cache/`.

One `paths.py` exporting both; the rest is find-and-replace across the five
files. Needs a first-run seed step too (copy `config.example.json` /
`appearance.example.json` into `DATA_DIR` if absent), since the exe is useless
without a `config.json` and there's no longer a repo folder to `cp` inside.

Open question: `DATA_DIR = %LOCALAPPDATA%\DesktopCards\` (proper, survives the
exe being moved) vs. a folder beside the exe (portable and visible, but breaks
if the exe lands in `Program Files`). Leaning `%LOCALAPPDATA%`, with a tray
item to open it.

## 2. Three places spawn Python by name

- `widget.py:311-314` — `sys.executable.replace("python.exe", "pythonw.exe")`
  + `serve.py`, as a subprocess
- `widget.py:267-270` — the same trick spawning `widget.py` for the tray's
  Restart
- `setup_schedule.py:46` — registers `"pythonw.exe" "fetch_data.py"` with
  Windows Task Scheduler

All three break the same way: frozen, `sys.executable` *is* the exe, there is
no `python.exe` substring to replace, and there is no `serve.py` or
`fetch_data.py` on disk to point at.

**Fix:** a subcommand dispatch on a single entry point —
`DesktopCards.exe --serve`, `--fetch`, no args = the widget. Then
`sys.executable` + a flag covers all three call sites, and the scheduled task
points at the exe. This is cleaner than the current arrangement regardless of
whether the build ever happens.

### Onefile wrinkle: the server should become a thread

A child process spawned from `sys.executable` under onefile re-extracts the
entire bundle a second time (plaid-python, Pillow, pythonnet, the WebView2
interop assemblies) — double unpack cost, two temp copies live at once.

So under the frozen build, **run the HTTP server as a daemon thread in-process
rather than as a subprocess.** It also deletes the orphaned-port problem that
`restart_now`'s docstring (`widget.py:247-259`) documents at length: a thread
dies with its process, so there's nothing to terminate and nothing to race for
the port. `serve.py` already imports `fetch_data` as a module, so there's no
import-cost argument for keeping it separate.

## 3. The genuinely risky dependency: pywebview

pywebview's Windows backend is .NET/WinForms via pythonnet. It ships
PyInstaller hooks and usually works, but it's the most likely piece to need
`--hidden-import` / `--collect-all` tuning, and when it fails it tends to fail
as a silent blank window rather than a traceback. Budget debugging time here
specifically.

The WebView2 runtime itself is an Edge system component — not bundled, and
present on Win11. That part is fine.

Pillow, pystray and plaid-python all bundle without drama.

## 4. Icons — two separate things

- **exe / Start-menu icon:** PyInstaller's `--icon` needs a `.ico`, not a
  `.png`. Generate a multi-size `.ico` from `logo.png` with Pillow as a build
  step, so `logo.png` stays the single source of truth.
- **tray icon:** replace `build_tray_image` (`widget.py:201`) with
  `Image.open()` on the bundled `logo.png`, keeping the drawn circle as a
  fallback if the asset is missing.

Note the trade this makes: the current circle automatically matches
`appearance.json`'s `accent`, as its docstring says. A fixed logo won't.

## 5. "Runnable from the Start menu" is a separate step

PyInstaller doesn't create shortcuts. It needs a `.lnk` written into
`%APPDATA%\Microsoft\Windows\Start Menu\Programs` — ~10 lines of PowerShell or
a small `install.py`. Trivial, just not part of the build.

## 6. Smaller things worth knowing

- An unsigned onefile exe frequently trips SmartScreen on first run, and
  occasionally a Defender heuristic. Personal-use annoyance, not a blocker.
- Onefile costs ~1-3s of unpack per launch; onedir launches instantly but is a
  folder rather than one file. For something launched once and left pinned to
  the desktop, onefile's cost is irrelevant.
- `plaid_link_setup.py` is a Flask app for the Plaid Link flow. Either becomes
  a `--link` subcommand (so the exe is genuinely standalone on a fresh
  machine, at the cost of carrying Flask) or stays a run-from-source dev
  script. Leaning subcommand.

## Suggested order

1. `paths.py` + the `ASSET_DIR` / `DATA_DIR` split across the five files,
   plus first-run seeding.
2. The subcommand entry point, and the server as a thread.
3. Only then the PyInstaller spec, the `.ico` generation, and the shortcut
   installer.

Steps 1 and 2 are worth doing on their own merits even if the build fights us.
