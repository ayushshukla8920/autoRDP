"""Terminal rendering: colour, spinners, progress bars, tables.

Hand-rolled rather than pulled from ``rich`` or ``click`` on purpose. Every
dependency added here is also a dependency PyInstaller has to bundle into the
single-file binary that ships on npm, and the whole of what this module does
fits in a few hundred lines of ANSI escapes. The trade is deliberate: a couple
of megabytes off every platform package, against not having ``rich``'s tables.

Three rules the rest of the CLI relies on:

1. **Narration goes to one stream, data goes to another.** Progress, steps and
   warnings are narration. ``--json`` output is data. In JSON mode narration is
   redirected to stderr so that ``autordp doctor --json | jq`` works.
2. **Nothing here is required for correctness.** Every animation degrades to a
   plain line when the stream is not a terminal, so piping to a file or running
   under cron produces a readable log rather than a smear of carriage returns.
3. **Detection happens once**, at import, and can be overridden afterwards by
   :func:`configure` when the command line has been parsed.
"""

from __future__ import annotations

import io
import itertools
import os
import shutil
import sys
import threading
import time
from contextlib import contextmanager

# --------------------------------------------------------------- capabilities

# SGR codes, by role rather than by colour, so a theme change is one edit.
_SGR = {
    "reset": "0",
    "bold": "1",
    "dim": "2",
    "italic": "3",
    "underline": "4",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "grey": "90",
    "bright_red": "91",
    "bright_green": "92",
    "bright_yellow": "93",
    "bright_cyan": "96",
    "white": "97",
}

# Two glyph sets. The ASCII one is not a lowest-common-denominator afterthought:
# a Windows console still running code page 437 raises UnicodeEncodeError on a
# box-drawing character, which would crash the program in the middle of a run
# for the sake of a prettier bullet.
_GLYPHS_UNICODE = {
    "tick": "✔", "cross": "✘", "warn": "⚠", "info": "•",
    "arrow": "›", "bullet": "•", "rule": "─",
    "bar_full": "█", "bar_empty": "░",
    "bar_left": "▕", "bar_right": "▏",
    "corner_tl": "┌", "corner_tr": "┐",
    "corner_bl": "└", "corner_br": "┘",
    "line_h": "─", "line_v": "│",
    "spinner": "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏",
}
_GLYPHS_ASCII = {
    "tick": "+", "cross": "x", "warn": "!", "info": "-",
    "arrow": ">", "bullet": "*", "rule": "-",
    "bar_full": "#", "bar_empty": ".",
    "bar_left": "[", "bar_right": "]",
    "corner_tl": "+", "corner_tr": "+",
    "corner_bl": "+", "corner_br": "+",
    "line_h": "-", "line_v": "|",
    "spinner": "|/-\\",
}


class _Terminal:
    """What this particular terminal can do, and where output should go."""

    def __init__(self) -> None:
        self.color = False
        self.unicode = False
        self.animate = False
        self.quiet = False
        self.verbose = 0
        self.glyphs = _GLYPHS_ASCII
        self.narration = sys.stdout
        self.data = sys.stdout
        self._detect()

    # -- detection ---------------------------------------------------------

    def _detect(self) -> None:
        stream = sys.stdout
        tty = _is_tty(stream)
        self.animate = tty and os.environ.get("TERM") != "dumb"
        self.color = self._detect_color(tty)
        self.unicode = _can_encode(stream, "".join(_GLYPHS_UNICODE.values()))
        self.glyphs = _GLYPHS_UNICODE if self.unicode else _GLYPHS_ASCII

    @staticmethod
    def _detect_color(tty: bool) -> bool:
        # The informal standards, applied in the order they specify:
        # https://no-color.org and https://force-color.org
        if os.environ.get("NO_COLOR"):
            return False
        if os.environ.get("FORCE_COLOR") or os.environ.get("CLICOLOR_FORCE"):
            return True
        if os.environ.get("TERM") == "dumb" or not tty:
            return False
        # A modern Windows console understands ANSI, but only once the mode bit
        # is set; older ones never will. _enable_windows_vt reports which.
        if sys.platform == "win32":
            return _enable_windows_vt()
        return True

    # -- overrides ---------------------------------------------------------

    def configure(self, color: str = "auto", quiet: bool = False,
                  verbose: int = 0, json_mode: bool = False,
                  ascii_only: bool = False) -> None:
        """Apply the parsed command line over the detected defaults."""
        if color == "always":
            self.color = True
            if sys.platform == "win32":
                _enable_windows_vt()
        elif color == "never":
            self.color = False
        if ascii_only:
            self.unicode = False
        self.glyphs = _GLYPHS_UNICODE if self.unicode else _GLYPHS_ASCII
        self.quiet = quiet
        self.verbose = verbose
        # Data on stdout, narration out of its way. Without this a progress bar
        # would end up inside the JSON document the caller is trying to parse.
        self.narration = sys.stderr if json_mode else sys.stdout
        if quiet:
            self.animate = False

    @property
    def width(self) -> int:
        """Usable columns, clamped to something a bar can be drawn in."""
        try:
            columns = shutil.get_terminal_size(fallback=(80, 24)).columns
        except (OSError, ValueError):
            columns = 80
        return max(40, min(columns, 120))


