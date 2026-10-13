"""
Every Windows-specific call the widget makes, in one place.

Nothing in here is about balances or cards -- it's the OS plumbing that
makes a frameless window behave like a desktop widget: clipping it to
rounded corners, keeping it off the taskbar, and parenting it onto the
desktop's own paint layer so Win+D doesn't take it away.

It exists as its own module so there's exactly one file to write a sibling
of for another platform, rather than a Windows port with Mac exceptions
threaded through it. See mac_port.md -- three of these five are *simpler*
on macOS, where windows are composited and a window level can be set
directly instead of reparenting onto a layer you first have to go find.

The interface the rest of the app depends on:

    round_corners(window, radius)
    hide_from_taskbar(window)
    pin_to_desktop(window)
    system_accent()
    window_rect(hwnd) / erase_vacated_area(window, old_rect)
    create_shortcut(...) / install_autostart(...)
    start_menu_dir() / startup_dir()
    confirm_with_checkbox(...) / message_box(...)
    register_uninstaller(...) / unregister_uninstaller(...)
    delete_after_exit(...)
"""

import ctypes
import ctypes.wintypes
import os
import subprocess
import time


def hwnd_of(window):
    """The native handle for a pywebview window.

    .ToInt64() matters: .ToInt32() truncates a 64-bit HWND on x64 Windows,
    producing a garbage handle that the user32 calls then fail on silently."""
    return ctypes.c_void_p(window.native.Handle.ToInt64())


def round_corners(window, radius):
    """Clips the actual OS window to a rounded-rect region via
    SetWindowRgn -- the area outside it simply isn't painted, so whatever
    is behind the window (desktop or another window) shows through there.
    This is the real way to get a non-rectangular window shape on Win32;
    pywebview's transparent=True only makes WebView2's own background
    transparent against the Form's paint, not the window's actual shape,
    so it doesn't give rounded corners by itself.

    Reads the window's actual client rect in device pixels rather than
    trusting our own WINDOW_WIDTH/resize-height values -- those are CSS
    pixels, and at any display scaling above 100% the real window is
    physically larger, so a region built from the CSS numbers clipped off
    a real slice of the right/bottom edge."""
    if radius <= 0:
        return
    hwnd = hwnd_of(window)
    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(rect))
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    hrgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius, radius)
    ctypes.windll.user32.SetWindowRgn(hwnd, hrgn, True)


def window_rect(hwnd):
    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect


def erase_vacated_area(window, old_rect):
    """Repaints the desktop over the area the window used to cover.

    Only needed when the window is transparent. An opaque window's own
    background brush erases whatever it was painting over, and when it
    shrinks, Windows invalidates the uncovered region of the parent for us
    -- but a transparent window has no brush to erase with, and because
    this one is reparented onto WorkerW (the desktop's own paint layer),
    the thing that would have to redraw underneath is the desktop, which
    never notices it should.

    SetWindowRgn doesn't help -- clipping the window stops *it* painting
    there, which is exactly how stale pixels would end up orphaned rather
    than overwritten.

    Unverified against an actual occurrence: this was written for a ghost
    of the previous frame's text appearing above the widget, which turned
    out to be a stray HTML comment rendering as body text. The mechanism
    it guards against is real, but it has never been seen to fire.

    The old rect is in screen coordinates and RedrawWindow wants the
    parent's client coordinates, which are the same thing for a full-screen
    desktop window at the origin, but converted properly anyway rather than
    relying on that."""
    user32 = ctypes.windll.user32
    user32.GetParent.restype = ctypes.c_void_p
    user32.GetParent.argtypes = [ctypes.c_void_p]

    hwnd = hwnd_of(window)
    parent = user32.GetParent(hwnd)
    if not parent:
        return  # never pinned (survive_show_desktop off) -- DWM handles it

    origin = ctypes.wintypes.POINT(old_rect.left, old_rect.top)
    user32.ScreenToClient(ctypes.c_void_p(parent), ctypes.byref(origin))
    stale = ctypes.wintypes.RECT(
        origin.x,
        origin.y,
        origin.x + (old_rect.right - old_rect.left),
        origin.y + (old_rect.bottom - old_rect.top),
    )

    RDW_INVALIDATE, RDW_ERASE = 0x0001, 0x0004
    RDW_ALLCHILDREN, RDW_UPDATENOW = 0x0080, 0x0100
    user32.RedrawWindow(
        ctypes.c_void_p(parent),
        ctypes.byref(stale),
        None,
        RDW_INVALIDATE | RDW_ERASE | RDW_ALLCHILDREN | RDW_UPDATENOW,
    )


