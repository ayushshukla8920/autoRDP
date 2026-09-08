"""Clone a git repository and type its files into the remote editor.

The honest headline first: **typing is slow**. Every character is an RDP input
event, and the remote UI needs time to keep up, so throughput is roughly 20-25
characters per second. That is about 1 KB per minute:

    a 30 KB repository  ~ 30 minutes
    a 1 MB repository   ~ 17 hours

So this does not "clone a repo onto the server" -- for that you would run git
there. What it does is drive a real editor through a real repository's files,
which is what makes it useful as a GUI-automation exercise -- it drives a
real editor through a real repository. To keep that
honest, the work is bounded by a **time budget**: files are selected in path
order until the projected time runs out, and everything skipped is reported.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("autordp.codebase")

# Directories that are never worth typing.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", "dist", "build", "target", ".idea", ".vscode", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "vendor", "third_party", ".next", ".nuxt",
    "coverage", "htmlcov", ".tox", "site-packages", ".gradle", "Pods",
}

# Extensions worth typing into an editor. Anything else is skipped: this is a
# typing exercise, not a file transfer, so binaries and lock files add
# nothing.
CODE_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala", ".sh",
    ".ps1", ".sql", ".html", ".css", ".scss", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".md", ".rst", ".txt", ".vue", ".svelte", ".lua",
    ".r", ".pl", ".ex", ".exs", ".dart", ".m", ".mm",
}

SKIP_NAMES = {"package-lock.json", "yarn.lock", "poetry.lock", "Cargo.lock",
              "pnpm-lock.yaml", "composer.lock", "Gemfile.lock"}


class CodebaseError(Exception):
    """Raised when a repository cannot be cloned or has nothing to type."""


@dataclass
class SourceFile:
    """One file selected for typing."""
    relative: str          # posix-style path within the repository
    text: str
    characters: int
    lines: int

    @property
    def remote_relative(self) -> str:
        return self.relative.replace("/", "\\")


def slug(url: str) -> str:
    """A safe directory name for a repository URL."""
    name = url.rstrip("/").rsplit("/", 1)[-1]
    name = re.sub(r"\.git$", "", name)
    name = re.sub(r"[^A-Za-z0-9._-]", "-", name)
    return name or "repository"


def clone(url: str, into: Path | None = None, timeout: float = 300.0) -> Path:
    """Shallow-clone ``url`` locally and return the checkout directory."""
    if shutil.which("git") is None:
        raise CodebaseError(
            "git is not on PATH. Install Git for Windows to clone repositories.")

    target = Path(into) if into else Path(tempfile.mkdtemp(prefix="rdp-repo-"))
    target.mkdir(parents=True, exist_ok=True)
    checkout = target / slug(url)
    if checkout.exists():
        shutil.rmtree(checkout, ignore_errors=True)

    logger.info("Cloning %s", url)
    try:
        result = subprocess.run(
            ["git", "clone", "--depth", "1", "--quiet", url, str(checkout)],
            capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise CodebaseError(f"git clone timed out after {timeout:.0f}s") from None

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        raise CodebaseError(
            f"git clone failed: {detail[-1] if detail else 'unknown error'}")
    return checkout


def _is_typable(text: str) -> tuple[bool, str]:
    """Whether every character can be sent as a single RDP unicode event."""
    if "\x00" in text:
        return False, "looks binary"
    for ch in text:
        if ch in "\r\n\t":
            continue
        if len(ch.encode("utf-16-le")) != 2:
            # Report the codepoint, not the character: this message may be
            # printed to a console whose encoding cannot represent it.
            return False, f"contains U+{ord(ch):04X}, which is outside the BMP"
    return True, ""


def collect(root: Path, max_file_bytes: int = 20_000,
            suffixes: set[str] | None = None) -> tuple[list[SourceFile], list[str]]:
    """Walk the checkout and return typable files plus reasons for skips."""
    suffixes = suffixes or CODE_SUFFIXES
    found: list[SourceFile] = []
    skipped: list[str] = []

    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts[:-1]):
            continue
        if path.name in SKIP_NAMES:
            skipped.append(f"{relative}: lock file")
            continue
        if path.suffix.lower() not in suffixes:
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            skipped.append(f"{relative}: {exc}")
            continue
        if size > max_file_bytes:
            skipped.append(f"{relative}: {size / 1024:.0f} KB, over the per-file limit")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            skipped.append(f"{relative}: not UTF-8 text ({type(exc).__name__})")
            continue
        if not text.strip():
            continue
        typable, why = _is_typable(text)
        if not typable:
            skipped.append(f"{relative}: {why}")
            continue

        text = text.replace("\r\n", "\n").replace("\r", "\n")
        found.append(SourceFile(relative=relative, text=text,
                                characters=len(text), lines=text.count("\n") + 1))

    # Real source before dot-directory housekeeping. Plain path order would
    # spend the whole time budget on .devcontainer and .github templates and
    # never reach the code anyone actually wants to see typed.
    found.sort(key=lambda f: (any(p.startswith(".") for p in f.relative.split("/")),
                              f.relative))
    return found, skipped


def seconds_for(file: SourceFile, settings, editor_safe: bool,
                per_file_overhead: float = 14.0) -> float:
    """Estimate how long one file will take to type.

    Mirrors what ``InputSender`` actually does: a character costs a key delay
    plus a character delay, a newline costs a full key press, and the editor
    guard adds four more key presses per line.
    """
    per_char = settings.char_delay + settings.key_delay
    per_key = settings.key_delay + settings.action_delay
    total = file.characters * per_char + file.lines * per_key
    if editor_safe:
        total += file.lines * 4 * per_key
    return total + per_file_overhead


def plan(files: list[SourceFile], settings, editor_safe: bool,
         budget_seconds: float) -> tuple[list[SourceFile], list[SourceFile], float]:
    """Split files into what fits the time budget and what does not.

    A ``budget_seconds`` of 0 or less means no limit: every file is typed.

    Returns ``(chosen, dropped, estimated_seconds)``.
    """
    if budget_seconds <= 0:
        return (list(files), [],
                sum(seconds_for(f, settings, editor_safe) for f in files))

    chosen: list[SourceFile] = []
    dropped: list[SourceFile] = []
    spent = 0.0
    for file in files:
        cost = seconds_for(file, settings, editor_safe)
        if chosen and spent + cost > budget_seconds:
            dropped.append(file)
            continue
        if not chosen and cost > budget_seconds:
            # Always attempt at least one file, so a tight budget still shows
            # something happening rather than silently doing nothing.
            chosen.append(file)
            spent += cost
            continue
        chosen.append(file)
        spent += cost
    return chosen, dropped, spent


def human_time(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} hours"
