"""The command line grammar.

Two actions, and the utilities that support them:

    connect   open a session and drive it -- the connection test
    repo      clone a git repository and type it into a remote editor

    config    inspect and edit the saved profile
    doctor    check this machine is ready
    keys      list the key names `key` understands

The shape this replaces was ``cli.py [test|demo|repo] [repo-url] --editor X``,
where the action and the editor multiplied together into five hidden internal
names (``demo-notepad``, ``repo-code``, ...) and the second positional was only
meaningful for one of the three actions. That is hard to document and harder to
discover: ``--help`` could not say that ``--minutes`` applies to ``repo`` and
nothing else, because argparse had no way to know.

The connection flags are shared by the commands that need them through a parent
parser, which uses ``default=argparse.SUPPRESS`` for a specific reason. Sharing
a parent between the top level and a subparser normally means the subparser's
defaults overwrite whatever the top level parsed, so ``autordp --host x repo u``
would silently lose the host. Suppressed defaults are simply absent from the
namespace unless the user typed them, so both orders work and :data:`DEFAULTS`
fills in the rest afterwards.
"""

from __future__ import annotations

import argparse

from .. import __version__
from ..session import DEFAULT_REMOTE_DIR
from ..webview import DEFAULT_PORT

EDITORS = ("notepad", "code")

# Applied after parsing, for every option the suppressed parents left out.
DEFAULTS = {
    "host": "", "username": "", "domain": "",
    "width": "", "height": "", "auth": "",
    "conn": None, "password": "", "password_stdin": False, "no_input": False,
    "color": "auto", "ascii": False, "quiet": False, "verbose": 0,
    "json": False, "log_file": None, "remember": False,
    "view": False, "port": None, "view_host": "127.0.0.1",
    "detach": False, "pid_file": None,
}

EPILOG = """\
examples:
  autordp                                    interactive menu
  autordp list                               saved connections, numbered
  autordp connect --conn 2                   use saved connection 2
  autordp connect --view -p 9000             watch it in a browser on :9000
  autordp connect -d --view                  detach; survives the SSH session
  autordp status                             is the detached run alive?
  autordp stop                               stop it cleanly
  autordp connect -c "key win+r" -c "type notepad"
  autordp repo https://github.com/pallets/click --minutes 30
  autordp repo https://github.com/me/proj --dry-run   plan only, no connection
  autordp doctor                             check this machine before a long run

configuration:
  Connection details come from flags, then --conn N, then RDP_HOST /
  RDP_USERNAME / RDP_PASSWORD / RDP_DOMAIN, then a prompt. The RDP port is
  3389 and has no flag; set RDP_PORT if you really need another. Run
  `autordp config env` for every variable that is read.

exit codes:
  0 success          3 emergency stop        6 repository problem
  1 connection       4 input refused       130 interrupted
  2 configuration    5 input error
"""


class _Formatter(argparse.RawDescriptionHelpFormatter):
    """Keep the epilog verbatim but still wrap long option help."""

    def __init__(self, prog, **kwargs):
        # argparse defaults to the full terminal width, which makes help text
        # on a wide monitor a single unreadable line per option.
        kwargs.setdefault("max_help_position", 30)
        kwargs.setdefault("width", 96)
        super().__init__(prog, **kwargs)


