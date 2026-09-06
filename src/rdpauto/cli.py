"""Console entry point, for servers with no display.

`gui.py` needs an X server, which a headless VPS does not have. This gives the
same five actions from a terminal, with the same remembered profile, and never
imports tkinter.

Two ways to use it:

    python -m rdpauto.cli                     # a menu, prompting for anything missing
    python -m rdpauto.cli demo --editor code  # straight to it, for scripts and cron

Anything already known -- from the environment, or from the remembered profile
-- is offered as a default you accept with Enter, so the interactive path is
usually four keystrokes.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import threading

from rdpauto import codebase, console, credentials, session
from rdpauto.config import ConfigError, Settings, setup_logging
from rdpauto.loop import LoopThread
from rdpauto.input_events import (EmergencyStopped, InputError,
                                  SessionNotAcceptingInput)
from rdpauto import logfmt
from rdpauto.rdp_client import ConnectionFailed, RdpClient


def _enable_ansi() -> bool:
    """Best-effort: turn on ANSI colour in the Windows console. Returns whether
    stdout is a terminal we should colour at all."""
    if not sys.stdout.isatty():
        return False
    if sys.platform == "win32":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            k.SetConsoleMode(k.GetStdHandle(-11), 7)   # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except Exception:  # noqa: BLE001 - colour is a nicety, never fatal
            return False
    return True


class _ColorStream:
    """Line-buffers stdout and colours each finished line via :mod:`logfmt`, so
    the CLI session log gets the same visual hierarchy as the GUI."""

    def __init__(self, real) -> None:
        self._real = real
        self._buf = ""
        self._lock = threading.Lock()   # loop thread and progress ticker both write

    def write(self, text: str) -> int:
        with self._lock:
            self._buf += text
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self._real.write(logfmt.colorize(line) + "\n")
        return len(text)

    def flush(self) -> None:
        with self._lock:
            if self._buf:
                self._real.write(self._buf)
                self._buf = ""
            self._real.flush()

    def isatty(self) -> bool:
        return True


ACTIONS = {
    "1": ("test", "Connection test        - an interactive rdp> prompt"),
    "2": ("demo-notepad", "Editor demo, Notepad   - type a generated Python file"),
    "3": ("demo-code", "Editor demo, VS Code   - the same, in VS Code"),
    "4": ("repo-notepad", "Type a codebase        - clone a git repo, into Notepad"),
    "5": ("repo-code", "Type a codebase        - clone a git repo, into VS Code"),
}

# What `python -m rdpauto.cli <action>` accepts, and which editor it implies.
SHORTHAND = {"test": "test", "demo": "demo-", "repo": "repo-"}


def ask(label: str, default: str = "", required: bool = False) -> str:
    """Prompt with a default shown in brackets; Enter accepts it."""
    suffix = f" [{default}]" if default else ""
    while True:
        try:
            value = input(f"{label}{suffix}: ").strip()
        except EOFError:
            print()
            if default or not required:
                return default
            raise ConfigError(f"{label} is required but stdin is closed") from None
        value = value or default
        if value or not required:
            return value
        print("  -> required")


def ask_password(has_saved: bool) -> str:
    """Read a password without echoing, or keep the one already known."""
    if has_saved:
        typed = getpass.getpass("Password [keep saved, Enter to accept]: ")
        if not typed:
            return ""          # signals "use what is already there"
        return typed
    while True:
        value = getpass.getpass("Password (not echoed): ")
        if value:
            return value
        print("  -> required")


def choose_action() -> str | None:
    """Show the menu and return the chosen action, or None to quit."""
    print("\nWhat would you like to run?")
    for key, (_, description) in ACTIONS.items():
        print(f"  {key}) {description}")
    print("  q) Quit")
    while True:
        choice = input("Choice [1]: ").strip().lower() or "1"
        if choice in ("q", "quit", "exit"):
            return None
        if choice in ACTIONS:
            return ACTIONS[choice][0]
        print("  -> pick 1-5, or q")


def collect_settings(args: argparse.Namespace, interactive: bool) -> Settings:
    """Resolve connection details, prompting only for what is still missing."""
    # If a specific --host was given, load that saved server; else the last-used.
    saved = credentials.load((args.host or "").strip() or None)

    def pick(flag, env, key):
        return (flag or os.environ.get(env, "").strip() or saved.get(key, ""))

    host = pick(args.host, "RDP_HOST", "host")
    username = pick(args.username, "RDP_USERNAME", "username")
    domain = pick(args.domain, "RDP_DOMAIN", "domain")
    port = pick(args.port, "RDP_PORT", "port") or "3389"
    password = os.environ.get("RDP_PASSWORD", "") or saved.get("password", "")

    if interactive:
        if saved:
            print(f"Remembered details from {credentials.profile_path()}")
        host = ask("Host / IP", host, required=True)
        username = ask("Username", username, required=True)
        domain = ask("Domain (blank for none)", domain)
        port = ask("Port", port, required=True)
        typed = ask_password(bool(password))
        password = typed or password
    elif not (host and username and password):
        missing = [n for n, v in (("host", host), ("username", username),
                                  ("password", password)) if not v]
        raise ConfigError(
            "Missing " + ", ".join(missing) +
            ". Pass them as flags, set RDP_HOST/RDP_USERNAME/RDP_PASSWORD, "
            "or run `python -m rdpauto.cli` with no arguments to be prompted.")

    for key, value in (("RDP_HOST", host), ("RDP_USERNAME", username),
                       ("RDP_DOMAIN", domain), ("RDP_PORT", port),
                       ("RDP_PASSWORD", password)):
        os.environ[key] = value
    settings = Settings.load(interactive=False)
    # Don't leave the plaintext password in the environment: it would otherwise
    # be inherited by child processes such as the git clone in codebase.clone.
    os.environ.pop("RDP_PASSWORD", None)
    return settings


def _print_summary(settings: Settings, action: str) -> None:
    """Show the resolved connection details (so auto-filled values are visible)
    before doing anything with them."""
    pw = "******** (saved/entered)" if settings.password else "(none!)"
    tg = "on" if (settings.telegram_token and settings.telegram_chat_id) else "off"
    print("\nUsing these details:")
    print(f"  server    : {settings.host}:{settings.port}")
    print(f"  username  : {settings.username}" +
          (f"   domain: {settings.domain}" if settings.domain else ""))
    print(f"  password  : {pw}")
    print(f"  screen    : {settings.width}x{settings.height}")
    print(f"  telegram  : {tg}")
    print(f"  action    : {action}\n")


def _progress_ticker(progress: "session.Progress", stop: threading.Event,
                     every: float = 8.0) -> None:
    """Print a one-line progress bar every few seconds during a codebase run,
    so the terminal shows it is alive even mid-file. Stops when ``stop`` is set."""
    while not stop.wait(every):
        if not (progress.active and progress.total):
            continue
        pct = int(100 * progress.done / progress.total)
        filled = pct // 5
        bar = "#" * filled + "-" * (20 - filled)
        if progress.phase == "done":
            print(f"  [{bar}] 100%  {progress.total} files typed in "
                  f"{codebase.human_time(progress.elapsed())}", flush=True)
            return
        print(f"  [{bar}] {pct:3d}%  files {progress.done}/{progress.total}  "
              f"now: {progress.current}  "
              f"elapsed {codebase.human_time(progress.elapsed())}  "
              f"left ~{codebase.human_time(progress.eta_seconds())}", flush=True)


def run(action: str, settings: Settings, args: argparse.Namespace) -> int:
    """Connect and perform one action, then disconnect cleanly."""
    client = RdpClient(settings)
    if settings.stop_file.exists():
        print(f"Removing stale stop file {settings.stop_file}")
        client.stop.reset()

    editor = "code" if action.endswith("-code") else "notepad"
    run_args = session.parse_args(["--editor", editor]
                                + (["--remember"] if args.remember else [])
                                + (["--run"] if args.run else [])
                                + (["--no-screenshots"] if not args.screenshots else []))
    if args.seed is not None:
        run_args.seed = args.seed

    _print_summary(settings, action)
    print(f"Emergency stop: create {settings.stop_file}, or press Ctrl+C\n")

    # Colour the session log to match the GUI -- but not for the `test` REPL,
    # whose input() prompts do not play well with a line-buffered stream.
    real_stdout = sys.stdout
    use_color = _enable_ansi() and action != "test"
    if use_color:
        sys.stdout = _ColorStream(real_stdout)

    loop = LoopThread()
    loop.start()
    trip = lambda: client.stop.trip("Ctrl+C")  # noqa: E731
    try:
        if action == "test":
            print(f"Connecting to {settings.target} ...")
            loop.run(client.connect(), on_interrupt=trip)
            print("\nConnected successfully")
            settings.remember(remember_password=args.remember)
            print(f"Remote screen: {settings.width}x{settings.height}")
            print("Type 'help' for commands.\n")
            return console.repl(loop, client, settings)

        if action.startswith("repo"):
            progress = session.Progress()
            stop_ticker = threading.Event()
            ticker = threading.Thread(target=_progress_ticker,
                                      args=(progress, stop_ticker), daemon=True)
            ticker.start()
            try:
                loop.run(session.run_codebase_session(
                    client, run_args, args.repo, args.minutes * 60.0, progress),
                    on_interrupt=trip)
            finally:
                stop_ticker.set()
        else:
            loop.run(session.run_session(client, run_args), on_interrupt=trip)
        print("\nFinished.")
        return 0

    except KeyboardInterrupt:
        print("\nInterrupted. Disconnecting.", file=sys.stderr)
        return 130
    except codebase.CodebaseError as exc:
        print(f"\nRepository problem: {exc}", file=sys.stderr)
        return 6
    except ConnectionFailed as exc:
        print(f"\nConnection failed: {exc}", file=sys.stderr)
        return 1
    except EmergencyStopped as exc:
        print(f"\nEmergency stop: {exc}", file=sys.stderr)
        return 3
    except SessionNotAcceptingInput as exc:
        print(f"\nThe session stopped accepting input: {exc}", file=sys.stderr)
        return 4
    except InputError as exc:
        print(f"\nInput error: {exc}", file=sys.stderr)
        return 5
    finally:
        try:
            loop.run(client.disconnect())
        except Exception as exc:  # noqa: BLE001
            print(f"Warning: unclean disconnect: {exc}", file=sys.stderr)
        loop.close()
        if use_color:
            sys.stdout.flush()
            sys.stdout = real_stdout


_DESCRIPTION = """\
autordp - headless RDP automation.

