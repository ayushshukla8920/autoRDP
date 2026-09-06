"""Put the autoRDP logo on tkinter windows and headers.

Without this every window shows the default Tk feather: the ``.ico`` is only
embedded in the PyInstaller build, never used by a ``python -m rdpauto.gui``
run. This loads ``assets/autoRDP-256.png`` once and hands out sized copies.

PhotoImages must outlive the widgets that show them, so every image made here
is cached at module scope.
"""

from __future__ import annotations

import logging
import sys
import tkinter as tk
from pathlib import Path

from PIL import Image, ImageTk

from ..config import PROJECT_ROOT

logger = logging.getLogger("rdpauto.gui.icon")


def _asset_candidates() -> list[Path]:
    """Where the logo PNG might live: dev checkout, and a PyInstaller bundle."""
    paths = [PROJECT_ROOT / "assets" / "autoRDP-256.png"]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        paths.append(Path(meipass) / "assets" / "autoRDP-256.png")
    return paths

_source: Image.Image | None = None
_loaded = False
# PhotoImages are bound to one Tk root, so the cache is keyed to it and rebuilt
# when the root changes (else a reopened form hits "image doesn't exist").
_cache: dict[int, ImageTk.PhotoImage] = {}
_cache_root: int | None = None


def _base() -> Image.Image | None:
    global _source, _loaded
    if _loaded:
        return _source
    _loaded = True
    for png in _asset_candidates():
        try:
            _source = Image.open(png).convert("RGBA")
            return _source
        except Exception as exc:  # noqa: BLE001 - a missing icon is cosmetic
            logger.debug("Could not load window icon %s: %s", png, exc)
    _source = None
    return _source


def sized(px: int) -> ImageTk.PhotoImage | None:
    """A square PhotoImage of the logo at ``px`` pixels for the current Tk root,
    or None. Rebuilt when the root changes so it never references a dead one."""
    global _cache_root
    root = tk._default_root
    if root is None:
        return None
    if id(root) != _cache_root:
        _cache.clear()
        _cache_root = id(root)
    if px in _cache:
        return _cache[px]
    base = _base()
    if base is None:
        return None
    _cache[px] = ImageTk.PhotoImage(base.resize((px, px), Image.LANCZOS))
    return _cache[px]


def apply(window: tk.Misc) -> None:
    """Set the window/taskbar icon; a no-op if the asset cannot load."""
    photo = sized(256)
    if photo is not None:
        try:
            window.iconphoto(True, photo)
        except Exception as exc:  # noqa: BLE001
            logger.debug("iconphoto failed: %s", exc)