def hide_from_taskbar(window):
    """Hides the window's taskbar button -- WS_EX_TOOLWINDOW instead of
    WS_EX_APPWINDOW. Requires a hide/show cycle for the taskbar to actually
    drop the button; just changing the style bit isn't enough."""
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_APPWINDOW = 0x00040000
    SW_HIDE, SW_SHOW = 0, 5

    user32 = ctypes.windll.user32
    hwnd = hwnd_of(window)

    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    style = (style & ~WS_EX_APPWINDOW) | WS_EX_TOOLWINDOW
    user32.ShowWindow(hwnd, SW_HIDE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    user32.ShowWindow(hwnd, SW_SHOW)


def _find_workerw(user32):
    """Finds the window to reparent onto, behind the desktop icons. Usually
    that's a dedicated WorkerW sibling of whichever top-level window hosts
    SHELLDLL_DefView (Windows 10 1809+), but on builds where SHELLDLL_DefView
    is parented directly to Progman with no separate WorkerW at all (true on
    this machine), Progman itself is the right target."""
    progman = user32.FindWindowW("Progman", None)
    result = ctypes.c_ulong()
    user32.SendMessageTimeoutW(progman, 0x052C, 0, 0, 0, 1000, ctypes.byref(result))

    defview_host = None

    def enum_windows(hwnd_candidate, _lparam):
        nonlocal defview_host
        if user32.FindWindowExW(hwnd_candidate, None, "SHELLDLL_DefView", None):
            defview_host = hwnd_candidate
            return False  # found it, stop enumerating
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    user32.EnumWindows(WNDENUMPROC(enum_windows), 0)

    if not defview_host:
        return None

    workerw = user32.FindWindowExW(None, defview_host, "WorkerW", None)
    return workerw if workerw else defview_host


def pin_to_desktop(window):
    """Reparents the window onto the WorkerW layer behind desktop icons so
    it survives Win+D, the same trick Rainmeter/Wallpaper Engine use. Some
    Windows builds don't have WorkerW ready on the first message -- retry
    a few times before giving up."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.FindWindowW.restype = ctypes.c_void_p
    user32.FindWindowExW.restype = ctypes.c_void_p
    user32.FindWindowExW.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p
    ]
    user32.SetParent.restype = ctypes.c_void_p
    user32.SetParent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    hwnd = hwnd_of(window)

    for _ in range(5):
        workerw = _find_workerw(user32)
        if workerw:
            user32.SetParent(hwnd, workerw)
            print(f"Pinned to desktop (behind icons, target 0x{workerw:X})")
            return

        time.sleep(0.4)

    print("Could not find WorkerW after 5 attempts -- Win+D persistence not applied")


def _ps_quote(value):
    """A string as a PowerShell single-quoted literal.

    Single quotes because PowerShell does no expansion inside them -- a
    path containing $, backticks or spaces (this project lives under
    "Coding Files") stays literal. Doubling is how a single quote is
    escaped there."""
    return "'" + str(value).replace("'", "''") + "'"


def start_menu_dir():
    return os.path.join(
        os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu", "Programs"
    )


def startup_dir():
    return os.path.join(
        os.environ["APPDATA"], "Microsoft", "Windows", "Start Menu", "Programs", "Startup"
    )


def create_shortcut(link_path, target, arguments="", icon=None,
                    working_dir=None, description=None):
    """Writes a .lnk via the shell's own COM object, driven by PowerShell.

    A .lnk is a structured binary format, not a text file, so it can't
    just be written out. The two normal ways to make one are pywin32 and
    WScript.Shell; PowerShell reaches the latter without adding a
    dependency, which matters for something that has to work inside a
    frozen build where every extra package is weight in the bundle.

    Returns the path on success, None on failure -- a missing shortcut is
    a degraded install, not a reason to abandon one that otherwise
    worked."""
    os.makedirs(os.path.dirname(link_path), exist_ok=True)

    # Every field is assigned, including the empty ones. CreateShortcut on
    # an existing .lnk opens it rather than starting fresh, so a field that
    # isn't written keeps whatever was there before. Skipping empty values
    # is how a shortcut ends up pointing at the frozen exe while still
    # carrying a stale "desktopcards.py" argument from when it pointed at
    # pythonw -- which the exe then rejects as an unknown option.
    lines = [
        "$ws = New-Object -ComObject WScript.Shell",
        f"$s = $ws.CreateShortcut({_ps_quote(link_path)})",
        f"$s.TargetPath = {_ps_quote(target)}",
        f"$s.Arguments = {_ps_quote(arguments or '')}",
        f"$s.WorkingDirectory = {_ps_quote(working_dir or os.path.dirname(target))}",
        # ",0" picks the first icon group in the file. Without an index the
        # shell is inconsistent about which size it pulls.
        #
        # Falls back to the target's own icon rather than to "". An empty
        # IconLocation is silently ignored, which would leave a stale icon
        # path on an overwrite; naming the target says the same thing in a
        # way that actually takes effect. For the frozen exe that's also
        # the correct answer -- it has the icon compiled into it.
        f"$s.IconLocation = {_ps_quote((str(icon) if icon else target) + ',0')}",
        f"$s.Description = {_ps_quote(description or '')}",
        "$s.Save()",
    ]

    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", "; ".join(lines)],
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0 or not os.path.exists(link_path):
        print(f"[shortcut] failed for {link_path}: "
              f"{(result.stderr or result.stdout).strip()}")
        return None
    return link_path


def remove_shortcut(link_path):
    try:
        os.remove(link_path)
        return True
    except OSError:
        return False


def autostart_path(name="DesktopCards"):
    return os.path.join(startup_dir(), f"{name}.lnk")


def install_autostart(enabled, target, arguments="", icon=None, name="DesktopCards",
                      working_dir=None):
    """Adds or removes the Startup-folder shortcut.

    The Startup folder rather than the HKCU\\...\\Run registry key: it does
    the same job, but it's visible in a place people already know to look,
    and removable without a registry editor. Uninstalling stays 'delete
    the files'."""
    path = autostart_path(name)
    if not enabled:
        remove_shortcut(path)
        return None
    return create_shortcut(
        path, target, arguments=arguments, icon=icon,
        # Not dirname(target): the target can be pythonw.exe, whose folder
        # has nothing to do with where the script being passed to it lives.
        working_dir=working_dir or os.path.dirname(target),
        description="DesktopCards balance widget",
    )


def claim_single_instance(name="DesktopCards"):
    """Takes an app-wide lock, or reports that someone else holds it.

    Returns True if this process is the first, False if an instance is
    already running.

    A named mutex rather than a lock file or a port check: the kernel
    releases it when the process dies, however it dies, so a crash or a
    kill can't leave a stale lock that blocks every future launch. The
    Local\\ prefix scopes it to this login session, which is the right
    boundary -- two users on one machine each get their own widget.

    This matters because the app is now launched from several places at
    once: a Start-menu shortcut, a Startup shortcut, and setup's Launch
    button. Without it, the second one gets as far as binding the port,
    fails, and dies with a traceback nobody sees."""
    ERROR_ALREADY_EXISTS = 183
    # use_last_error=True is what makes ctypes.get_last_error() meaningful:
    # it snapshots GetLastError immediately after the call, before anything
    # else can overwrite it.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]

    handle = kernel32.CreateMutexW(None, False, f"Local\\{name}_instance")
    err = ctypes.get_last_error()
    if not handle:
        return True  # couldn't create the mutex at all; don't block startup

    # Parked on the function, not dropped: the handle has to stay open for
    # the lifetime of the process or the lock releases immediately.
    claim_single_instance._handle = handle
    return err != ERROR_ALREADY_EXISTS


def release_single_instance():
    """Gives up the instance lock while still running.

    Needed by the tray's Restart, which spawns its replacement *before*
    this process exits. Without this the new process would find the lock
    held by the old one, conclude an instance is already running, and
    exit -- so Restart would quietly kill the widget instead of
    restarting it."""
    handle = getattr(claim_single_instance, "_handle", None)
    if handle:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
        claim_single_instance._handle = None


def system_accent():
    """The user's Windows accent color as #rrggbb, from the registry."""
    import winreg

    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\DWM")
    value, _ = winreg.QueryValueEx(key, "ColorizationColor")
    # Stored as 0xAARRGGBB
    r = (value >> 16) & 0xFF
    g = (value >> 8) & 0xFF
    b = value & 0xFF
    return f"#{r:02x}{g:02x}{b:02x}"


# --------------------------------------------------------------- dialogs
# Uninstall is the one thing this app does with no page to host its own
# UI: the widget is gone or going, setup's server isn't running, and a
# browser window asking "are you sure" that outlives the thing it was
# asking about is worse than no window at all. So these two are the real
# Windows dialogs, drawn by the OS.

_TDF_ALLOW_DIALOG_CANCELLATION = 0x0008
_TDF_VERIFICATION_FLAG_CHECKED = 0x0100
_TDCBF_YES_BUTTON = 0x0002
_TDCBF_NO_BUTTON = 0x0004
_TD_WARNING_ICON = 0xFFFF  # MAKEINTRESOURCEW(-1)
_IDYES = 6

_MB_YESNO = 0x0004
_MB_ICONWARNING = 0x0030
_MB_ICONINFORMATION = 0x0040
_MB_ICONERROR = 0x0010
_MB_SETFOREGROUND = 0x00010000
_MB_TOPMOST = 0x00040000


class _TASKDIALOGCONFIG(ctypes.Structure):
    """commctrl.h's TASKDIALOGCONFIG.

    _pack_ = 1 is not a style choice: the header declares this struct
    under #pragma pack(1), so the natural alignment ctypes would other-
    wise apply puts every field after the first pointer at the wrong
    offset, and the call then reads the title out of the middle of a
    pointer.

    The two icon unions are declared as the pointer-sized member they
    are and handed an integer resource id, which is all
    MAKEINTRESOURCEW produces anyway."""

    _pack_ = 1
    _fields_ = [
        ("cbSize", ctypes.wintypes.UINT),
        ("hwndParent", ctypes.wintypes.HWND),
        ("hInstance", ctypes.wintypes.HINSTANCE),
        ("dwFlags", ctypes.wintypes.UINT),
        ("dwCommonButtons", ctypes.wintypes.UINT),
        ("pszWindowTitle", ctypes.c_wchar_p),
        ("hMainIcon", ctypes.c_void_p),
        ("pszMainInstruction", ctypes.c_wchar_p),
        ("pszContent", ctypes.c_wchar_p),
        ("cButtons", ctypes.wintypes.UINT),
        ("pButtons", ctypes.c_void_p),
        ("nDefaultButton", ctypes.c_int),
        ("cRadioButtons", ctypes.wintypes.UINT),
        ("pRadioButtons", ctypes.c_void_p),
        ("nDefaultRadioButton", ctypes.c_int),
        ("pszVerificationText", ctypes.c_wchar_p),
        ("pszExpandedInformation", ctypes.c_wchar_p),
        ("pszExpandedControlText", ctypes.c_wchar_p),
        ("pszCollapsedControlText", ctypes.c_wchar_p),
        ("hFooterIcon", ctypes.c_void_p),
        ("pszFooter", ctypes.c_wchar_p),
        ("pfCallback", ctypes.c_void_p),
        ("lpCallbackData", ctypes.c_void_p),
        ("cxWidth", ctypes.wintypes.UINT),
    ]


def message_box(text, title="DesktopCards", kind="info"):
    """A plain OK box. Topmost and foreground, because whatever puts one
    up here has usually just stopped being the active window."""
    icons = {"info": _MB_ICONINFORMATION, "warn": _MB_ICONWARNING,
             "error": _MB_ICONERROR}
    ctypes.windll.user32.MessageBoxW(
        None, str(text), str(title),
        icons.get(kind, _MB_ICONINFORMATION) | _MB_SETFOREGROUND | _MB_TOPMOST,
    )


def confirm_with_checkbox(instruction, content, checkbox, title="DesktopCards",
                          checked=True):
    """A Yes/No task dialog with one tick box. Returns (yes, ticked).

    TaskDialogIndirect is what Windows' own uninstall prompts use, and
    the only native dialog that can carry a checkbox -- MessageBoxW has
    no room for one, so the alternative is two windows and a question
    the second of them can no longer take back.

    It lives in comctl32 version 6, which a process only has with visual
    styles enabled by its manifest. That holds for the frozen build and
    for python.exe, but it is a property of how the app was built rather
    than anything this function can arrange, so a missing export falls
    back to two message boxes instead of failing. Esc and the X count as
    No, which is what ALLOW_DIALOG_CANCELLATION is for -- without it the
    dialog has no way out but an answer."""
    checked_out = ctypes.wintypes.BOOL(checked)
    pressed = ctypes.c_int(0)
    cfg = _TASKDIALOGCONFIG()
    cfg.cbSize = ctypes.sizeof(_TASKDIALOGCONFIG)
    cfg.dwFlags = _TDF_ALLOW_DIALOG_CANCELLATION | (
        _TDF_VERIFICATION_FLAG_CHECKED if checked else 0
    )
    cfg.dwCommonButtons = _TDCBF_YES_BUTTON | _TDCBF_NO_BUTTON
    cfg.pszWindowTitle = title
    cfg.hMainIcon = _TD_WARNING_ICON
    cfg.pszMainInstruction = instruction
    cfg.pszContent = content
    cfg.pszVerificationText = checkbox

    try:
        hr = ctypes.windll.comctl32.TaskDialogIndirect(
            ctypes.byref(cfg), ctypes.byref(pressed), None,
            ctypes.byref(checked_out),
        )
    except (AttributeError, OSError) as e:
        print(f"[dialog] no task dialog here ({e}); using message boxes")
        return _confirm_with_two_boxes(instruction, content, checkbox)
    if hr != 0:
        print(f"[dialog] TaskDialogIndirect failed (0x{hr & 0xFFFFFFFF:08x})")
        return _confirm_with_two_boxes(instruction, content, checkbox)
    return pressed.value == _IDYES, bool(checked_out.value)


def _confirm_with_two_boxes(instruction, content, checkbox):
    """The fallback, asked in the same order: No to the first means the
    second is never put up and nothing has happened yet."""
    flags = _MB_YESNO | _MB_ICONWARNING | _MB_SETFOREGROUND | _MB_TOPMOST
    box = ctypes.windll.user32.MessageBoxW
    if box(None, f"{instruction}\n\n{content}", "DesktopCards", flags) != _IDYES:
        return False, False
    return True, box(None, f"{checkbox}?", "DesktopCards", flags) == _IDYES


# ------------------------------------------------------- Add/Remove entry
# HKCU, not HKLM: everything this app installs is per-user and writable
# without a prompt, and an uninstall entry needing administrator to clear
# would be the only part of it that did.
_UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"


def register_uninstaller(uninstall_exe, name="DesktopCards",
                         display_name="DesktopCards", icon=None,
                         install_location=None, version=None,
                         publisher="DesktopCards", size_kb=None):
    """Lists the app in Settings > Apps with a working Uninstall button.

    The command is quoted even though these paths rarely need it: the
    value is handed to a shell, and %LOCALAPPDATA% contains the user's
    own name, which is free to contain a space."""
    import winreg

    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                              _UNINSTALL_KEY + "\\" + name) as key:
            def put(field, value, kind=winreg.REG_SZ):
                if value is not None:
                    winreg.SetValueEx(key, field, 0, kind, value)

            put("DisplayName", display_name)
            put("UninstallString", f'"{uninstall_exe}"')
            put("DisplayIcon", icon or uninstall_exe)
            put("InstallLocation", install_location)
            put("DisplayVersion", version)
            put("Publisher", publisher)
            put("EstimatedSize", size_kb, winreg.REG_DWORD)
            # Both set: a per-user install with no repair path, so the
            # Settings entry offers Uninstall and nothing else.
            put("NoModify", 1, winreg.REG_DWORD)
            put("NoRepair", 1, winreg.REG_DWORD)
        return True
    except OSError as e:
        print(f"[install] could not register the uninstaller: {e}")
        return False


