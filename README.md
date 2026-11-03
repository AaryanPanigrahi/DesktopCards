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

A new Plaid account starts on the free **Trial plan**, which covers both of
the things this widget reads – balances and credit card details – at no cost.
The limit is **10 linked banks**, which is plenty for a personal widget, with
one catch worth knowing up front: that 10 counts every bank you ever link, and
removing a bank does not give the slot back. The Accounts tab shows how many
you have used. Trial is US/Canada only; elsewhere, or past 10 banks, Plaid
needs a paid plan.

Plaid's **sandbox** is free and unlimited, and the same client ID works in
both. Add a sandbox profile (see Profiles) to try things out with fake banks
without spending any of your 10.

If you already had a Plaid account before April 2026, it sits on an older
plan that makes you apply for production access, and Plaid has no
self-service way to move it onto Trial. Create a new team instead (team
switcher, top left of the dashboard → Teams → Create team) and use that
team's keys – a team made today starts on Trial with no application.

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
Accounts tab lists them, and the tick box next to each says whether the widget
draws it. Tick two and both appear together, each card labelled with the
profile it came from; sandbox always runs on its own.

Editing a profile's secret never unlinks anything – a rotated secret keeps
working against banks you've already linked, because Plaid ties those to the
client ID. Changing the client ID with banks linked is refused, because the
tokens wouldn't survive it.

Unlink, reset and delete each save a **backup** first, listed under the same
section. A backup holds that profile's credentials and its bank tokens, so
restoring brings its banks back without spending any Items. The last five
automatic ones are kept; ones you take yourself are kept until you delete
them.

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
a reinstall; clear it and it then offers a backup, ticked, before the folder
goes. Leave that ticked and a restorable copy of your credentials and bank
tokens stays behind in `%LOCALAPPDATA%\DesktopCards\backups\`; clear it and
nothing is kept. Your banks stay linked at Plaid either way. From a checkout:
`python source/desktopcards.py --uninstall`.

## Changelog
### Version 1.2
- Plaid profiles
    - Any number of client ID + environment pairs, several drawing at once
    - Profile label under the bank name on the widget
    - One card order across profiles, kept when a profile is switched off
    - Backups and restore, so unlinking is undoable without spending Items

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