def _is_tty(stream) -> bool:
    try:
        return bool(stream) and stream.isatty()
    except (AttributeError, ValueError):
        return False


def _can_encode(stream, text: str) -> bool:
    """Whether ``text`` survives this stream's encoder."""
    encoding = getattr(stream, "encoding", None) or "ascii"
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def _enable_windows_vt() -> bool:
    """Turn on ANSI escape handling for the Windows console.

    Windows 10 1511 and later support ANSI, but the console starts with the
    flag off for backward compatibility, so escapes would otherwise be printed
    literally as an unreadable run of bracket codes. Returns whether colour is
    usable afterwards.
    """
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        if handle in (0, -1):
            return False
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            # Not a console at all -- a pipe or a redirect. Escapes would be
            # written verbatim into the file, so say no.
            return False
        enable_vt = 0x0004  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        if mode.value & enable_vt:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | enable_vt))
    except Exception:  # noqa: BLE001 - colour is never worth an exception
        return False


TERM = _Terminal()


def configure(**kwargs) -> None:
    TERM.configure(**kwargs)


def glyph(name: str) -> str:
    return TERM.glyphs[name]


# ------------------------------------------------------------------ styling

def style(text: str, *names: str) -> str:
    """Wrap ``text`` in SGR codes, or return it unchanged without colour."""
    if not TERM.color or not names:
        return text
    codes = ";".join(_SGR[n] for n in names if n in _SGR)
    return f"\033[{codes}m{text}\033[0m" if codes else text


def visible_length(text: str) -> int:
    """Length ignoring SGR sequences, for alignment and truncation."""
    length, in_escape = 0, False
    for char in text:
        if in_escape:
            in_escape = char != "m"
        elif char == "\033":
            in_escape = True
        else:
            length += 1
    return length


def truncate(text: str, limit: int) -> str:
    """Shorten to ``limit`` columns with a leading ellipsis.

    Paths are truncated from the left because the interesting part of
    ``src/very/deep/module.py`` is at the end.
    """
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return "..." + text[-(limit - 3):]


# ------------------------------------------------------------------- output

def _write(stream, text: str) -> None:
    """Write, tolerating a closed stream and an encoder that cannot cope.

    A CLI that raises from its own logging is worse than one that drops a
    glyph, and both failures happen in the wild: stdout is closed when the
    reader of a pipe exits early, and a legacy code page rejects box drawing.
    """
    try:
        stream.write(text)
        stream.flush()
    except UnicodeEncodeError:
        encoding = getattr(stream, "encoding", None) or "ascii"
        try:
            stream.write(text.encode(encoding, "replace").decode(encoding))
            stream.flush()
        except (OSError, ValueError, UnicodeError):
            pass
    except (OSError, ValueError):
        pass


# The bar currently drawn on the bottom line, if any. Narration printed while a
# bar is live has to erase it, print above it, and redraw -- otherwise the two
# fight over the same line and leave a smear of half-overwritten text.
_ACTIVE_PROGRESS = None