Opens its OWN Remote Desktop connection to a Windows server and drives the
remote desktop by sending RDP keyboard/mouse events. Your local machine is
never touched. Runs fully in a terminal (no display needed), so it works on a
headless server. Use the GUI (python -m rdpauto.gui) where a display exists.
"""

_EPILOG = """\
ACTIONS (positional)
  (none)            show an interactive menu, prompting for anything missing
  test              connect, then drop into the interactive rdp> prompt below
  demo              type a generated Python file into the remote editor
  repo <url>        clone a git repo and type its files into the remote editor

COMMON OPTIONS
  --editor notepad|code     which remote editor to drive (default notepad)
  --minutes N               time budget for 'repo'; 0 = no limit (default 10)
  --run                     run the demo file after saving it
  --seed N                  reproducible demo file
  --no-screenshots          skip the progress screenshots
  --host/--username/--domain/--port   override connection details
  --remember                also remember the password (Windows only, DPAPI)
  --forget                  delete the remembered connection details and exit

INTERACTIVE rdp> COMMANDS (in the 'test' action)
{repl}

DESKTOP GUI (where a display exists)
  autordp --gui             open the connection form + runner window
  autordp --gui --stop      just the floating emergency-STOP button

CONFIGURATION (environment, or a .env file next to the binary)
  RDP_HOST RDP_USERNAME RDP_PASSWORD RDP_DOMAIN RDP_PORT   connection
  RDP_AUTH=ntlm|kerberos|plain        RDP_WIDTH RDP_HEIGHT
  RDP_RECONNECT_ATTEMPTS RDP_STALL_TIMEOUT    resilience
  RDP_TELEGRAM_TOKEN RDP_TELEGRAM_CHAT_ID     alert if reconnect fails
  (on Linux the password is not remembered - set RDP_PASSWORD or use .env)

