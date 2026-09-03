"""Phase 1: connect to a Windows server over RDP and drive it from a prompt.

    python main.py

The asyncio event loop runs on a worker thread and the command prompt stays on
the main thread. That split matters on Windows: ``input()`` on the main thread
raises ``KeyboardInterrupt`` on Ctrl+C, so a long ``type`` can be interrupted
without tearing down the RDP session.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import shlex
import sys
import threading
import time
from pathlib import Path

import credentials
from config import ConfigError, Settings, setup_logging
from input_events import EmergencyStopped, InputError, SessionNotAcceptingInput
from rdp_client import ConnectionFailed, RdpClient

HELP = """
Commands
  type <text>              type text into the focused remote window
  key <key>                press a key or chord, e.g. ENTER, F5, ctrl+s
  click <x> <y> [button]   click at remote coordinates (left/right/middle)
  dclick <x> <y>           double-click at remote coordinates
  move <x> <y>             move the remote pointer
  scroll <x> <y> [up|down] [notches]
  wait <seconds>           pause locally
  screenshot [path]        save the remote screen as a PNG
  status                   connection and emergency-stop state
  keys                     list the key names that `key` understands
  stop                     trip the emergency stop
  resume                   clear the emergency stop
  help                     this text
  quit                     disconnect and exit
""".strip()


class LoopThread:
    """An asyncio event loop living on its own thread."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="rdp-loop", daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def start(self) -> None:
        self._thread.start()

    def submit(self, coro) -> concurrent.futures.Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def run(self, coro, on_interrupt=None):
        """Run a coroutine to completion, staying responsive to Ctrl+C.

        Polling the future in short slices is what lets a Ctrl+C be delivered to
        this thread; ``on_interrupt`` then asks the coroutine to wind itself down
        rather than being abandoned mid-keystroke.
        """
        future = self.submit(coro)
        interrupted = False
        while True:
            try:
                return future.result(timeout=0.2)
            except concurrent.futures.TimeoutError:
                continue
            except KeyboardInterrupt:
                if interrupted:
                    raise
                interrupted = True
                print("\n^C - stopping the current command...")
                if on_interrupt is not None:
                    on_interrupt()
                try:
                    future.result(timeout=5)
                except Exception:  # noqa: BLE001 - already unwinding
                    pass
                raise

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        self.loop.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Connect to a Windows server over RDP and drive it from a prompt.")
    parser.add_argument("--gui", action="store_true",
                        help="collect the connection details in a tkinter form")
    parser.add_argument("--remember", action="store_true",
                        help="also remember the password (encrypted for this Windows user)")
    parser.add_argument("--forget", action="store_true",
                        help="delete the remembered connection details and exit")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.forget:
        path = credentials.profile_path()
        print(f"Removed {path}" if credentials.forget() else f"Nothing saved at {path}")
        return 0

    print("rdp-background-automation - Phase 1 connection test")
    print("-" * 52)
    try:
        if args.gui:
            from gui import settings_from_gui
            settings = settings_from_gui()
            if settings is None:
                print("Cancelled.")
                return 130
        else:
            settings = Settings.load(interactive=True)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.")
        return 130

    setup_logging(settings)
    client = RdpClient(settings)

    if settings.stop_file.exists():
        print(f"Emergency stop file {settings.stop_file} exists - removing it before start.")
        client.stop.reset()

    loop = LoopThread()
    loop.start()
    try:
        try:
            loop.run(client.connect())
        except KeyboardInterrupt:
            print("Cancelled while connecting.")
            return 130
        except ConnectionFailed as exc:
            print(f"\nConnection failed: {exc}", file=sys.stderr)
            return 1

        print("\nConnected successfully")

        # Only remember details that actually worked.
        if settings.remember(remember_password=args.remember):
            note = "including the password" if args.remember else "password not saved"
            print(f"Remembered for next time ({note})")

        print(f"Remote screen: {settings.width}x{settings.height}")
        print(f"Emergency stop: create {settings.stop_file} (or press Ctrl+C)")
        print("Type 'help' for commands.\n")

        return repl(loop, client, settings)
    finally:
        try:
            loop.run(client.disconnect())
        except Exception as exc:  # noqa: BLE001
            print(f"Warning: unclean disconnect: {exc}", file=sys.stderr)
        loop.close()


def repl(loop: LoopThread, client: RdpClient, settings: Settings) -> int:
    """Read commands until the user quits or the session dies."""
    while True:
        if not client.is_alive:
            print("\nThe RDP session has disconnected. Exiting.", file=sys.stderr)
            return 1
        try:
            line = input("rdp> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nQuitting.")
            return 0

        if not line:
            continue

        command, _, argument = line.partition(" ")
        command = command.lower()
        argument = argument.strip()

        if command in ("quit", "exit"):
            print("Disconnecting...")
            return 0

        try:
            if not dispatch(loop, client, settings, command, argument, line):
                print(f"Unknown command {command!r}. Type 'help' for the list.")
        except KeyboardInterrupt:
            # The command was aborted; keep the session and clear the switch.
            if settings.stop_file.exists():
                print(f"Stop file {settings.stop_file} is present - use 'resume' to clear it.")
            else:
                client.stop.reset()
                print("Command stopped. Session still connected.")
        except EmergencyStopped as exc:
            print(f"Stopped: {exc}")
        except SessionNotAcceptingInput as exc:
            print(f"Cannot send input: {exc}", file=sys.stderr)
        except InputError as exc:
            print(f"Input error: {exc}")
        except Exception as exc:  # noqa: BLE001 - a bad command must not kill the prompt
            print(f"Error: {exc}", file=sys.stderr)