def _connection_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    group = parent.add_argument_group("connection")
    group.add_argument("-H", "--host", metavar="HOST",
                       default=argparse.SUPPRESS, help="server hostname or IP")
    group.add_argument("-u", "--user", "--username", dest="username",
                       metavar="NAME", default=argparse.SUPPRESS,
                       help="account to sign in as")
    # No short form: -d is --detach. A domain is rarely typed and never typed
    # twice in a session, whereas -d for "background" is muscle memory.
    group.add_argument("--domain", metavar="DOMAIN",
                       default=argparse.SUPPRESS,
                       help="Windows domain, if the account needs one")
    # There is deliberately no flag for the RDP port. It is 3389 on every
    # Windows box worth pointing this at, a flag for it collided with the two
    # things people actually want short options for, and the escape hatch
    # (RDP_PORT, or a saved connection) still exists for the rare exception.
    group.add_argument("--auth", choices=("ntlm", "kerberos", "plain"),
                       default=argparse.SUPPRESS,
                       help="authentication method (default ntlm)")
    group.add_argument("--conn", type=int, metavar="N",
                       default=argparse.SUPPRESS,
                       help="use saved connection N -- see `autordp list`. "
                            "Any flag given alongside it wins")
    group.add_argument("--password", metavar="SECRET",
                       default=argparse.SUPPRESS,
                       help="password. Visible in `ps` and in shell history, so "
                            "prefer RDP_PASSWORD or --password-stdin where you "
                            "can")
    group.add_argument("--width", metavar="PX", default=argparse.SUPPRESS,
                       help="remote screen width (default 1280)")
    group.add_argument("--height", metavar="PX", default=argparse.SUPPRESS,
                       help="remote screen height (default 800)")
    group.add_argument("--password-stdin", action="store_true",
                       default=argparse.SUPPRESS,
                       help="read the password from stdin, for a secret piped "
                            "in from a password manager")
    group.add_argument("--remember", action="store_true",
                       default=argparse.SUPPRESS,
                       help="save the password too, encrypted for this Windows "
                            "user with DPAPI (Windows only)")
    return parent


def _view_parent() -> argparse.ArgumentParser:
    """The browser live view, on the two commands that hold a session open."""
    parent = argparse.ArgumentParser(add_help=False)
    group = parent.add_argument_group("live view")
    group.add_argument("--view", action="store_true",
                       default=argparse.SUPPRESS,
                       help="serve a read-only live view of the remote screen "
                            "over HTTP")
    group.add_argument("-p", "--port", type=int, metavar="PORT",
                       default=argparse.SUPPRESS,
                       help=f"port for the live view (default {DEFAULT_PORT}; "
                            f"another is chosen if it is busy). Implies --view")
    group.add_argument("--view-host", metavar="ADDR", default=argparse.SUPPRESS,
                       help="address to bind the view to (default 127.0.0.1). "
                            "Anything else exposes an unauthenticated picture "
                            "of the remote desktop -- prefer an SSH tunnel")
    return parent


def _daemon_parent() -> argparse.ArgumentParser:
    """Running in the background, for the commands that can take a while."""
    parent = argparse.ArgumentParser(add_help=False)
    group = parent.add_argument_group("background")
    group.add_argument("-d", "--detach", action="store_true",
                       default=argparse.SUPPRESS,
                       help="run in the background and return the prompt. The "
                            "child has no controlling terminal, so it survives "
                            "the SSH session ending. Manage it with `autordp "
                            "status` and `autordp stop`")
    group.add_argument("--pid-file", metavar="PATH", default=argparse.SUPPRESS,
                       help="where the detached run records itself "
                            "(default autordp.pid, beside STOP)")
    return parent


def _output_parent() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    group = parent.add_argument_group("output")
    group.add_argument("-q", "--quiet", action="store_true",
                       default=argparse.SUPPRESS,
                       help="only errors and requested data")
    group.add_argument("-v", "--verbose", action="count",
                       default=argparse.SUPPRESS,
                       help="more detail; twice also turns on debug logging")
    group.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                       help="machine-readable output on stdout, narration on "
                            "stderr (doctor, config show, repo --dry-run)")
    group.add_argument("--color", choices=("auto", "always", "never"),
                       default=argparse.SUPPRESS,
                       help="colour output (default auto; NO_COLOR is honoured)")
    group.add_argument("--ascii", action="store_true", default=argparse.SUPPRESS,
                       help="plain ASCII glyphs, for a console that cannot "
                            "encode box drawing")
    group.add_argument("--no-input", action="store_true",
                       default=argparse.SUPPRESS,
                       help="never prompt; fail instead if something is missing")
    group.add_argument("--log-file", metavar="PATH", default=argparse.SUPPRESS,
                       help="also write the log to this file")
    return parent