def say(text: str = "") -> None:
    """A line of narration. Silenced by ``--quiet``."""
    if TERM.quiet:
        return
    active = _ACTIVE_PROGRESS
    if active is not None and TERM.animate:
        active.log(text)
    else:
        _write(TERM.narration, text + "\n")


def emit(text: str = "") -> None:
    """A line of data. Always printed, even under ``--quiet``."""
    _write(TERM.data, text + "\n")


def detail(text: str) -> None:
    """Narration shown only with ``-v``."""
    if TERM.verbose > 0:
        say(style("      " + text, "grey"))


def title(text: str, subtitle: str = "") -> None:
    say()
    say(style(text, "bold", "bright_cyan")
        + (style("  " + subtitle, "grey") if subtitle else ""))
    say(style(glyph("rule") * min(TERM.width, max(len(text) + 12, 40)), "grey"))


def heading(text: str) -> None:
    say()
    say(style(text, "bold"))


def rule() -> None:
    say(style(glyph("rule") * TERM.width, "grey"))


def step(number: int, total: int, text: str) -> None:
    """A numbered stage of a long operation."""
    say(f"{style('[' + str(number) + '/' + str(total) + ']', 'bold', 'cyan')} {text}")


def ok(text: str) -> None:
    say(f"  {style(glyph('tick'), 'green')} {text}")


def fail(text: str) -> None:
    say(f"  {style(glyph('cross'), 'red')} {text}")


def warn(text: str) -> None:
    say(f"  {style(glyph('warn'), 'yellow')} {text}")


def info(text: str) -> None:
    say(f"  {style(glyph('info'), 'grey')} {text}")


def note(text: str) -> None:
    say(style("    " + text, "grey"))


def bullet(text: str, indent: int = 2) -> None:
    say(" " * indent + style(glyph("bullet"), "grey") + " " + text)


def error(text: str, hint: str = "") -> None:
    """A fatal problem. Always shown, and always on stderr."""
    _write(sys.stderr, "\n" + style(" ERROR ", "bold", "red") + " " + text + "\n")
    if hint:
        _write(sys.stderr, style("        " + hint, "grey") + "\n")


def kv(label: str, value: str, width: int = 18, value_style: str = "") -> None:
    """One aligned ``label : value`` row."""
    shown = style(value, value_style) if value_style else value
    say(f"  {style(label.ljust(width), 'grey')} {shown}")


def table(rows: list[tuple[str, ...]], headers: tuple[str, ...] | None = None,
          indent: int = 2) -> None:
    """A minimal left-aligned table, sized to its contents."""
    if not rows:
        return
    columns = max(len(r) for r in rows)
    padded = [tuple(list(r) + [""] * (columns - len(r))) for r in rows]
    measured = padded + ([headers] if headers else [])
    widths = [
        max(visible_length(str(row[index])) for row in measured if len(row) > index)
        for index in range(columns)
    ]
    available = TERM.width - indent - 2 * (columns - 1)
    if sum(widths) > available:  # give the slack to the widest column
        widest = widths.index(max(widths))
        widths[widest] = max(8, widths[widest] - (sum(widths) - available))

    def line(cells, cell_style: str = "") -> str:
        parts = []
        for index, cell in enumerate(cells):
            text = truncate(str(cell), widths[index])
            pad = " " * max(0, widths[index] - visible_length(text))
            parts.append((style(text, cell_style) if cell_style else text) + pad)
        return " " * indent + "  ".join(parts).rstrip()

    if headers:
        say(line(headers, "bold"))
        say(" " * indent + style("  ".join(glyph("rule") * w for w in widths), "grey"))
    for row in padded:
        say(line(row))


