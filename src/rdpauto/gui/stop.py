"""The floating emergency-STOP window.

It only creates and removes the sentinel file, so it needs no connection to the
running automation and works even if that process is wedged.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from . import icon


class StopButton:
    """A small always-on-top window whose button trips the emergency stop."""

    def __init__(self, root: tk.Tk, stop_file: Path) -> None:
        self.root = root
        self.stop_file = stop_file

        root.title("Emergency stop")
        icon.apply(root)
        root.attributes("-topmost", True)
        root.resizable(False, False)

        frame = ttk.Frame(root, padding=14)
        frame.grid()
        self.status = ttk.Label(frame, text="", font=("Segoe UI", 9))
        self.button = tk.Button(frame, text="STOP", command=self._toggle,
                                bg="#c62828", fg="white", activebackground="#8e0000",
                                activeforeground="white", font=("Segoe UI", 20, "bold"),
                                width=10, height=2, relief="raised", bd=3)
        self.button.grid(row=0, column=0, pady=(0, 8))
        self.status.grid(row=1, column=0)
        ttk.Label(frame, text=str(stop_file), foreground="#666",
                  font=("Segoe UI", 7), wraplength=230, justify="center").grid(
                      row=2, column=0, pady=(6, 0))

        root.eval("tk::PlaceWindow . center")
        self._refresh()

    def _toggle(self) -> None:
        if self.stop_file.exists():
            try:
                self.stop_file.unlink()
            except OSError as exc:
                messagebox.showerror("Could not clear", str(exc), parent=self.root)
        else:
            try:
                self.stop_file.write_text("stop\n", encoding="utf-8")
            except OSError as exc:
                messagebox.showerror("Could not create", str(exc), parent=self.root)
        self._refresh()

    def _refresh(self) -> None:
        if self.stop_file.exists():
            self.button.configure(text="RESUME", bg="#2e7d32", activebackground="#1b5e20")
            self.status.configure(text="STOPPED - input is blocked", foreground="#c62828")
        else:
            self.button.configure(text="STOP", bg="#c62828", activebackground="#8e0000")
            self.status.configure(text="Running - click to block all input",
                                  foreground="#2e7d32")
        self.root.after(500, self._refresh)


def show_stop_button(stop_file: Path) -> None:
    root = tk.Tk()
    StopButton(root, stop_file)
    root.mainloop()
