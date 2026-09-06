"""A read-only window mirroring the remote desktop.

Genuinely view-only: it reads the same decoded frame buffer the screenshots
come from and never sends anything back. Clicking or typing in this window does
nothing to the remote session -- use the command box for that.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageTk

from . import icon


class LiveView:
    """A read-only window showing the remote desktop as it changes."""

    SIZES = {"2 fps (light)": 2, "5 fps": 5, "10 fps (heavy)": 10}

    def __init__(self, parent: tk.Tk, client, on_close=None) -> None:
        self.client = client
        self.on_close = on_close
        self._photo = None

        self.top = tk.Toplevel(parent)
        self.top.title("Live view - read only")
        icon.apply(self.top)
        settings = client.settings
        self.top.geometry(f"{min(settings.width, 1100)}x{min(settings.height, 720) + 34}")
        self.top.minsize(320, 240)
        self.top.columnconfigure(0, weight=1)
        self.top.rowconfigure(1, weight=1)

        bar = ttk.Frame(self.top, padding=(8, 5))
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(1, weight=1)
        ttk.Label(bar, text="View only - this window sends no input",
                  foreground="#888", font=("Segoe UI", 8)).grid(row=0, column=0, sticky="w")
        self.rate = tk.StringVar(value="5 fps")
        ttk.Combobox(bar, textvariable=self.rate, values=list(self.SIZES),
                     state="readonly", width=15).grid(row=0, column=2, sticky="e")

        self.canvas = tk.Label(self.top, background="#101010", anchor="center",
                               text="waiting for the first frame...",
                               foreground="#777")
        self.canvas.grid(row=1, column=0, sticky="nsew")

        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self._alive = True
        self._tick()

    def _tick(self) -> None:
        if not self._alive:
            return
        try:
            frame = self.client.current_frame()
        except Exception:  # noqa: BLE001 - a bad frame must not kill the window
            frame = None

        if frame is not None:
            width = max(self.canvas.winfo_width(), 1)
            height = max(self.canvas.winfo_height(), 1)
            if width > 10 and height > 10:
                scale = min(width / frame.width, height / frame.height)
                size = (max(1, int(frame.width * scale)),
                        max(1, int(frame.height * scale)))
                shown = frame.convert("RGB").resize(size, Image.BILINEAR)
                self._photo = ImageTk.PhotoImage(shown)
                self.canvas.configure(image=self._photo, text="")

        fps = self.SIZES.get(self.rate.get(), 5)
        self.top.after(int(1000 / fps), self._tick)

    def close(self) -> None:
        self._alive = False
        if self.on_close is not None:
            self.on_close()
        self.top.destroy()
