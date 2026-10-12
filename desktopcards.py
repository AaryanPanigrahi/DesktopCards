"""
The one entry point. Everything the app can do is a flag on this file.

Frozen, there is only ever one executable: sys.executable *is* the app, it
takes no script argument, and there are no .py files on disk to point at.
Three places used to spawn Python by name instead --
`pythonw.exe serve.py`, `pythonw.exe widget.py`, and a Task Scheduler
command line -- and all three break the same way under a build. A single
entry point with subcommands means `sys.executable` plus a flag covers
every one of them, from source and frozen alike.

    DesktopCards.exe              the widget (default)
    DesktopCards.exe --serve      just the HTTP server, for debugging
    DesktopCards.exe --fetch      one Plaid pull, then exit
    DesktopCards.exe --link       add a bank through Plaid Link
    DesktopCards.exe --accent     copy the Windows accent into appearance.json
    DesktopCards.exe --setup      first-run setup

Imports are deliberately inside each branch. The widget pulls in pywebview,
pythonnet and the whole .NET interop layer; the Link flow pulls in Flask;
neither has any business loading when the other was asked for. From source
that's a slower start, and frozen it's wasted unpacking.
"""

import os
import sys

LOG_NAME = "desktopcards.log"
MAX_LOG_BYTES = 512 * 1024


def redirect_output_to_log():
    """Points stdout/stderr at a file when there's no console to print to.

    A windowed build has no console, and PyInstaller sets sys.stdout and
    sys.stderr to None rather than to something inert -- so every print()
    in the codebase would raise AttributeError on None.write. That turns
    harmless progress messages into crashes, in the build where they're
    hardest to see.

    Pointing them at a file fixes that and buys the thing this app
    otherwise has no way to produce: a record of what went wrong. The
    known-worst failure mode here is pywebview coming up as a blank window
    with no traceback anywhere, and the tray -- the only control surface --
    not appearing either. A log is the difference between diagnosing that
    and guessing.

    Truncated rather than rotated: this is a debug aid, and the last run is
    the one anyone ever wants."""
    if sys.stdout is not None and sys.stderr is not None:
        return None

    from paths import DATA_DIR, ensure_data_dir

    try:
        ensure_data_dir()
        path = os.path.join(DATA_DIR, LOG_NAME)
        if os.path.exists(path) and os.path.getsize(path) > MAX_LOG_BYTES:
            os.remove(path)
        stream = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
    except OSError:
        # Nowhere writable. Swallow output rather than die for the sake of
        # logging -- os.devnull always opens, and the app still runs.
        stream = open(os.devnull, "w")
        path = os.devnull

    sys.stdout = stream
    sys.stderr = stream
    return path

# A name containing "setup" means the setup build, so a first-time user who
# downloads one file and double-clicks it lands in setup rather than in a
# widget with nothing to show. The installed copy is named DesktopCards.exe
# and gets the widget. An explicit flag always wins over the name, so
# neither file is ever a dead end.
SETUP_NAMES = ("setup", "install")


def looks_like_setup_build():
    name = os.path.basename(sys.executable if getattr(sys, "frozen", False) else sys.argv[0])
    return any(token in name.lower() for token in SETUP_NAMES)


def run_setup():
    import setup_app

    setup_app.main()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    mode = argv[0] if argv else None

    log_path = redirect_output_to_log()
    if log_path:
        import datetime

        # The label has to account for the no-argument case resolving by
        # filename, or the log claims "widget" on a run that did setup.
        label = mode or ("setup" if looks_like_setup_build() else "widget")
        print(f"\n=== DesktopCards {label} "
              f"{datetime.datetime.now().isoformat(timespec='seconds')} ===")

    if mode in ("-h", "--help"):
        print(__doc__.strip())
        return 0

    if mode == "--serve":
        import serve

        serve.main()
    elif mode == "--fetch":
        import fetch_data

        fetch_data.main()
    elif mode == "--link":
        import plaid_link_setup

        plaid_link_setup.main()
    elif mode == "--accent":
        import accent_color

        accent_color.main()
    elif mode == "--setup":
        run_setup()
    elif mode is None:
        if looks_like_setup_build():
            run_setup()
        else:
            import widget

            widget.main()
    else:
        print(f"Unknown option {mode!r}. Try --help.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
