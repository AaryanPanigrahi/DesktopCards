# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for DesktopCards. Build with:

    python -m PyInstaller DesktopCards.spec --noconfirm

A spec file rather than a long command line, because several of the
settings below need explaining and a shell invocation has nowhere to say
why.

Run build.py instead if you want the icon regenerated first.
"""

import os

# ---------------------------------------------------------------- assets
# Everything the app reads but never writes. These land next to the
# bundled Python inside the onefile archive, which paths.ASSET_DIR
# resolves to at runtime via sys._MEIPASS.
#
# config.example.json and appearance.example.json are here because a
# frozen first run has no repo folder to copy them out of -- setup seeds
# DATA_DIR from these.
datas = [
    ("dashboard.html", "."),
    ("dashboard.js", "."),
    ("logo.png", "."),
    ("config.example.json", "."),
    ("appearance.example.json", "."),
]

# ---------------------------------------------------------- hidden imports
# desktopcards.py imports each mode's dependencies inside its own branch,
# which is good for startup cost but invisible to PyInstaller's static
# analysis -- a function-local `import widget` is not a module-level one it
# can follow from the entry point. Naming them here is what keeps the
# non-default subcommands from being left out of the bundle entirely.
hiddenimports = [
    "widget",
    "serve",
    "fetch_data",
    "accent_color",
    "plaid_link_setup",
    "setup_app",
    "platform_win",
    "logo_cache",
    "paths",
    # pywebview resolves its backend by name at runtime, so the winforms
    # module and the pythonnet bridge it needs aren't reachable statically
    # either. This is the part most likely to need adjusting -- pywebview
    # failing to load its backend shows up as a blank window, not an
    # ImportError.
    "webview.platforms.winforms",
    "clr",
]

a = Analysis(
    ["desktopcards.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # tkinter comes with the stdlib and nothing here uses it; excluding it
    # saves several MB of Tcl/Tk that would otherwise be unpacked on every
    # launch.
    excludes=["tkinter"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="DesktopCards",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX-packed exes trip antivirus heuristics far more often
    upx_exclude=[],
    runtime_tmpdir=None,
    # No console. The widget is a desktop ornament -- a console window
    # behind it would defeat the point, and it's pinned to the desktop
    # layer where it can't be focused anyway.
    #
    # This is why desktopcards.redirect_output_to_log exists: windowed,
    # sys.stdout is None and every print() would raise. It also means
    # setup can't be a terminal prompt -- it runs in the browser, which
    # is where the Plaid Link flow already lives.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(os.getcwd(), "DesktopCards.ico"),
)
