"""Shared log-line classification for the CLI (ANSI) and GUI (Text tags).

Both front ends print the same session output; this is the one place that
decides what each line *is* (a step header, a file header, success, a warning,
an error, or dim detail) so the two stay in visual sync.
"""

from __future__ import annotations

import re

_FILE = re.compile(r"^\[\d+/\d+\]")     # [37/75] app/login/page.js
_STEP = re.compile(r"^\[\d+\]\s")       # [4] Connecting to ...
_ERR_STARTS = ("Error", "Cannot", "Warning", "Unexpected", "Traceback",
               "Input error", "Stopped", "Repository problem", "Connection failed",
               "The session", "Emergency stop")
_OK_MARKS = ("Connected successfully", "reconnected on", "typed in ", "Typed ",
             "desktop has settled", "Demo finished", "Finished.", "all files typed",
             "Done")

# tag -> ANSI colour, for the CLI on a real terminal.
ANSI = {"err": "\033[91m", "warn": "\033[93m", "file": "\033[93;1m",
        "step": "\033[96;1m", "ok": "\033[92m", "dim": "\033[90m"}
RESET = "\033[0m"


def classify(line: str) -> str | None:
    """One of step|file|ok|warn|err|dim, or None for plain text."""
    s = line.strip()
    if not s:
        return None
    if s.startswith(_ERR_STARTS) or "could not be recovered" in s:
        return "err"
    if _FILE.match(s):
        return "file"
    if _STEP.match(s):
        return "step"
    # Success is checked before warnings so 'reconnected on attempt N' (a
    # recovery, not a problem) is not caught by the 'reconnect' warn test.
    if any(m in s for m in _OK_MARKS):
        return "ok"
    if s.startswith("!") or "reconnect" in s.lower() or "gave up waiting" in s.lower():
        return "warn"
    if any(t in s for t in (" INFO ", " DEBUG ", " WARNING ")) or line.startswith("  "):
        return "dim"
    return None


def colorize(line: str) -> str:
    """Wrap a line in its ANSI colour for terminal output; plain if uncoloured."""
    tag = classify(line)
    return f"{ANSI[tag]}{line}{RESET}" if tag in ANSI else line