def box(lines: list[str], heading_text: str = "") -> None:
    """A framed block, used for the one summary a run leaves behind."""
    g = glyph
    inner = max([visible_length(text) for text in lines] + [len(heading_text) + 2, 20])
    inner = min(inner, TERM.width - 4)
    top = g("corner_tl") + g("line_h") * (inner + 2) + g("corner_tr")
    if heading_text:
        label = f" {heading_text} "
        top = (g("corner_tl") + g("line_h") + label
               + g("line_h") * max(0, inner + 1 - len(label)) + g("corner_tr"))
    say(style(top, "grey"))
    for text in lines:
        pad = " " * max(0, inner - visible_length(text))
        say(style(g("line_v"), "grey") + " " + text + pad + " "
            + style(g("line_v"), "grey"))
    say(style(g("corner_bl") + g("line_h") * (inner + 2) + g("corner_br"), "grey"))


# ------------------------------------------------------------------ spinner

class Spinner:
    """An indeterminate progress indicator for a step of unknown length.

    Falls back to printing the label once when the stream is not a terminal,
    which is what makes the same code readable in a cron log.
    """

    def __init__(self, label: str) -> None:
        self.label = label
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def __enter__(self) -> "Spinner":
        if not TERM.animate or TERM.quiet:
            say(f"  {style(glyph('arrow'), 'cyan')} {self.label}")
            return self
        _write(TERM.narration, "\033[?25l")  # hide the cursor
        self._thread = threading.Thread(target=self._spin, daemon=True,
                                        name="rdp-spinner")
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            with self._lock:
                _write(TERM.narration, "\r\033[2K\033[?25h")
        if exc_type is None:
            ok(self.label)
        else:
            fail(self.label)
        return False

    def update(self, label: str) -> None:
        self.label = label
        if not TERM.animate:
            detail(label)

    def _spin(self) -> None:
        frames = itertools.cycle(glyph("spinner"))
        while not self._stop.is_set():
            with self._lock:
                frame = style(next(frames), "cyan")
                text = truncate(self.label, TERM.width - 6)
                _write(TERM.narration, f"\r\033[2K  {frame} {text}")
            self._stop.wait(0.08)


@contextmanager
def spinner(label: str):
    with Spinner(label) as active:
        yield active


# ----------------------------------------------------------------- progress

class Progress:
    """A determinate bar with a rate and an ETA.

    Sized in *characters typed* rather than files, because file sizes in a
    repository vary by two orders of magnitude and a per-file bar would sit at
    3/40 for twenty minutes and then jump. Characters are what actually costs
    time, so a character-weighted bar moves smoothly and its ETA is worth
    reading.
    """

    def __init__(self, total: int, label: str = "", unit: str = "chars") -> None:
        self.total = max(1, total)
        self.label = label
        self.unit = unit
        self.done = 0
        self.status = ""
        self.started = time.monotonic()
        self._last_render = 0.0
        self._decile = -1
        self._lock = threading.Lock()
        self._finished = False

    def __enter__(self) -> "Progress":
        global _ACTIVE_PROGRESS
        _ACTIVE_PROGRESS = self
        if TERM.animate and not TERM.quiet:
            _write(TERM.narration, "\033[?25l")  # hide the cursor
        return self

    def __exit__(self, exc_type, exc, traceback) -> bool:
        self.finish()
        return False

    def log(self, text: str) -> None:
        """Print a line *above* the bar, then put the bar back.

        Called by :func:`say` while this bar is the active one, so ordinary
        narration keeps working during a forty-minute typing run.
        """
        with self._lock:
            _write(TERM.narration, "\r\033[2K" + text + "\n")
            if not self._finished:
                self._render(force=True)

    def advance(self, amount: int, status: str = "") -> None:
        with self._lock:
            self.done = min(self.total, self.done + amount)
            if status:
                self.status = status
            self._render()

    def set_status(self, status: str) -> None:
        with self._lock:
            self.status = status
            self._render()

    def finish(self, summary: str = "") -> None:
        global _ACTIVE_PROGRESS
        with self._lock:
            already = self._finished
            self._finished = True
            if _ACTIVE_PROGRESS is self:
                _ACTIVE_PROGRESS = None
            if not already and TERM.animate and not TERM.quiet:
                _write(TERM.narration, "\r\033[2K\033[?25h")
        if summary and not already:
            ok(summary)

    # -- rendering ---------------------------------------------------------

    def _render(self, force: bool = False) -> None:
        if TERM.quiet:
            return
        now = time.monotonic()
        if not TERM.animate:
            # Not a terminal: emit a line every 10% instead of redrawing, so a
            # log file gets progress without a megabyte of escape codes.
            decile = int(self.done * 10 / self.total)
            if force or decile > self._decile:
                self._decile = decile
                say(f"      {decile * 10:3d}%  {self.status}")
            return
        if not force and now - self._last_render < 0.1:
            return
        self._last_render = now
        _write(TERM.narration, "\r\033[2K  " + self._line())

    def _line(self) -> str:
        fraction = self.done / self.total
        elapsed = max(1e-6, time.monotonic() - self.started)
        rate = self.done / elapsed
        eta = (self.total - self.done) / rate if rate > 0 else 0.0

        right = (f"{fraction * 100:3.0f}%  {_compact(self.done)}/"
                 f"{_compact(self.total)}  {_compact(rate)}/s  "
                 f"ETA {_duration(eta)}")
        # Everything but the bar is fixed width; the bar takes what is left.
        prefix = f"{self.label} " if self.label else ""
        spare = TERM.width - visible_length(prefix) - len(right) - 8
        bar_width = max(10, min(30, spare))
        filled = int(bar_width * fraction)
        bar = (style(glyph("bar_full") * filled, "cyan")
               + style(glyph("bar_empty") * (bar_width - filled), "grey"))
        line = (style(prefix, "bold") + style(glyph("bar_left"), "grey") + bar
                + style(glyph("bar_right"), "grey") + "  " + style(right, "grey"))

        room = TERM.width - visible_length(line) - 4
        if self.status and room > 12:
            line += "  " + style(truncate(self.status, room), "grey")
        return line


