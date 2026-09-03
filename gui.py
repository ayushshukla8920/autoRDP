"""A small tkinter front end.

Two modes:

    python gui.py           # connection form, then a log window that runs it
    python gui.py --stop    # a floating STOP button, nothing else

This is the only entry point: main.py, demo.py and the rest are modules of the
application and refuse to run on their own.

The form picks one of five actions -- an interactive connection test, the
generated-code editor demo, or typing a cloned git repository -- and the runner
window then executes it in this process, streaming its output into a log pane.

Connection details are remembered between runs by :mod:`credentials`. The
password is only saved if you tick "Remember password", and then only as DPAPI
ciphertext tied to your Windows user account.
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from PIL import Image, ImageTk

import codebase
import credentials
import demo
import main as main_module
from config import PROJECT_ROOT, Settings, setup_logging
from input_events import EmergencyStopped, InputError, SessionNotAcceptingInput
from main import LoopThread
from rdp_client import ConnectionFailed, RdpClient

PAD = {"padx": 8, "pady": 4}


class ConnectionForm:
    """Collects connection details. ``result`` is None if the user cancels."""

    FIELDS = [
        ("host", "Host / IP", "RDP_HOST", ""),
        ("port", "Port", "RDP_PORT", "3389"),
        ("username", "Username", "RDP_USERNAME", ""),
        ("domain", "Domain (optional)", "RDP_DOMAIN", ""),
        ("password", "Password", "RDP_PASSWORD", ""),
        ("width", "Screen width", "RDP_WIDTH", "1280"),
        ("height", "Screen height", "RDP_HEIGHT", "800"),
    ]

    # Only used by the "type a codebase" action.
    EXTRA = [
        ("repo", "Repo URL", "RDP_REPO", ""),
        ("budget", "Minutes (0 = no limit)", "RDP_BUDGET", "10"),
    ]

    def __init__(self, root: tk.Tk, show_action: bool = True) -> None:
        self.root = root
        self.result: dict[str, str] | None = None
        self.show_action = show_action

        root.title("RDP Background Automation")
        root.resizable(False, False)

        frame = ttk.Frame(root, padding=12)
        frame.grid(sticky="nsew")

        ttk.Label(frame, text="Connect to the Windows server",
                  font=("Segoe UI", 11, "bold")).grid(
                      row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        # Prefill from the remembered profile, with environment overrides.
        saved = credentials.load()
        self.vars: dict[str, tk.StringVar] = {}
        for index, (name, label, env, fallback) in enumerate(
                self.FIELDS + (self.EXTRA if show_action else []), start=1):
            ttk.Label(frame, text=label).grid(row=index, column=0, sticky="w", **PAD)
            prefill = ((os.environ.get(env) or "").strip()
                       or saved.get(name, "")
                       or fallback)
            var = tk.StringVar(value=prefill)
            entry = ttk.Entry(frame, textvariable=var, width=30,
                              show="*" if name == "password" else "")
            entry.grid(row=index, column=1, sticky="ew", **PAD)
            self.vars[name] = var

        row = len(self.FIELDS) + (len(self.EXTRA) if show_action else 0) + 1

        self.remember = tk.BooleanVar(value=credentials.has_saved_password())
        ttk.Checkbutton(
            frame, variable=self.remember,
            text="Remember password (encrypted for this user)").grid(
                row=row, column=0, columnspan=2, sticky="w", padx=8, pady=(6, 0))
        row += 1
        ttk.Label(frame, text=f"Saved to {credentials.profile_path()}",
                  foreground="#666", font=("Segoe UI", 7), wraplength=330,
                  justify="left").grid(
                      row=row, column=0, columnspan=2, sticky="w", padx=8)
        row += 1

        self.action = tk.StringVar(value="test")
        if show_action:
            ttk.Separator(frame, orient="horizontal").grid(
                row=row, column=0, columnspan=2, sticky="ew", pady=8)
            row += 1
            ttk.Label(frame, text="Then run").grid(row=row, column=0, sticky="w", **PAD)
            box = ttk.Frame(frame)
            box.grid(row=row, column=1, sticky="w")
            ttk.Radiobutton(box, text="Connection test (main.py)",
                            variable=self.action, value="test").grid(sticky="w")
            ttk.Radiobutton(box, text="Editor demo, Notepad (demo.py)",
                            variable=self.action, value="demo-notepad").grid(sticky="w")
            ttk.Radiobutton(box, text="Editor demo, VS Code (demo.py)",
                            variable=self.action, value="demo-code").grid(sticky="w")
            ttk.Radiobutton(box, text="Type a codebase, Notepad",
                            variable=self.action, value="repo-notepad").grid(sticky="w")
            ttk.Radiobutton(box, text="Type a codebase, VS Code",
                            variable=self.action, value="repo-code").grid(sticky="w")
            row += 1

        buttons = ttk.Frame(frame)
        buttons.grid(row=row, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="Forget saved",
                   command=self._forget).grid(row=0, column=0, padx=4)
        ttk.Button(buttons, text="Cancel", command=self._cancel).grid(row=0, column=1, padx=4)
        go = ttk.Button(buttons, text="Connect", command=self._submit)
        go.grid(row=0, column=2, padx=4)
        go.focus_set()

        root.bind("<Return>", lambda _e: self._submit())
        root.bind("<Escape>", lambda _e: self._cancel())
        root.protocol("WM_DELETE_WINDOW", self._cancel)

        # Focus the first empty field so the common case needs no mouse.
        for name, *_ in self.FIELDS:
            if not self.vars[name].get():
                frame.grid_slaves(row=[f[0] for f in self.FIELDS].index(name) + 1,
                                  column=1)[0].focus_set()
                break

    def _submit(self) -> None:
        values = {name: var.get().strip() for name, var in self.vars.items()}
        problems = []
        if not values["host"]:
            problems.append("Host is required.")
        if not values["username"]:
            problems.append("Username is required.")
        if not values["password"]:
            problems.append("Password is required.")
        checks = [("port", "Port"), ("width", "Width"), ("height", "Height")]
        if self.action.get().startswith("repo"):
            if not values.get("repo"):
                problems.append("A repository URL is required to type a codebase.")
            # 0 (or blank) means no limit, so it is checked separately.
            budget = values.get("budget") or "0"
            if not budget.isdigit():
                problems.append("Minutes must be a whole number (0 for no limit).")
        for numeric, label in checks:
            raw = values.get(numeric) or ""
            if not raw.isdigit() or int(raw) <= 0:
                problems.append(f"{label} must be a positive number.")
        if problems:
            messagebox.showerror("Check the form", "\n".join(problems), parent=self.root)
            return
        credentials.save(values, remember_password=self.remember.get())
        values["action"] = self.action.get()
        values["remember"] = "1" if self.remember.get() else ""
        self.result = values
        self.root.destroy()

    def _forget(self) -> None:
        removed = credentials.forget()
        for name, *_ in self.FIELDS:
            if name == "port":
                self.vars[name].set("3389")
            elif name == "width":
                self.vars[name].set("1280")
            elif name == "height":
                self.vars[name].set("800")
            else:
                self.vars[name].set("")
        self.remember.set(False)
        messagebox.showinfo(
            "Saved details",
            "Cleared." if removed else "Nothing was saved.", parent=self.root)

    def _cancel(self) -> None:
        self.result = None
        self.root.destroy()


def collect_connection_details(show_action: bool = False) -> dict[str, str] | None:
    """Show the connection form and return the entered values, or None."""
    root = tk.Tk()
    form = ConnectionForm(root, show_action=show_action)
    root.eval("tk::PlaceWindow . center")
    root.mainloop()
    return form.result


def settings_from_gui() -> Settings | None:
    """Build Settings from the form; everything else keeps its default."""
    values = collect_connection_details(show_action=False)
    if values is None:
        return None
    # Settings.load reads these from the environment, so the form simply
    # populates it. This keeps one code path for configuration.
    os.environ["RDP_HOST"] = values["host"]
    os.environ["RDP_PORT"] = values["port"]
    os.environ["RDP_USERNAME"] = values["username"]
    os.environ["RDP_DOMAIN"] = values["domain"]
    os.environ["RDP_PASSWORD"] = values["password"]
    os.environ["RDP_WIDTH"] = values["width"]
    os.environ["RDP_HEIGHT"] = values["height"]
    return Settings.load(interactive=False)


class StopButton:
    """A small always-on-top window whose button trips the emergency stop.

    It only creates and removes the sentinel file, so it needs no connection to
    the running automation and works even if that process is wedged.
    """

    def __init__(self, root: tk.Tk, stop_file: Path) -> None:
        self.root = root
        self.stop_file = stop_file

        root.title("Emergency stop")
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
        # Someone may have created or deleted the file elsewhere.
        self.root.after(500, self._refresh)


def show_stop_button(stop_file: Path) -> None:
    root = tk.Tk()
    StopButton(root, stop_file)
    root.mainloop()


class _QueueWriter:
    """A text stream that captures one thread's output into a queue.

    ``sys.stdout`` and ``sys.stderr`` are pointed at this while a run is
    active, so the ``print`` calls in demo.py and main.py's command handlers
    land in the log pane unchanged. Logging is configured afterwards, so its
    StreamHandler picks up the redirected stderr and comes along too.

    ``sys.stdout`` is process-global, so capturing unconditionally would
    swallow output from the rest of the program. Writes from ``passthrough_on``
    -- the Tk thread -- therefore go to the real stream, and everything else is
    captured. That covers both threads the session runs on: the worker, and the
    asyncio loop thread that the coroutines and their ``print`` calls execute
    on.
    """

    def __init__(self, queue_: queue.Queue, passthrough,
                 passthrough_on: threading.Thread) -> None:
        self._queue = queue_
        self._passthrough = passthrough
        self._passthrough_on = passthrough_on

    def _is_passthrough(self) -> bool:
        return threading.current_thread() is self._passthrough_on

    def write(self, text: str) -> int:
        if not text:
            return 0
        if self._is_passthrough() and self._passthrough is not None:
            return self._passthrough.write(text)
        self._queue.put(text)
        return len(text)

    def flush(self) -> None:
        if self._is_passthrough() and self._passthrough is not None:
            self._passthrough.flush()

    def isatty(self) -> bool:
        return False


class LiveView:
    """A read-only window showing the remote desktop as it changes.

    This is genuinely view-only. It reads the same decoded frame buffer the
    screenshots come from and never sends anything back, so clicking or typing
    in this window does nothing to the remote session -- use the command box
    for that.

    It is a picture, not a video codec: the buffer only contains regions the
    server has actually sent, and aardwolf decodes a subset of RDP's bitmap
    encodings, so areas it cannot decode simply stay as they were.
    """

    SIZES = {"2 fps (light)": 2, "5 fps": 5, "10 fps (heavy)": 10}

    def __init__(self, parent: tk.Tk, client, on_close=None) -> None:
        self.client = client
        self.on_close = on_close
        self._photo = None

        self.top = tk.Toplevel(parent)
        self.top.title("Live view - read only")
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
                # BILINEAR, not LANCZOS: this runs several times a second and
                # the difference is invisible at these scales.
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


class RunnerWindow:
    """Runs the session in this process and shows its output in a log pane.

    Three threads are in play, which is what keeps the window responsive:

    * the Tk main thread only ever touches widgets, and polls a queue on a timer
    * a worker thread drives the session, so a long ``type`` never blocks the UI
      and the STOP button stays clickable throughout
    * :class:`LoopThread` runs the asyncio loop the RDP client lives on
    """

    MAX_LINES = 5000

    def __init__(self, root: tk.Tk, settings: Settings, action: str,
                 options: dict[str, str] | None = None) -> None:
        self.root = root
        self.settings = settings
        self.action = action
        self.options = options or {}
        self.interactive = action == "test"

        self.log_queue: queue.Queue = queue.Queue()
        self.commands: queue.Queue = queue.Queue()
        self.loop = LoopThread()
        self.client = RdpClient(settings)
        self._finished = threading.Event()
        self.live_view: LiveView | None = None

        root.title(f"RDP Automation - {settings.target}")
        root.geometry("900x560")
        root.minsize(640, 360)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)

        # --- status bar ---
        top = ttk.Frame(root, padding=(10, 8, 10, 4))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(1, weight=1)
        self.status = ttk.Label(top, text="starting...", font=("Segoe UI", 9, "bold"))
        self.status.grid(row=0, column=0, sticky="w")
        ttk.Label(top, text=self._describe(), foreground="#666",
                  font=("Segoe UI", 8)).grid(row=0, column=1, sticky="e")

        # --- log pane ---
        wrap = ttk.Frame(root, padding=(10, 0))
        wrap.grid(row=1, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        self.log = tk.Text(wrap, wrap="none", font=("Consolas", 9),
                           background="#1e1e1e", foreground="#d4d4d4",
                           insertbackground="#d4d4d4", relief="flat",
                           state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(wrap, orient="vertical", command=self.log.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(wrap, orient="horizontal", command=self.log.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        self.log.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.log.tag_configure("err", foreground="#f48771")
        self.log.tag_configure("me", foreground="#6a9955")

        # --- command entry (interactive mode only) ---
        bottom = ttk.Frame(root, padding=(10, 6, 10, 10))
        bottom.grid(row=2, column=0, sticky="ew")
        bottom.columnconfigure(1, weight=1)

        self.command = tk.StringVar()
        if self.interactive:
            ttk.Label(bottom, text="rdp>").grid(row=0, column=0, padx=(0, 6))
            self.entry = ttk.Entry(bottom, textvariable=self.command,
                                   font=("Consolas", 10))
            self.entry.grid(row=0, column=1, sticky="ew")
            self.entry.bind("<Return>", lambda _e: self._send())
            self.entry.bind("<Up>", lambda _e: self._history(-1))
            self.entry.bind("<Down>", lambda _e: self._history(1))
            self.send_button = ttk.Button(bottom, text="Send", command=self._send)
            self.send_button.grid(row=0, column=2, padx=6)
            self._past: list[str] = []
            self._at = 0
        else:
            ttk.Label(bottom, text="").grid(row=0, column=1, sticky="ew")

        buttons = ttk.Frame(bottom)
        buttons.grid(row=0, column=3, sticky="e")
        self.stop_button = tk.Button(buttons, text="STOP", command=self._toggle_stop,
                                     bg="#c62828", fg="white", activebackground="#8e0000",
                                     activeforeground="white",
                                     font=("Segoe UI", 9, "bold"), width=8)
        self.stop_button.grid(row=0, column=0, padx=(0, 6))
        self.view_button = ttk.Button(buttons, text="Live view",
                                      command=self._toggle_live_view)
        self.view_button.grid(row=0, column=1, padx=(0, 6))
        ttk.Button(buttons, text="Screenshots",
                   command=self._open_screenshots).grid(row=0, column=2, padx=(0, 6))
        self.close_button = ttk.Button(buttons, text="Disconnect", command=self._quit)
        self.close_button.grid(row=0, column=3)

        root.protocol("WM_DELETE_WINDOW", self._quit)

        self.loop.start()
        self.worker = threading.Thread(target=self._work, name="rdp-session", daemon=True)
        self.worker.start()
        self.root.after(80, self._drain)
        self.root.after(600, self._refresh_stop)

    def _describe(self) -> str:
        return {"test": "interactive connection test",
                "demo-notepad": "editor demo (Notepad)",
                "demo-code": "editor demo (VS Code)",
                "repo-notepad": "typing a codebase (Notepad)",
                "repo-code": "typing a codebase (VS Code)"}.get(self.action, self.action)

    # -------------------------------------------------------------- UI thread

    def _append(self, text: str, tag: str | None = None) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text, tag or ())
        # Keep the buffer bounded on a long run.
        excess = int(self.log.index("end-1c").split(".")[0]) - self.MAX_LINES
        if excess > 0:
            self.log.delete("1.0", f"{excess + 1}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain(self) -> None:
        """Move queued output into the log pane. Runs on the Tk thread only."""
        try:
            while True:
                chunk = self.log_queue.get_nowait()
                tag = "err" if chunk.startswith(("Error", "Cannot", "Warning",
                                                 "Input error", "Stopped")) else None
                self._append(chunk, tag)
        except queue.Empty:
            pass

        if self._finished.is_set() and self.log_queue.empty():
            self.status.configure(text="finished - disconnected")
            self.close_button.configure(text="Close")
            if self.interactive:
                self.send_button.configure(state="disabled")
                self.entry.configure(state="disabled")
            return
        self.root.after(80, self._drain)

    def _refresh_stop(self) -> None:
        tripped = self.client.stop.tripped
        self.stop_button.configure(
            text="RESUME" if tripped else "STOP",
            bg="#2e7d32" if tripped else "#c62828",
            activebackground="#1b5e20" if tripped else "#8e0000")
        if not self._finished.is_set():
            self.status.configure(
                text="STOPPED - input blocked" if tripped
                else ("connected" if self.client.is_alive else "connecting..."))
            self.root.after(600, self._refresh_stop)

    def _toggle_stop(self) -> None:
        if self.client.stop.tripped:
            self.client.stop.reset()
            self.log_queue.put("[emergency stop cleared]\n")
        else:
            self.client.stop.trip("STOP button")
            self.log_queue.put("[EMERGENCY STOP - input blocked]\n")
        self._refresh_stop()

    def _toggle_live_view(self) -> None:
        """Open or close the read-only mirror of the remote desktop."""
        if self.live_view is not None:
            self.live_view.close()
            return
        if not self.client.is_alive:
            messagebox.showinfo("Live view",
                                "Not connected yet - try again once the session is up.",
                                parent=self.root)
            return
        self.live_view = LiveView(self.root, self.client, on_close=self._live_view_closed)
        self.view_button.configure(text="Hide view")

    def _live_view_closed(self) -> None:
        self.live_view = None
        self.view_button.configure(text="Live view")

    def _open_screenshots(self) -> None:
        folder = self.settings.screenshot_dir
        folder.mkdir(parents=True, exist_ok=True)
        try:
            if hasattr(os, "startfile"):          # Windows
                os.startfile(folder)  # noqa: S606 - a local folder, for the user
            else:                                  # Linux/macOS
                import subprocess
                opener = "open" if sys.platform == "darwin" else "xdg-open"
                subprocess.Popen([opener, str(folder)])
        except (OSError, FileNotFoundError) as exc:
            messagebox.showinfo(
                "Screenshots", f"Saved in {folder} (could not open it: {exc})",
                parent=self.root)

    def _send(self) -> None:
        line = self.command.get().strip()
        if not line:
            return
        self._past.append(line)
        self._at = len(self._past)
        self.command.set("")
        self._append(f"rdp> {line}\n", "me")
        self.commands.put(line)

    def _history(self, delta: int) -> None:
        if not self._past:
            return
        self._at = max(0, min(len(self._past), self._at + delta))
        self.command.set(self._past[self._at] if self._at < len(self._past) else "")

    def _quit(self) -> None:
        if self.live_view is not None:
            self.live_view.close()
        if self._finished.is_set():
            self.loop.close()
            self.root.destroy()
            return
        self.status.configure(text="disconnecting...")
        self.client.stop.trip("window closed")
        self.commands.put(None)          # release an idle interactive worker
        self.root.after(300, self._quit_when_done)

    def _quit_when_done(self) -> None:
        if self._finished.is_set():
            self._drain()
            self.loop.close()
            self.root.destroy()
        else:
            self.root.after(300, self._quit_when_done)

    # ---------------------------------------------------------- worker thread

    def _work(self) -> None:
        real_out, real_err = sys.stdout, sys.stderr
        ui = threading.main_thread()
        sys.stdout = _QueueWriter(self.log_queue, real_out, ui)
        sys.stderr = _QueueWriter(self.log_queue, real_err, ui)
        # Configure logging only now, so its StreamHandler binds the redirect.
        setup_logging(self.settings)
        try:
            if self.settings.stop_file.exists():
                print(f"Removing stale stop file {self.settings.stop_file}")
                self.client.stop.reset()
            if self.interactive:
                self._run_interactive()
            elif self.action.startswith("repo"):
                self._run_codebase()
            else:
                self._run_demo()
        except codebase.CodebaseError as exc:
            print(f"\nRepository problem: {exc}")
        except ConnectionFailed as exc:
            print(f"\nConnection failed: {exc}")
        except EmergencyStopped as exc:
            print(f"\nEmergency stop: {exc}")
        except SessionNotAcceptingInput as exc:
            print(f"\nThe session stopped accepting input: {exc}")
        except InputError as exc:
            print(f"\nInput error: {exc}")
        except Exception as exc:  # noqa: BLE001 - always report, never hang
            print(f"\nUnexpected error: {exc}")
        finally:
            try:
                self.loop.run(self.client.disconnect())
            except Exception as exc:  # noqa: BLE001
                print(f"Warning: unclean disconnect: {exc}")
            sys.stdout, sys.stderr = real_out, real_err
            self._finished.set()

    def _run_demo(self) -> None:
        editor = "code" if self.action == "demo-code" else "notepad"
        args = demo.parse_args(["--editor", editor])
        self.loop.run(demo.run_session(self.client, args))
        print("\nDemo finished.")

    def _run_codebase(self) -> None:
        """Clone the repository, pick what fits the budget, then type it."""
        editor = "code" if self.action == "repo-code" else "notepad"
        args = demo.parse_args(["--editor", editor])
        profile = demo.EDITORS[editor]
        # 0 (or blank) in the form means no limit: type the whole repository.
        budget = float(self.options.get("budget") or 0) * 60.0
        url = self.options["repo"]

        print(f"[1] Cloning {url}")
        checkout = codebase.clone(url)
        print(f"    into {checkout}")

        print("[2] Selecting files")
        files, skipped = codebase.collect(checkout)
        if not files:
            raise codebase.CodebaseError(
                "No typable source files found. The repository may contain only "
                "binaries, very large files, or unsupported extensions.")
        total_chars = sum(f.characters for f in files)
        print(f"    {len(files)} candidate file(s), {total_chars:,} characters")
        if skipped:
            print(f"    {len(skipped)} skipped, first few:")
            for line in skipped[:5]:
                print(f"      - {line}")

        chosen, dropped, estimate = codebase.plan(
            files, self.settings, profile["editor_safe"], budget)
        print(f"\n[3] Typing runs at roughly 20-25 characters a second, so the "
              f"whole repository would take "
              f"{codebase.human_time(sum(codebase.seconds_for(f, self.settings, profile['editor_safe']) for f in files))}.")
        if budget > 0:
            print(f"    Budget is {budget / 60:.0f} min: typing {len(chosen)} file(s), "
                  f"about {codebase.human_time(estimate)}.")
        else:
            print(f"    No budget set: typing all {len(chosen)} file(s), "
                  f"about {codebase.human_time(estimate)}.")
        if dropped:
            print(f"    {len(dropped)} file(s) left out; raise the budget "
                  f"(or set it to 0) to include more.")

        print(f"\n[4] Connecting to {self.settings.target}")
        self.loop.run(self.client.connect())
        print("    Connected successfully")
        self.settings.remember(remember_password=False)

        print("[5] Waiting for the remote desktop to finish starting up")
        settled = self.loop.run(
            self.client.wait_for_desktop(timeout=args.startup_timeout))
        print("    desktop has settled" if settled
              else "    gave up waiting; continuing on the configured delays")

        remote_root = f"{args.remote_dir.rstrip(chr(92))}\\{codebase.slug(url)}"
        print(f"[6] Typing into {remote_root}\n")
        self.loop.run(demo.type_codebase(self.client, args, chosen, remote_root))

    def _run_interactive(self) -> None:
        print(f"Connecting to {self.settings.target} ...")
        self.loop.run(self.client.connect())
        print("\nConnected successfully")
        self.settings.remember(remember_password=False)
        print(f"Remote screen: {self.settings.width}x{self.settings.height}")
        print("Type a command below. 'help' lists them; 'quit' disconnects.\n")

        while True:
            line = self.commands.get()
            if line is None:
                return
            command, _, argument = line.partition(" ")
            command = command.lower()
            if command in ("quit", "exit"):
                print("Disconnecting...")
                return
            if not self.client.is_alive:
                print("The RDP session has disconnected.")
                return
            try:
                if not main_module.dispatch(self.loop, self.client, self.settings,
                                           command, argument.strip(), line):
                    print(f"Unknown command {command!r}. Type 'help' for the list.")
            except EmergencyStopped as exc:
                print(f"Stopped: {exc}")
            except SessionNotAcceptingInput as exc:
                print(f"Cannot send input: {exc}")
                return
            except InputError as exc:
                print(f"Input error: {exc}")
            except Exception as exc:  # noqa: BLE001 - a bad command must not end the session
                print(f"Error: {exc}")


def run_in_window(values: dict[str, str]) -> int:
    """Apply the form values, then run the session in a log window."""
    os.environ.update({
        "RDP_HOST": values["host"],
        "RDP_PORT": values["port"],
        "RDP_USERNAME": values["username"],
        "RDP_DOMAIN": values["domain"],
        "RDP_PASSWORD": values["password"],
        "RDP_WIDTH": values["width"],
        "RDP_HEIGHT": values["height"],
    })
    settings = Settings.load(interactive=False)

    root = tk.Tk()
    RunnerWindow(root, settings, values.get("action", "test"), options=values)
    root.mainloop()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="tkinter front end for rdp-background-automation")
    parser.add_argument("--stop", action="store_true",
                        help="show only the floating emergency STOP button")
    args = parser.parse_args(argv)

    if args.stop:
        stop_file = Path((os.environ.get("RDP_STOP_FILE") or "").strip()
                         or PROJECT_ROOT / "STOP")
        show_stop_button(stop_file)
        return 0

    values = collect_connection_details(show_action=True)
    if values is None:
        print("Cancelled.")
        return 130
    return run_in_window(values)


if __name__ == "__main__":
    sys.exit(main())
