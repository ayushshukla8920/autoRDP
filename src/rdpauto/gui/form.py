"""The connection form: a wide two-column layout with grouped sections.

``result`` is ``None`` if the user cancels, otherwise the entered values plus
the chosen action. Secrets (password, Telegram token) can be remembered -- the
password only when the box is ticked, the Telegram token whenever it is typed --
both DPAPI-encrypted; see :mod:`rdpauto.credentials`.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import messagebox, ttk

from .. import credentials
from . import icon

PAD = {"padx": 8, "pady": 4}

# (field name, label, env var, default). Secrets flagged for a Show toggle.
SERVER = [
    ("host", "Host / IP", "RDP_HOST", ""),
    ("port", "Port", "RDP_PORT", "3389"),
    ("username", "Username", "RDP_USERNAME", ""),
    ("domain", "Domain (optional)", "RDP_DOMAIN", ""),
    ("password", "Password", "RDP_PASSWORD", ""),
]
DISPLAY = [
    ("width", "Screen width", "RDP_WIDTH", "1280"),
    ("height", "Screen height", "RDP_HEIGHT", "800"),
]
CODEBASE = [
    ("repo", "Repo URL", "RDP_REPO", ""),
    ("budget", "Minutes (0 = no limit)", "RDP_BUDGET", "0"),
]
TELEGRAM = [
    ("telegram_token", "Bot token", "RDP_TELEGRAM_TOKEN", ""),
    ("telegram_chat_id", "Chat ID", "RDP_TELEGRAM_CHAT_ID", ""),
]
SECRETS = {"password", "telegram_token"}
FALLBACKS = {name: fb for name, _, _, fb in SERVER + DISPLAY + CODEBASE + TELEGRAM}

ACTIONS = [
    ("Connection test", "test"),
    ("Editor demo, Notepad", "demo-notepad"),
    ("Editor demo, VS Code", "demo-code"),
    ("Type a codebase, Notepad", "repo-notepad"),
    ("Type a codebase, VS Code", "repo-code"),
]


class ConnectionForm:
    """Collects connection details across two columns of labelled sections."""

    def __init__(self, root: tk.Tk, show_action: bool = True) -> None:
        self.root = root
        self.result: dict[str, str] | None = None
        self.show_action = show_action
        self.vars: dict[str, tk.StringVar] = {}
        self._entries: dict[str, ttk.Entry] = {}
        self._show: dict[str, tk.BooleanVar] = {}
        self._saved = credentials.load()

        root.title("autoRDP - Background Automation")
        root.resizable(False, False)
        icon.apply(root)

        outer = ttk.Frame(root, padding=14)
        outer.grid(sticky="nsew")
        outer.columnconfigure(0, weight=1, uniform="col")
        outer.columnconfigure(1, weight=1, uniform="col")

        self._header(outer)
        self._server_picker(outer)

        left = ttk.Frame(outer)
        left.grid(row=2, column=0, sticky="nsew", padx=(0, 8))
        left.columnconfigure(0, weight=1)
        self._section(left, 0, "Server", SERVER)
        self._section(left, 1, "Display", DISPLAY)

        right = ttk.Frame(outer)
        right.grid(row=2, column=1, sticky="nsew", padx=(8, 0))
        right.columnconfigure(0, weight=1)
        if show_action:
            self._section(right, 0, "Codebase (for 'type a codebase')", CODEBASE)
        self._section(right, 1, "Telegram alerts (optional)", TELEGRAM)
        if show_action:
            self._actions(right, 2)

        self._footer(outer, row=3)
        self._buttons(outer, row=4)

        root.bind("<Return>", lambda _e: self._submit())
        root.bind("<Escape>", lambda _e: self._cancel())
        root.protocol("WM_DELETE_WINDOW", self._cancel)

    # ---------------------------------------------------------------- layout

    def _header(self, parent: ttk.Frame) -> None:
        head = ttk.Frame(parent)
        head.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 10))
        logo = icon.sized(44)
        if logo is not None:
            lbl = ttk.Label(head, image=logo)
            lbl.image = logo
            lbl.grid(row=0, column=0, rowspan=2, padx=(0, 12))
        ttk.Label(head, text="Connect to the Windows server",
                  font=("Segoe UI", 13, "bold")).grid(row=0, column=1, sticky="w")
        ttk.Label(head, text="Drives its own RDP session - your local desktop is untouched.",
                  foreground="#666", font=("Segoe UI", 8)).grid(row=1, column=1, sticky="w")

    def _server_picker(self, parent: ttk.Frame) -> None:
        """A dropdown of saved servers; picking one fills the whole form."""
        saved_hosts = credentials.hosts()
        if not saved_hosts:
            return
        bar = ttk.Frame(parent)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Label(bar, text="Saved server:").grid(row=0, column=0, sticky="w")
        self.host_pick = tk.StringVar(value=self._saved.get("host", saved_hosts[0]))
        self._combo = ttk.Combobox(bar, textvariable=self.host_pick, values=saved_hosts,
                                   state="readonly", width=32)
        self._combo.grid(row=0, column=1, sticky="w", padx=(6, 0))
        self._combo.bind("<<ComboboxSelected>>",
                         lambda _e: self._load_host(self.host_pick.get()))
        ttk.Label(bar, text="(or type a new host below to add one)",
                  foreground="#777", font=("Segoe UI", 8)).grid(
                      row=0, column=2, sticky="w", padx=(10, 0))

    def _load_host(self, host: str) -> None:
        vals = credentials.load(host)
        for name, var in self.vars.items():
            var.set(vals.get(name, FALLBACKS.get(name, "")))
        self.remember.set(credentials.has_saved_password(host))
        for name, entry in self._entries.items():
            if name in SECRETS:
                entry.configure(show="*")
                self._show[name].set(False)

    def _section(self, parent: ttk.Frame, row: int, title: str, fields) -> None:
        box = ttk.LabelFrame(parent, text=title, padding=(8, 6))
        box.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        box.columnconfigure(1, weight=1)
        for index, (name, label, env, fallback) in enumerate(fields):
            ttk.Label(box, text=label).grid(row=index, column=0, sticky="w", **PAD)
            prefill = ((os.environ.get(env) or "").strip()
                       or self._saved.get(name, "") or fallback)
            var = tk.StringVar(value=prefill)
            entry = ttk.Entry(box, textvariable=var, width=26,
                              show="*" if name in SECRETS else "")
            entry.grid(row=index, column=1, sticky="ew", **PAD)
            self.vars[name] = var
            self._entries[name] = entry
            if name in SECRETS:
                show = tk.BooleanVar(value=False)
                self._show[name] = show
                ttk.Checkbutton(box, text="Show", variable=show,
                                command=lambda n=name: self._toggle_secret(n)).grid(
                                    row=index, column=2, padx=(0, 2))

    def _actions(self, parent: ttk.Frame, row: int) -> None:
        box = ttk.LabelFrame(parent, text="Then run", padding=(8, 6))
        box.grid(row=row, column=0, sticky="ew", pady=(0, 8))
        self.action = tk.StringVar(value="test")
        for text, value in ACTIONS:
            ttk.Radiobutton(box, text=text, variable=self.action,
                            value=value).grid(sticky="w")

    def _footer(self, parent: ttk.Frame, row: int) -> None:
        foot = ttk.Frame(parent)
        foot.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        self.remember = tk.BooleanVar(value=credentials.has_saved_password())
        ttk.Checkbutton(
            foot, variable=self.remember,
            text="Remember password (encrypted for this Windows user)").grid(
                row=0, column=0, sticky="w")
        ttk.Label(foot, text=f"Saved to {credentials.profile_path()}",
                  foreground="#777", font=("Segoe UI", 7)).grid(row=1, column=0, sticky="w")
        ttk.Label(foot,
                  text="The Telegram token & chat ID are remembered too "
                       "(token encrypted); an alert fires only if reconnect fails.",
                  foreground="#777", font=("Segoe UI", 7)).grid(row=2, column=0, sticky="w")

    def _buttons(self, parent: ttk.Frame, row: int) -> None:
        buttons = ttk.Frame(parent)
        buttons.grid(row=row, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="Forget saved", command=self._forget).grid(
            row=0, column=0, padx=4)
        ttk.Button(buttons, text="Cancel", command=self._cancel).grid(
            row=0, column=1, padx=4)
        go = ttk.Button(buttons, text="Connect", command=self._submit)
        go.grid(row=0, column=2, padx=4)
        go.focus_set()

    # ------------------------------------------------------------- behaviour

    def _toggle_secret(self, name: str) -> None:
        self._entries[name].configure(show="" if self._show[name].get() else "*")

    def _submit(self) -> None:
        values = {name: var.get().strip() for name, var in self.vars.items()}
        action = getattr(self, "action", None)
        action = action.get() if action is not None else "test"
        problems = []
        for key, label in (("host", "Host"), ("username", "Username"),
                           ("password", "Password")):
            if not values.get(key):
                problems.append(f"{label} is required.")
        if action.startswith("repo"):
            if not values.get("repo"):
                problems.append("A repository URL is required to type a codebase.")
            budget = values.get("budget") or "0"
            if not budget.isdigit():
                problems.append("Minutes must be a whole number (0 for no limit).")
        for numeric, label in (("port", "Port"), ("width", "Width"),
                               ("height", "Height")):
            raw = values.get(numeric) or ""
            if not raw.isdigit() or int(raw) <= 0:
                problems.append(f"{label} must be a positive number.")
        if values.get("telegram_token") and not values.get("telegram_chat_id"):
            problems.append("A Telegram token needs a chat ID (or clear both).")
        if problems:
            messagebox.showerror("Check the form", "\n".join(problems), parent=self.root)
            return
        credentials.save(values, remember_password=self.remember.get())
        values["action"] = action
        values["remember"] = "1" if self.remember.get() else ""
        self.result = values
        self.root.destroy()

    def _forget(self) -> None:
        """Forget just the server whose host is in the form."""
        host = self.vars["host"].get().strip()
        removed = credentials.forget(host) if host else False
        for name in self.vars:
            self.vars[name].set(FALLBACKS.get(name, ""))
        self.remember.set(False)
        if hasattr(self, "_combo"):
            remaining = credentials.hosts()
            self._combo.configure(values=remaining)
            self.host_pick.set(remaining[0] if remaining else "")
        messagebox.showinfo(
            "Saved server",
            f"Removed {host}." if removed else "Nothing saved for that host.",
            parent=self.root)

    def _cancel(self) -> None:
        self.result = None
        self.root.destroy()
