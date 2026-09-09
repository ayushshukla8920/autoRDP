"""The interactive menu shown when ``autordp`` is run with no command.

Two things this tool does, so two entries. The editor and the repository URL
are asked for afterwards, rather than multiplied into the menu -- the old five
entries were the same two actions crossed with the two editors, which made the
list longer without making it say more.

Deliberately a numbered list rather than an arrow-key selector. A full-screen
TUI needs raw terminal mode, and the two places this tool is most often started
from -- a Windows console inside an existing RDP session, and a plain VPS shell
-- are exactly where raw mode is least reliable. A numbered list works in both,
works in a pipe, and can be read out of a screenshot when something has gone
wrong.

Each choice prints the equivalent command line before running it, so the menu
teaches its way out of itself.
"""

from __future__ import annotations

import argparse
import shlex

from .. import __version__
from . import parser, ui

# Flags typed before the menu appeared. `autordp --host box repo u` is one
# thing, but `autordp --host box` lands here, and the host must not be lost.
_CARRIED = ("host", "username", "domain", "auth", "width", "height", "conn",
            "password", "password_stdin", "remember", "no_input", "quiet",
            "verbose", "json", "color", "ascii", "log_file", "view", "port",
            "view_host")

CHOICES = [
    ("Test connection", "connect",
     "connect, then drive the session from an rdp> prompt"),
    ("Type a codebase", "repo",
     "clone a git repository and type it into a remote editor"),
]


def run(args: argparse.Namespace, dispatch) -> int:
    """Show the menu and run the choice. ``dispatch`` comes from main."""
    ui.title("autordp", f"v{__version__}")
    ui.note("drives a Windows machine over RDP, in a session it opens itself")

    if not ui.is_interactive() or args.no_input:
        # Nothing to select with. A pointer to --help is more use than a prompt
        # that immediately reads EOF, which is what the old menu did here.
        ui.say()
        ui.warn("no terminal to read a choice from")
        ui.note("run `autordp --help`, or name a command directly")
        return 2

    ui.say()
    for index, (label, _, why) in enumerate(CHOICES, start=1):
        ui.say(f"  {ui.style(str(index), 'bold', 'cyan')}  "
               f"{label.ljust(20)}{ui.style(why, 'grey')}")
    ui.say(f"  {ui.style('d', 'bold', 'cyan')}  "
           f"{'Check this machine'.ljust(20)}"
           f"{ui.style('runtime, credentials, whether the port answers', 'grey')}")
    ui.say(f"  {ui.style('s', 'bold', 'cyan')}  "
           f"{'Save connection'.ljust(20)}"
           f"{ui.style('verify details, then remember them', 'grey')}")
    ui.say(f"  {ui.style('q', 'bold', 'cyan')}  Quit")
    ui.say()

    try:
        choice = ui.prompt("Choice", "1").lower()
    except EOFError:
        return 130
    if choice in ("q", "quit", "exit"):
        return 130

    if choice == "d":
        argv = ["doctor"]
    elif choice == "s":
        argv = ["config", "set"]
    elif choice.isdigit() and 1 <= int(choice) <= len(CHOICES):
        argv = _build_argv(CHOICES[int(choice) - 1][1])
    else:
        ui.error(f"{choice!r} is not one of 1-{len(CHOICES)}, d, s or q")
        return 2

    ui.say()
    ui.note("same as: " + ui.style(
        "autordp " + " ".join(shlex.quote(a) for a in argv), "cyan"))

    # Round-tripping through the real parser, rather than calling the command
    # function directly, is what makes the line printed above honest: if it
    # would not parse, the menu cannot run it either.
    chosen = parser.parse(argv)
    for name in _CARRIED:
        value = getattr(args, name, None)
        if value not in (None, "", False, 0) and not _was_typed(argv, name):
            setattr(chosen, name, value)
    return dispatch(chosen)


def _build_argv(command: str) -> list[str]:
    """Ask for the arguments the chosen command cannot do without."""
    if command == "connect":
        argv = ["connect"]
        if ui.confirm("Watch it in a browser?", default=False):
            argv.append("--view")
        return argv

    ui.say()
    url = ui.prompt("Repository URL", required=True)
    minutes = ui.prompt("Minutes to spend (0 = no limit)", "10")
    editor = ui.prompt("Editor", "notepad", choices=("notepad", "code"))
    argv = ["repo", url, "--minutes", minutes, "--editor", editor]
    if ui.confirm("Watch it in a browser?", default=False):
        argv.append("--view")
    return argv


def _was_typed(argv: list[str], name: str) -> bool:
    """Whether the menu's own argv already sets this option."""
    return ("--" + name.replace("_", "-")) in argv
