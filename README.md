# DesktopCards

A small desktop widget that shows your bank balances and credit cards at a
glance, pinned to a corner of your screen. It starts with Windows, keeps
itself up to date, and stays put when you press Win+D.

<img src="logo.png" alt="" width="96" align="right">

Checking and savings cards show the bank, the last four digits and the
balance, two to a row. Credit cards add a usage bar and the next payment due
date, turning yellow then red as more of the limit gets used.

### Supports

- Light/Dark theme, and an 'accent' colour
- Translucency and Shadow Settings
- Changing card layouts
- 'Survive' show desktop (Win + D) toggle
- Rounded Corner, Scale, and Custom Spacing
- Hiding any account you'd rather not see
- Several Plaid accounts at once, so work and personal banks share one widget

All of it changes from a settings page in your browser.

### Before you start

Users **will** have to setup a Plaid API account before using this widget,
instructions are in the setup-script. [Plaid](https://dashboard.plaid.com) is
what reads your balances.

- A new account is on the free **Trial plan**, which covers everything this
  widget reads.
- Trial allows **10 linked banks**, for the life of the account – removing a
  bank doesn't give the slot back. The Accounts tab shows how many you've
  used.
- Plaid's **sandbox** is free and unlimited. Add a sandbox profile to try
  things out with fake banks.
- US/Canada only on Trial.

Had a Plaid account before April 2026? It's on an older plan – make a new
team and use its keys. [Details in the docs](Docs/README.md#getting-started).

**All information is only saved _locally_**

Your keys and bank details stay in a file on your own computer. The widget
runs a small background server so the panel can load, but only this computer
can reach it. A roommate on your Wi-Fi can't scan for it, and neither can
anyone online. Your bank password is only ever typed on Plaid's own page.

## Getting started

Two ways to run it:

**1) From the built app** (easiest, no Python needed)

Download the latest release from the **Releases** section and unzip it, then
run `DesktopCards-Setup.exe`. It opens in your browser and walks you through
your Plaid details, linking banks, a quick test, and installing. Windows will
warn you it isn't signed: choose *More info*, then *Run anyway*.

**2) From Python**

```
pip install -r requirements.txt
python source/desktopcards.py --setup     # first time: credentials + banks
pythonw source/desktopcards.py            # run the widget
```

## Changing things

Right-click the tray icon and choose **Modify** for three tabs: **Accounts**
to add banks and pick which to show, **Layout** to drag cards into order, and
**Appearance** for everything in the list above. The tray also has Reload,
Restart and Quit.

### Profiles

A profile is one Plaid client ID in one environment. **Advanced** on the
Accounts tab lists them, with a tick box for whether the widget draws it.

- Tick two and both appear, each card labelled with its profile.
- Sandbox always runs on its own.
- A new secret keeps working on banks you've already linked. Changing the
  client ID with banks linked is refused.
- Unlink, reset and delete each save a **backup** first, listed under the
  same section. Restoring brings that profile's banks back.

One setting isn't on that page: `shadow_ui` in `config/appearance.json` gives
the settings page's own tabs, boxes and buttons the same hard shadow the cards
have. It's file-only on purpose – a control that restyles the control you're
holding is a toy rather than a setting. Set `enabled` to `false`, or tune the
distances per group, and reload the page.

## Uninstalling

`Uninstall.exe` sits next to the app in `%LOCALAPPDATA%\DesktopCards\`, and
DesktopCards is listed in **Settings → Apps**, so either route works. It
confirms first, with **Keep my settings and linked accounts** ticked – leave
it ticked and your Plaid credentials, linked banks and appearance survive for
a reinstall. Clear it and it offers a backup before the folder goes. Your
banks stay linked at Plaid either way. From a checkout:
`python source/desktopcards.py --uninstall`.

## Changelog
### Version 1.2
- Plaid profiles
    - Any number of client ID + environment pairs
    - Profile label, and multi-profile selection
    - Added Backups and restore
- UI Changes
    - Reload/Save bar pinned to the bottom
    - Move 'Shortcuts' section to the Appearance Tab
- Fixed a bug where non-credit accounts couldn't be added

### Version 1.1
- Fresh Config UI
    - Simplified initial setup 
    - Accounts/Layout/Appearance Tabs
    - Light/Dark Theme (same as widget)
    - Shadows for elements
 - Uninstall script and windows settings integration

## What's in this repo

| | |
|---|---|
| `source/` | The code, and the panel itself |
| `config/` | Example settings. Your real ones are made on first run and stay on your machine. Your Plaid keys live in `secrets.json`, apart from the rest |
| `Docs/` | The full write-up |
| `logo.png` | The app's icon |

[`Docs/README.md`](Docs/README.md) is the long version: every setting, how
refreshing works, and why things are built the way they are.
