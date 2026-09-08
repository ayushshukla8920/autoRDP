"""The commands available at the ``rdp>`` prompt.

One function, :func:`dispatch`, runs one command. It is kept separate from the
prompt that reads them so that the same commands work three ways with no second
implementation: typed interactively, passed with ``autordp connect -c``, or read
from a file with ``--script``.

Coordinates are checked against the remote screen size before anything is sent.
A click at (5000, 5000) on a 1280x800 desktop is silently clamped by the
protocol, so without this it would land in a corner rather than report a
mistake.
"""

from __future__ import annotations

import shlex
import time
from pathlib import Path

from .config import Settings
from .client import RdpClient
from .input import InputError
from .loop import LoopThread

# Shown by the CLI's `help`. Kept here, next to the implementations, so a
# command cannot be added without the help text being right there to update.
COMMANDS = [
    ("type <text>", "type text into the focused remote window"),
    ("key <key>", "press a key or chord, e.g. ENTER, F5, ctrl+s"),
    ("click <x> <y> [button]", "click at remote coordinates"),
    ("dclick <x> <y>", "double-click at remote coordinates"),
    ("move <x> <y>", "move the remote pointer"),
    ("scroll <x> <y> [up|down] [n]", "scroll at remote coordinates"),
    ("wait <seconds>", "pause locally"),
    ("screenshot [path]", "save the remote screen as a PNG"),
    ("status", "connection and emergency-stop state"),
    ("keys", "list the key names that `key` understands"),
    ("stop", "trip the emergency stop"),
    ("resume", "clear the emergency stop"),
    ("help", "this text"),
    ("quit", "disconnect and exit"),
]


def dispatch(loop: LoopThread, client: RdpClient, settings: Settings,
             command: str, argument: str, raw: str) -> bool:
    """Run one command. Returns False if the command name is unknown."""
    sender = client.input
    trip = lambda: client.stop.trip("Ctrl+C")  # noqa: E731

    if command == "keys":
        from .input import KEY_ALIASES

        names = sorted(KEY_ALIASES)
        for start in range(0, len(names), 6):
            print("  " + "  ".join(f"{n:<12}" for n in names[start:start + 6]))
        print("  Combine with '+', e.g. ctrl+s, ctrl+shift+p, alt+F4")

    elif command == "type":
        if not argument:
            print("Usage: type <text>")
            return True
        # Taken from `raw`, not `argument`: the text must keep its inner
        # spacing exactly, and argument has been stripped.
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
            print(f"Usage: {command} <x> <y>"
                  + ("" if command == "move" else " [button]"))
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
        loop.run(sender.scroll(x, y, notches, up=direction == "up"),
                 on_interrupt=trip)
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
        raise InputError(
            f"Coordinates must be integers, got {raw_x!r} {raw_y!r}") from None
    if not (0 <= x < settings.width and 0 <= y < settings.height):
        raise InputError(f"({x}, {y}) is outside the "
                         f"{settings.width}x{settings.height} remote screen")
    return x, y
