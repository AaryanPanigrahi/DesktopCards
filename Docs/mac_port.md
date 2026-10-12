# Porting DesktopCards to macOS

Verdict: possible, and most of the work is in one file.

## What ports unchanged

`dashboard.html`, `dashboard.js`, `serve.py`, `fetch_data.py`, `appearance.json`
and the Plaid flow – roughly 1100 of ~1300 lines. Every design feature (shadows,
dark theme, translucency, layout) comes along for free. pywebview, pystray,
Pillow and plaid-python all support macOS.

## What has to be rewritten

About 170 lines, in three files:

- **`widget.py`** – 59 lines of `ctypes`/`user32` calls.
- **`setup_schedule.py`** – Windows Task Scheduler. Being deleted anyway in
  favour of an in-process timer thread, so this may be moot by then.
- **`accent_color.py`** – reads the accent colour out of the registry.

## Windows trick → macOS equivalent

| Windows | macOS |
|---|---|
| Reparent onto `WorkerW` to survive Win+D | Set the `NSWindow` level to the desktop level – ~5 lines, and more reliable |
| `SetWindowRgn` for rounded corners | Delete it. macOS composites windows, so CSS `border-radius` on a transparent window just works |
| `transparent=True` + per-pixel alpha | Native. `NSVisualEffectView` even gives real background blur for free |
| Task Scheduler | `launchd`, or nothing once the fetch is a timer thread |
| Registry accent colour | `NSColor.controlAccentColor` |
| `pythonw.exe` to avoid a console | Not needed |

Three of those are *simpler* on macOS than what we have now.

## The two real costs

1. **You must build on a Mac.** No cross-compiling a `.app` from Windows.
2. **Gatekeeper is stricter than SmartScreen.** An unsigned `.app` can't just be
   double-clicked – the user has to right-click → Open and approve it in System
   Settings. Making it click-to-run needs an Apple Developer account
   ($99/year) for notarization.

## What to do now, cheaply

While refactoring `widget.py` for the exe build, pull the Windows-specific calls
behind a `platform_win.py` with a named interface:

```
pin_to_desktop()   round_corners()   system_accent()   install_autostart()
```

Nearly free to do while already editing that file, and it turns the port from a
rewrite into writing one sibling `platform_mac.py`.