def dispatch(loop: LoopThread, client: RdpClient, settings: Settings,
             command: str, argument: str, raw: str) -> bool:
    """Run one command. Returns False if the command name is unknown."""
    sender = client.input
    trip = lambda: client.stop.trip("Ctrl+C")  # noqa: E731

    if command == "help":
        print(HELP)

    elif command == "keys":
        from input_events import KEY_ALIASES
        names = sorted(KEY_ALIASES)
        for i in range(0, len(names), 6):
            print("  " + "  ".join(f"{n:<12}" for n in names[i:i + 6]))
        print("  Combine with '+', e.g. ctrl+s, ctrl+shift+p, alt+F4")

    elif command == "type":
        if not argument:
            print("Usage: type <text>")
            return True
        # `argument` keeps the text verbatim, including inner spacing.
        text = raw.partition(" ")[2].rstrip("\n")
        client.require_alive()
        started = time.monotonic()
        loop.run(sender.type_text(text), on_interrupt=trip)
        print(f"Typed {len(text)} characters in {time.monotonic() - started:.1f}s")

    elif command == "key":
        if not argument:
            print("Usage: key <key>   (e.g. key ENTER, key ctrl+s)")
            return True
        client.require_alive()
        loop.run(sender.chord(argument), on_interrupt=trip)
        print(f"Pressed {argument}")

    elif command in ("click", "dclick", "move"):
        parts = shlex.split(argument)
        if len(parts) < 2:
            print(f"Usage: {command} <x> <y>" + ("" if command == "move" else " [button]"))
            return True
        x, y = _coords(parts[0], parts[1], settings)
        client.require_alive()
        if command == "move":
            loop.run(sender.move(x, y), on_interrupt=trip)
            print(f"Pointer moved to ({x}, {y})")
        else:
            button = parts[2] if len(parts) > 2 else "left"
            action = sender.double_click if command == "dclick" else sender.click
            loop.run(action(x, y, button), on_interrupt=trip)
            print(f"{'Double-clicked' if command == 'dclick' else 'Clicked'} "
                  f"{button} at ({x}, {y})")

    elif command == "scroll":
        parts = shlex.split(argument)
        if len(parts) < 2:
            print("Usage: scroll <x> <y> [up|down] [notches]")
            return True
        x, y = _coords(parts[0], parts[1], settings)
        direction = (parts[2].lower() if len(parts) > 2 else "up")
        if direction not in ("up", "down"):
            print("Direction must be 'up' or 'down'")
            return True
        notches = int(parts[3]) if len(parts) > 3 else 3
        client.require_alive()
        loop.run(sender.scroll(x, y, notches, up=direction == "up"), on_interrupt=trip)
        print(f"Scrolled {direction} {notches} notches at ({x}, {y})")

    elif command in ("wait", "sleep"):
        try:
            seconds = float(argument)
        except ValueError:
            print("Usage: wait <seconds>")
            return True
        if seconds < 0:
            print("Seconds must not be negative")
            return True
        time.sleep(seconds)
        print(f"Waited {seconds:g}s")

    elif command == "screenshot":
        path = Path(argument) if argument else None
        saved = loop.run(client.screenshot(path))
        # Show a short path when it is under the working directory: absolute
        # Windows paths wrap awkwardly in a narrow console.
        try:
            shown = saved.relative_to(Path.cwd())
        except ValueError:
            shown = saved
        print(f"Screenshot saved to {shown}")

    elif command == "status":
        print(f"  target        : {settings.target}")
        print(f"  connected     : {client.is_alive}")
        print(f"  screen        : {settings.width}x{settings.height}")
        print(f"  type mode     : {settings.type_mode}")
        print(f"  emergency stop: {'TRIPPED' if client.stop.tripped else 'clear'}"
              f" (file: {settings.stop_file})")

    elif command == "stop":
        client.stop.trip("requested from the prompt")
        print("Emergency stop tripped. Input is blocked until you run 'resume'.")

    elif command == "resume":
        client.stop.reset()
        print("Emergency stop cleared.")

    else:
        return False

    return True


def _coords(raw_x: str, raw_y: str, settings: Settings) -> tuple[int, int]:
    """Parse and range-check remote screen coordinates."""
    try:
        x, y = int(raw_x), int(raw_y)
    except ValueError:
        raise InputError(f"Coordinates must be integers, got {raw_x!r} {raw_y!r}") from None
    if not (0 <= x < settings.width and 0 <= y < settings.height):
        raise InputError(
            f"({x}, {y}) is outside the {settings.width}x{settings.height} remote screen")
    return x, y


if __name__ == "__main__":
    raise SystemExit(
        "main.py is part of the application, not an entry point. "
        "Start it from the GUI instead:  python gui.py"
    )