def _compact(value: float) -> str:
    """1234 -> 1.2k. Keeps the bar's right-hand block a stable width."""
    if value < 1000:
        return f"{value:.0f}"
    if value < 1_000_000:
        return f"{value / 1000:.1f}k"
    return f"{value / 1_000_000:.1f}M"


def _duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"
    return f"{int(seconds // 3600)}h{int((seconds % 3600) // 60):02d}m"


def duration(seconds: float) -> str:
    """Public spelling of the duration formatter, for summaries."""
    return _duration(seconds)


# -------------------------------------------------------------------- input

def prompt(label: str, default: str = "", required: bool = False,
           choices: tuple[str, ...] = ()) -> str:
    """Ask for one value, showing any default in brackets.

    Raises ``EOFError`` when there is no one to answer and no default, which
    the caller turns into an actionable "pass --host" message rather than a
    traceback.
    """
    hint = ""
    if choices:
        hint += style(" (" + "/".join(choices) + ")", "grey")
    if default:
        hint += style(f" [{default}]", "grey")
    while True:
        _write(TERM.narration, style(glyph("arrow"), "cyan") + " " + label + hint + ": ")
        try:
            value = input().strip()
        except EOFError:
            _write(TERM.narration, "\n")
            if default or not required:
                return default
            raise
        value = value or default
        if choices and value and value not in choices:
            fail(f"pick one of: {', '.join(choices)}")
            continue
        if value or not required:
            return value
        fail("required")


def confirm(label: str, default: bool = True) -> bool:
    answer = prompt(label, "y" if default else "n").lower()
    return answer[:1] in ("y", "t", "1")


def is_interactive() -> bool:
    """Whether there is a human on the other end of stdin."""
    return _is_tty(sys.stdin) and _is_tty(sys.stdout)


class Capture(io.TextIOBase):
    """Redirect ``print`` from the session layer into a callback.

    ``session.py`` narrates with ``print`` so that its output reads correctly
    in a log file or a cron mail, where a redrawing progress bar would be
    unreadable. Rather than fork that into two narrations, the CLI intercepts
    the same stream and repaints each line for a terminal.
    """

    def __init__(self, sink) -> None:
        self._sink = sink
        self._buffer = ""

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            line, _, self._buffer = self._buffer.partition("\n")
            self._sink(line)
        return len(text)

    def flush(self) -> None:
        if self._buffer:
            self._sink(self._buffer)
            self._buffer = ""

    def isatty(self) -> bool:
        return False