def build() -> argparse.ArgumentParser:
    connection = _connection_parent()
    output = _output_parent()
    view = _view_parent()
    background = _daemon_parent()

    parser = argparse.ArgumentParser(
        prog="autordp",
        description="Drive a Windows machine over RDP, in a session this tool "
                    "opens itself. Your own desktop is never touched.",
        epilog=EPILOG,
        formatter_class=_Formatter,
        parents=[connection, output])
    parser.add_argument("-V", "--version", action="version",
                        version=f"autordp {__version__}")

    subcommands = parser.add_subparsers(dest="command", metavar="<command>")

    # -- connect ------------------------------------------------------------
    connect = subcommands.add_parser(
        "connect", parents=[connection, view, background, output],
        formatter_class=_Formatter,
        help="test the connection and drive it from an rdp> prompt",
        description="Connect, prove the credentials work, and hand you an "
                    "interactive prompt. Type `help` there for the command "
                    "list, or send commands without a prompt using -c / "
                    "--script.")
    # dest is not "command": the subparsers already own that name, and letting
    # -c write to it would overwrite "connect" with the list of rdp> commands,
    # leaving the dispatcher with nothing to route on.
    connect.add_argument("-c", "--command", action="append", metavar="CMD",
                         dest="run_commands", default=[],
                         help="run one rdp> command and exit; repeatable, and "
                              "runs in the order given")
    connect.add_argument("--script", metavar="FILE",
                         help="run rdp> commands from a file, one per line; "
                              "`-` reads stdin. Blank lines and # comments are "
                              "skipped")
    connect.add_argument("--keep-open", action="store_true",
                         help="stay at the prompt after -c / --script instead "
                              "of disconnecting")
    connect.add_argument("--hold", action="store_true",
                         help="hold the session open without a prompt, until "
                              "the server drops it or the process is signalled. "
                              "This is the mode for a service manager -- pm2, "
                              "systemd, docker -- where there is no terminal to "
                              "read commands from. Pairs with --view")

    # -- repo ---------------------------------------------------------------
    repo = subcommands.add_parser(
        "repo", parents=[connection, view, background, output],
        formatter_class=_Formatter,
        help="clone a git repository and type it into a remote editor",
        description="Shallow-clone a repository, choose as many files as fit "
                    "the time budget, and type them one by one. Typing runs at "
                    "roughly 20-25 characters a second, so use --dry-run first "
                    "to see what a budget actually buys.")
    repo.add_argument("url", help="repository URL to clone")
    repo.add_argument("-m", "--minutes", type=float, default=10.0, metavar="N",
                      help="time budget; 0 means no limit (default 10)")
    repo.add_argument("-e", "--editor", choices=EDITORS, default="notepad",
                      help="which remote editor to drive (default notepad)")
    repo.add_argument("--dry-run", action="store_true",
                      help="clone, select and estimate, then stop. Never "
                           "connects, so it needs no credentials")
    repo.add_argument("--max-file-bytes", type=int, default=20_000, metavar="N",
                      help="skip files larger than this (default 20000)")
    repo.add_argument("--remote-dir", metavar="PATH", default=DEFAULT_REMOTE_DIR,
                      help=f"remote directory to write into "
                           f"(default {DEFAULT_REMOTE_DIR})")
    repo.add_argument("--no-screenshots", dest="screenshots",
                      action="store_false",
                      help="skip the progress screenshots")

    timing = repo.add_argument_group("timing")
    timing.add_argument("--editor-wait", type=float, metavar="SECONDS",
                        default=None,
                        help="seconds to wait for the editor to start "
                             "(default 4 for notepad, 12 for code)")
    timing.add_argument("--startup-timeout", type=float, metavar="SECONDS",
                        default=45.0,
                        help="longest to wait for the remote shell (default 45)")
    timing.add_argument("--startup-wait", type=float, metavar="SECONDS",
                        default=2.0,
                        help="extra settle time after the desktop appears "
                             "(default 2)")
    timing.add_argument("--dialog-wait", type=float, metavar="SECONDS",
                        default=1.5,
                        help="seconds to wait for the Run dialog (default 1.5)")
    timing.add_argument("--action-delay", type=float, metavar="SECONDS",
                        default=0.05,
                        help="post-keystroke delay while typing (default 0.05)")

    # -- config -------------------------------------------------------------
    config = subcommands.add_parser(
        "config", parents=[output], formatter_class=_Formatter,
        help="inspect and edit the saved connection profile",
        description="The profile lives outside the repository -- in "
                    "%LOCALAPPDATA% on Windows -- so it can never be committed "
                    "by accident.")
    actions = config.add_subparsers(dest="config_command", metavar="<action>")
    actions.add_parser("show", parents=[output], formatter_class=_Formatter,
                       help="print the saved profile (password masked)")
    actions.add_parser("path", parents=[output], formatter_class=_Formatter,
                       help="print where the profile file lives")
    actions.add_parser("env", parents=[output], formatter_class=_Formatter,
                       help="list every environment variable that is read")
    config_set = actions.add_parser(
        "set", parents=[connection, output], formatter_class=_Formatter,
        help="save connection details for next time",
        description="Prompts for each field, showing what is already saved as "
                    "the default, then verifies the details by connecting "
                    "before writing anything.")
    config_set.add_argument("--remember-password", action="store_true",
                            help="also save the password, encrypted with DPAPI")
    config_set.add_argument("--name", metavar="LABEL",
                            help="a label for this connection, so `autordp list` "
                                 "shows something friendlier than user@host")
    config_forget = actions.add_parser(
        "forget", parents=[output], formatter_class=_Formatter,
        help="delete one saved connection, or all of them")
    config_forget.add_argument("index", nargs="?", type=int, metavar="N",
                               help="which connection to remove (see `autordp "
                                    "list`). Omit to remove every one")

    # -- doctor / keys ------------------------------------------------------
    doctor = subcommands.add_parser(
        "doctor", parents=[connection, output], formatter_class=_Formatter,
        help="check this machine is ready for a run",
        description="Checks the runtime, the bundled RDP stack and its "
                    "keyboard layouts, git, the saved profile, the writable "
                    "paths, and whether the server's RDP port answers. Exits "
                    "non-zero if anything would stop a run.")
    doctor.add_argument("--no-network", action="store_true",
                        help="skip the TCP reachability probe")

    status = subcommands.add_parser(
        "status", parents=[output], formatter_class=_Formatter,
        help="is a detached run alive?",
        description="Reports on the run started by -d: its pid, how long it "
                    "has been up, where its log is, and the live view URL.")
    status.add_argument("--pid-file", metavar="PATH",
                        help="which detached run to look at "
                             "(default autordp.pid in this directory)")

    stop = subcommands.add_parser(
        "stop", parents=[output], formatter_class=_Formatter,
        help="stop a detached run",
        description="Asks the detached run to disconnect cleanly and waits for "
                    "it. Falls back to the emergency stop file if the signal "
                    "cannot be delivered.")
    stop.add_argument("--pid-file", metavar="PATH",
                      help="which detached run to stop")
    stop.add_argument("--timeout", type=float, default=20.0, metavar="SECONDS",
                      help="how long to wait for a clean exit (default 20)")
    stop.add_argument("--force", action="store_true",
                      help="kill it if it has not gone by then")

    subcommands.add_parser(
        "list", parents=[output], formatter_class=_Formatter,
        help="list saved connections and their index numbers",
        description="Every connection `autordp config set` has saved, numbered. "
                    "Pass a number to any command with --conn N.")

    keys = subcommands.add_parser(
        "keys", parents=[output], formatter_class=_Formatter,
        help="list the key names that `key` understands",
        description="Names accepted by the rdp> `key` command and by "
                    "`connect -c`. Combine them with '+' for a chord.")
    keys.add_argument("pattern", nargs="?",
                      help="show only names containing this text")

    subcommands.add_parser(
        "menu", parents=[connection, output], formatter_class=_Formatter,
        help="the interactive menu (also what running with no command does)")

    return parser


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse ``argv`` and fill in the values the suppressed parents left out."""
    args = build().parse_args(argv)
    for name, value in DEFAULTS.items():
        if not hasattr(args, name):
            setattr(args, name, value)
    return args
