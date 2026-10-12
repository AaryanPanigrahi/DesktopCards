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

All of it changes from a settings page in your browser.

### Before you start

Users **will** have to setup a Plaid API account before using this widget,
instructions are in the setup-script. [Plaid](https://dashboard.plaid.com) is
what reads your balances, and it's free for personal use.

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

## What's in this repo

| | |
|---|---|
| `source/` | The code, and the panel itself |
| `config/` | Example settings. Your real ones are made on first run and stay on your machine |
| `Docs/` | The full write-up |
| `logo.png` | The app's icon |

[`Docs/README.md`](Docs/README.md) is the long version: every setting, how
refreshing works, and why things are built the way they are.