EMERGENCY STOP
  Press Ctrl+C to abort, or create a file named STOP next to the binary.
  In the rdp> prompt: 'stop' pauses (session kept), 'resume' continues.

EXAMPLES
  autordp                                       # menu, prompts for anything missing
  autordp test                                  # interactive rdp> prompt
  autordp demo --editor code --run
  autordp repo https://github.com/pallets/click --minutes 30
  autordp repo https://github.com/me/proj --minutes 0        # whole repo, no limit
  autordp --forget
""".format(repl="\n".join("  " + line for line in console.HELP.splitlines()
                          if line.strip() and not line.startswith("Commands")))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="autordp",
        description=_DESCRIPTION,
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter)

    parser.add_argument("action", nargs="?", choices=sorted(SHORTHAND),
                        help="test, demo or repo. Omit for a menu.")
    parser.add_argument("repo", nargs="?", help="repository URL, for the repo action")

    parser.add_argument("--editor", choices=("notepad", "code"), default="notepad",
                        help="which remote editor to drive (default notepad)")
    parser.add_argument("--minutes", type=float, default=10.0,
                        help="time budget for a codebase run; 0 means no limit "
                             "(default 10)")
    parser.add_argument("--seed", type=int, help="seed for the generated demo file")
    parser.add_argument("--run", action="store_true",
                        help="execute the demo file after saving it")
    parser.add_argument("--no-screenshots", dest="screenshots", action="store_false",
                        help="skip the progress screenshots")

    parser.add_argument("--host"), parser.add_argument("--username")
    parser.add_argument("--domain"), parser.add_argument("--port")
    parser.add_argument("--remember", action="store_true",
                        help="also remember the password (Windows only; uses DPAPI)")
    parser.add_argument("--forget", action="store_true",
                        help="delete the remembered connection details and exit")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.forget:
        path = credentials.profile_path()
        print(f"Removed {path}" if credentials.forget() else f"Nothing saved at {path}")
        return 0

    print("rdp-background-automation")
    print("-" * 52)

    # An action on the command line means "do not prompt unless something is
    # genuinely missing" -- that is what makes this usable from cron.
    interactive = args.action is None
    try:
        settings = collect_settings(args, interactive=interactive)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        return 130

    setup_logging(settings)

    if args.action is None:
        action = choose_action()
        if action is None:
            print("Cancelled.")
            return 130
    elif args.action == "test":
        action = "test"
    else:
        action = SHORTHAND[args.action] + args.editor

    if action.startswith("repo"):
        if not args.repo:
            if not interactive:
                print("A repository URL is required: "
                      "python -m rdpauto.cli repo <url>", file=sys.stderr)
                return 2
            args.repo = ask("Repository URL", required=True)
            args.minutes = float(ask("Minutes to spend (0 = no limit)", "10") or 10)

    try:
        return run(action, settings, args)
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
