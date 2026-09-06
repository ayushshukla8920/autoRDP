"""Runs the session in this process and streams its output into a log pane.

Three threads keep the window responsive:

* the Tk main thread only ever touches widgets, and polls a queue on a timer
* a worker thread drives the session, so a long ``type`` never blocks the UI
  and the STOP button stays clickable throughout
* :class:`LoopThread` runs the asyncio loop the RDP client lives on
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from .. import codebase, console, session
from ..config import Settings, setup_logging
from ..input_events import (EmergencyStopped, InputError,
                            SessionNotAcceptingInput)
from ..logfmt import classify as _classify
from ..rdp_client import ConnectionFailed
from . import icon
from .liveview import LiveView

# Status-dot colours by state.
DOT = {"connecting": "#f9a825", "connected": "#2e7d32",
       "stopped": "#c62828", "finished": "#888888"}


class _QueueWriter:
    """A text stream that captures one thread's output into a queue.

    ``sys.stdout``/``sys.stderr`` are pointed here while a run is active, so the
    ``print`` calls in session.py land in the log pane unchanged. Writes from
    ``passthrough_on`` (the Tk thread) go to the real stream; everything else --
    the worker and the asyncio loop thread -- is captured.
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


class RunnerWindow:
    """Runs one session and shows its output, with live status."""

    MAX_LINES = 5000

    def __init__(self, root: tk.Tk, app, action: str,
                 options: dict[str, str] | None = None) -> None:
        self.root = root
        self.app = app                       # shared loop + client across actions
        self.settings = app.client.settings
        self.action = action
        self.options = options or {}
        self.interactive = action == "test"

        self.log_queue: queue.Queue = queue.Queue()
        self.commands: queue.Queue = queue.Queue()
        self.loop = app.loop
        self.client = app.client
        self._finished = threading.Event()
        self.live_view: LiveView | None = None
        self.progress = session.Progress()
        self.is_repo = action.startswith("repo")
        self.restart = False       # Menu button sets this to reopen the form

        root.title(f"autoRDP - {self.settings.target}")
        icon.apply(root)
        root.geometry("900x600")
        root.minsize(640, 380)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)   # the log pane grows

        # --- status bar with a coloured dot ---
        top = ttk.Frame(root, padding=(10, 8, 10, 4))
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(2, weight=1)
        self.dot = tk.Canvas(top, width=14, height=14, highlightthickness=0)
        self._dot_id = self.dot.create_oval(2, 2, 12, 12, fill=DOT["connecting"],
                                            outline="")
        self.dot.grid(row=0, column=0, padx=(0, 6))
        self.status = ttk.Label(top, text="starting...", font=("Segoe UI", 9, "bold"))
        self.status.grid(row=0, column=1, sticky="w")
        ttk.Label(top, text=self._describe(), foreground="#666",
                  font=("Segoe UI", 8)).grid(row=0, column=2, sticky="e")

        # --- progress panel (codebase runs only) ---
        self._build_progress(root)

        # --- log pane ---
        wrap = ttk.Frame(root, padding=(10, 0))
        wrap.grid(row=2, column=0, sticky="nsew")
        wrap.columnconfigure(0, weight=1)
        wrap.rowconfigure(0, weight=1)
        self.log = tk.Text(wrap, wrap="none", font=("Consolas", 9),
                           background="#1e1e1e", foreground="#d4d4d4",
                           insertbackground="#d4d4d4", relief="flat", state="disabled")
        self.log.grid(row=0, column=0, sticky="nsew")
        yscroll = ttk.Scrollbar(wrap, orient="vertical", command=self.log.yview)
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll = ttk.Scrollbar(wrap, orient="horizontal", command=self.log.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        self.log.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.log.tag_configure("err", foreground="#f48771")            # errors
        self.log.tag_configure("me", foreground="#6a9955")             # your input
        self.log.tag_configure("step", foreground="#4fc1ff",
                               font=("Consolas", 9, "bold"))           # [n] step headers
        self.log.tag_configure("file", foreground="#dcdcaa",
                               font=("Consolas", 9, "bold"))           # [n/n] file headers
        self.log.tag_configure("ok", foreground="#6a9955")             # success / done
        self.log.tag_configure("warn", foreground="#e5c07b")           # reconnect / warnings
        self.log.tag_configure("dim", foreground="#7a7a7a")            # indented detail / logs
        self._linebuf = ""

        # --- command entry (interactive mode only) + buttons ---
        bottom = ttk.Frame(root, padding=(10, 6, 10, 10))
        bottom.grid(row=3, column=0, sticky="ew")
        bottom.columnconfigure(1, weight=1)

        self.command = tk.StringVar()
        if self.interactive:
            ttk.Label(bottom, text="rdp>").grid(row=0, column=0, padx=(0, 6))
            self.entry = ttk.Entry(bottom, textvariable=self.command, font=("Consolas", 10))
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
        self.menu_button = ttk.Button(buttons, text="Menu", command=self._back_to_menu)
        self.menu_button.grid(row=0, column=3, padx=(0, 6))
        self.close_button = ttk.Button(buttons, text="Disconnect", command=self._quit)
        self.close_button.grid(row=0, column=4)

        root.protocol("WM_DELETE_WINDOW", self._quit)

        # The loop is owned and already started by the shared AppSession.
        self.worker = threading.Thread(target=self._work, name="rdp-session", daemon=True)
        self.worker.start()
        self.root.after(80, self._drain)
        self.root.after(600, self._refresh_stop)
        if self.is_repo:
            self.root.after(500, self._refresh_progress)

    def _build_progress(self, root: tk.Tk) -> None:
        """A files/percent/ETA panel, shown only for codebase runs."""
        if not self.is_repo:
            return
        box = ttk.LabelFrame(root, text="Progress", padding=(10, 6))
        box.grid(row=1, column=0, sticky="ew", padx=10, pady=(4, 2))
        box.columnconfigure(0, weight=1)

        self.pbar = ttk.Progressbar(box, mode="determinate", maximum=100)
        self.pbar.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.pct = ttk.Label(box, text="0%", font=("Segoe UI", 11, "bold"), width=6)
        self.pct.grid(row=0, column=1, sticky="e")

        info = ttk.Frame(box)
        info.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        info.columnconfigure(1, weight=1)
        self.p_files = ttk.Label(info, text="Files 0 / 0", font=("Segoe UI", 9, "bold"))
        self.p_files.grid(row=0, column=0, sticky="w")
        self.p_current = ttk.Label(info, text="waiting to start...",
                                   foreground="#555", font=("Segoe UI", 9))
        self.p_current.grid(row=0, column=1, sticky="w", padx=(14, 0))
        self.p_time = ttk.Label(info, text="", foreground="#555",
                                font=("Segoe UI", 9))
        self.p_time.grid(row=0, column=2, sticky="e")

    def _refresh_progress(self) -> None:
        p = self.progress
        if p.active and p.total:
            pct = int(100 * p.done / p.total)
            self.pbar.configure(value=pct)
            self.pct.configure(text=f"{pct}%")
            self.p_files.configure(text=f"Files {p.done} / {p.total}")
            if p.phase == "done":
                self.p_current.configure(text="all files typed", foreground="#2e7d32")
                self.p_time.configure(
                    text=f"took {codebase.human_time(p.elapsed())}")
            else:
                shown = p.current if len(p.current) <= 48 else "..." + p.current[-45:]
                self.p_current.configure(
                    text=f"[{p.current_index}/{p.total}] {shown} "
                         f"({p.current_lines} lines)", foreground="#555")
                self.p_time.configure(
                    text=f"elapsed {codebase.human_time(p.elapsed())}   "
                         f"left ~{codebase.human_time(p.eta_seconds())}")
        elif not p.active and not self._finished.is_set():
            self.p_current.configure(text="cloning & estimating...")
        if not self._finished.is_set():
            self.root.after(500, self._refresh_progress)

    def _describe(self) -> str:
        return {"test": "interactive connection test",
                "demo-notepad": "editor demo (Notepad)",
                "demo-code": "editor demo (VS Code)",
                "repo-notepad": "typing a codebase (Notepad)",
                "repo-code": "typing a codebase (VS Code)"}.get(self.action, self.action)

    def _set_dot(self, state: str) -> None:
        self.dot.itemconfigure(self._dot_id, fill=DOT.get(state, DOT["connecting"]))

    # -------------------------------------------------------------- UI thread

    def _append(self, text: str, tag: str | None = None) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text, tag or ())
        excess = int(self.log.index("end-1c").split(".")[0]) - self.MAX_LINES
        if excess > 0:
            self.log.delete("1.0", f"{excess + 1}.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain(self) -> None:
        # Buffer to whole lines so each is coloured by its own content, not by
        # whichever fragment a print() happened to flush.
        try:
            while True:
                self._linebuf += self.log_queue.get_nowait()
        except queue.Empty:
            pass
        while "\n" in self._linebuf:
            line, self._linebuf = self._linebuf.split("\n", 1)
            self._append(line + "\n", _classify(line))

        if self._finished.is_set() and self.log_queue.empty():
            if self._linebuf:                      # flush any trailing partial line
                self._append(self._linebuf, _classify(self._linebuf))
                self._linebuf = ""
            self.status.configure(text="finished - disconnected")
            self._set_dot("finished")
            self.close_button.configure(text="Close")
            if self.interactive:
                self.send_button.configure(state="disabled")
                self.entry.configure(state="disabled")
            return
        self.root.after(80, self._drain)

    def _refresh_stop(self) -> None:
        paused = self.client.stop.paused
        self.stop_button.configure(
            text="RESUME" if paused else "STOP",
            bg="#2e7d32" if paused else "#c62828",
            activebackground="#1b5e20" if paused else "#8e0000")
        if not self._finished.is_set():
            if paused:
                self.status.configure(text="PAUSED - typing suspended")
                self._set_dot("stopped")
            elif self.client.is_alive:
                self.status.configure(text="connected")
                self._set_dot("connected")
            else:
                self.status.configure(text="connecting...")
                self._set_dot("connecting")
            self.root.after(600, self._refresh_stop)

    def _toggle_stop(self) -> None:
        if self.client.stop.paused:
            self.client.stop.resume()
            self.log_queue.put("[resumed - typing continues]\n")
        else:
            self.client.stop.pause("STOP button")
            self.log_queue.put("[PAUSED - typing suspended, session kept]\n")
        self._refresh_stop()

    def _toggle_live_view(self) -> None:
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

    def _back_to_menu(self) -> None:
        """Stop the current action and close this window, keeping the connection
        alive; the app then reopens the form so the next action reuses it."""
        self.restart = True
        self._quit()

    def _quit(self) -> None:
        # Close this window. The shared loop/connection are owned by the
        # AppSession, so this only stops the current action -- it never closes
        # the loop or disconnects here (the app does that on final exit).
        if self.live_view is not None:
            self.live_view.close()
        if self._finished.is_set():
            self.root.destroy()
            return
        self.status.configure(text="stopping...")
        self.client.stop.trip("window closed")
        self.commands.put(None)
        self.root.after(300, self._quit_when_done)

    def _quit_when_done(self) -> None:
        if self._finished.is_set():
            self._drain()
            self.root.destroy()
        else:
            self.root.after(300, self._quit_when_done)

    # ---------------------------------------------------------- worker thread

    def _work(self) -> None:
        real_out, real_err = sys.stdout, sys.stderr
        ui = threading.main_thread()
        sys.stdout = _QueueWriter(self.log_queue, real_out, ui)
        sys.stderr = _QueueWriter(self.log_queue, real_err, ui)
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
            # Leave the connection up: the AppSession reuses it for the next
            # action, and disconnects only when the whole app closes.
            sys.stdout, sys.stderr = real_out, real_err
            # Rebind logging to the real stderr so later records (e.g. a shared-
            # loop disconnect) don't pile up in a queue that is no longer drained.
            setup_logging(self.settings)
            self._finished.set()

    def _run_demo(self) -> None:
        editor = "code" if self.action == "demo-code" else "notepad"
        args = session.parse_args(["--editor", editor])
        self.loop.run(session.run_session(self.client, args))
        print("\nDemo finished.")

    def _run_codebase(self) -> None:
        editor = "code" if self.action == "repo-code" else "notepad"
        args = session.parse_args(["--editor", editor])
        budget = float(self.options.get("budget") or 0) * 60.0
        self.loop.run(session.run_codebase_session(
            self.client, args, self.options["repo"], budget, self.progress))

    def _run_interactive(self) -> None:
        if self.client.is_alive:
            print("Reusing the existing connection.")
        else:
            print(f"Connecting to {self.settings.target} ...")
            self.loop.run(self.client.connect())
            print("Connected successfully")
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
                if not console.dispatch(self.loop, self.client, self.settings,
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