def unregister_uninstaller(name="DesktopCards"):
    """Removes that entry. True once it is gone, however it got there."""
    import winreg

    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, _UNINSTALL_KEY + "\\" + name)
        return True
    except FileNotFoundError:
        return True
    except OSError as e:
        print(f"[uninstall] could not remove the Add/Remove entry: {e}")
        return False


# ------------------------------------------------------------ self-delete
def delete_after_exit(files, directories=(), delay_seconds=3):
    """Deletes files once this process is gone, through a detached shell.

    Windows holds an executing image open, so the uninstaller cannot be
    the thing that deletes the uninstaller. A short-lived cmd that waits
    for us to exit and then removes the files is the standard way round
    that, and it needs no helper binary of its own.

    ping is the sleep: timeout.exe wants a console this detached process
    hasn't got, where ping always takes its interval. Directories go
    through rmdir without /s, so one still holding a file the user asked
    to keep is left alone rather than emptied."""
    steps = [f"ping -n {int(delay_seconds) + 1} 127.0.0.1 >nul"]
    steps += [f'del /f /q "{f}"' for f in files]
    steps += [f'rmdir "{d}"' for d in directories]
    command = "cmd /c " + " & ".join(steps)
    subprocess.Popen(
        command,
        creationflags=(
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        ),
        close_fds=True,
    )
    return command
