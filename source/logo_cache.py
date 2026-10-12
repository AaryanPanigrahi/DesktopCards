"""
Derived copies of the logo, cached in DATA_DIR/cache.

logo.png is a 1024px square, which is the right thing to keep as the
source and the wrong thing to hand to anything that draws it. The tray
wants ~64px; a Windows shortcut wants a multi-size .ico. Both conversions
are cheap but not free, and neither changes between runs, so they're done
once and kept next to the cached bank logos.

Regenerated whenever the source is newer than the cached copy, which is
what makes replacing logo.png take effect: drop in a new one and the next
launch rebuilds both derivatives rather than serving stale art forever.

This also gets the packaged build closer to "change the logo and
everything follows". The exe's own embedded icon still needs a rebuild --
Windows reads that from a resource compiled into the file -- but the
Start-menu and Startup shortcuts can point at a cached .ico instead, and
that one does update.
"""

import hashlib
import json
import os

from PIL import Image

from paths import CACHE_DIR, logo_path

TRAY_SIZE = 64
TRAY_CACHE = os.path.join(CACHE_DIR, f"logo_tray_{TRAY_SIZE}.png")
ICO_CACHE = os.path.join(CACHE_DIR, "logo.ico")

# What Windows actually asks for, smallest to largest. All of them go in
# rather than one scaled badly at display time -- the shell picks whichever
# fits the context it's drawing in (16px in a titlebar, 256px in a folder
# view).
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


STAMP_PATH = os.path.join(CACHE_DIR, "logo_cache.json")


def _fingerprint(source):
    """A short content hash of the source image, plus its size.

    Not the modification time, which is the obvious choice and the wrong
    one here. Under a onefile build the bundled logo.png is extracted to a
    fresh temp directory on every launch, so its mtime is always newer
    than anything cached -- an mtime comparison would rebuild both
    derivatives every single run, which is precisely what the cache exists
    to avoid. Content doesn't change when the file is re-extracted, so
    hashing it gives a stable answer frozen and still notices a genuinely
    different logo.

    Reading a one-megabyte PNG and hashing it costs far less than
    re-encoding seven .ico sizes."""
    with open(source, "rb") as f:
        data = f.read()
    return {"size": len(data), "sha": hashlib.sha256(data).hexdigest()[:16]}


def _read_stamp():
    try:
        with open(STAMP_PATH, "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_stamp(stamp):
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        with open(STAMP_PATH, "w") as f:
            json.dump(stamp, f, indent=2)
    except OSError:
        pass  # a cache that can't remember is slow, not broken


def _is_stale(cached, source, key):
    """Whether this cached derivative needs rebuilding.

    Missing counts as stale, as does a source whose content no longer
    matches what the cached copy was built from."""
    if not os.path.exists(cached):
        return True
    return _read_stamp().get(key) != _fingerprint(source)


def _mark_fresh(source, key):
    stamp = _read_stamp()
    stamp[key] = _fingerprint(source)
    _write_stamp(stamp)


def _load_rgba(source):
    img = Image.open(source)
    img.load()
    # RGBA so transparent corners stay transparent rather than going black,
    # and so a palette-mode PNG doesn't lose its alpha on the way in.
    return img.convert("RGBA")


def write_ico(source, dest, sizes=ICO_SIZES):
    """Writes a multi-size .ico from an image. Used by the build for the
    exe's embedded icon and at runtime for the shortcut icon, so there's
    one implementation of what a DesktopCards icon looks like."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    _load_rgba(source).save(dest, format="ICO", sizes=sizes)
    return dest


def tray_image(size=TRAY_SIZE):
    """The tray icon as a PIL image, cached at tray size.

    Returns None if the logo can't be read at all -- the caller has a
    drawn fallback, and the tray is the app's only control surface, so
    failing to produce an icon must never mean failing to produce a tray."""
    source = logo_path()
    try:
        if _is_stale(TRAY_CACHE, source, "tray"):
            os.makedirs(CACHE_DIR, exist_ok=True)
            img = _load_rgba(source)
            if max(img.size) > size:
                img = img.resize((size, size), Image.LANCZOS)
            img.save(TRAY_CACHE, format="PNG")
            _mark_fresh(source, "tray")
            print(f"[logo] cached tray icon {size}px from {source}")
        return Image.open(TRAY_CACHE).convert("RGBA")
    except Exception as e:
        print(f"[logo] could not prepare a tray icon from {source}: {e}")
        return None


def icon_file():
    """Path to a cached .ico built from the current logo, or None.

    What shortcuts point at, so changing logo.png changes the Start-menu
    icon on the next run that notices."""
    source = logo_path()
    try:
        if _is_stale(ICO_CACHE, source, "ico"):
            write_ico(source, ICO_CACHE)
            _mark_fresh(source, "ico")
            print(f"[logo] cached shortcut icon from {source}")
        return ICO_CACHE
    except Exception as e:
        print(f"[logo] could not prepare an .ico from {source}: {e}")
        return None
