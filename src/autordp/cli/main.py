"""Entry point: parse, dispatch, and turn exceptions into exit codes.

Exactly one place maps a failure to a number, and it is this one. Commands
raise; ``--help`` documents what each number means; cron and CI can rely on
both agreeing because neither is written down twice.

  0  success                4  the session stopped accepting input
  1  connection failed      5  input error
  2  configuration          6  repository problem
  3  emergency stop       130  interrupted
"""

from __future__ import annotations

import argparse
import sys

from ..codebase import CodebaseError
from ..config import ConfigError
from ..client import ConnectionFailed
from ..input import EmergencyStopped, InputError, SessionNotAcceptingInput
from . import commands, menu, parser, ui

EXIT_OK = 0
EXIT_CONNECTION = 1
EXIT_CONFIG = 2
EXIT_STOPPED = 3
EXIT_REFUSED = 4
EXIT_INPUT = 5
EXIT_REPOSITORY = 6
EXIT_INTERRUPTED = 130

COMMANDS = {
    "connect": commands.connect,
    "repo": commands.repo,
    "config": commands.config,
    "doctor": commands.doctor,
    "keys": commands.keys,
}


def dispatch(args: argparse.Namespace) -> int:
    if args.command in (None, "menu"):
        return menu.run(args, dispatch)
    return COMMANDS[args.command](args)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parser.parse(argv)
    except SystemExit as exc:  # --help and argparse's own errors
        return int(exc.code or 0)

    ui.configure(color=args.color, quiet=args.quiet, verbose=args.verbose,
                 json_mode=args.json, ascii_only=args.ascii)

    try:
        return dispatch(args)

    except KeyboardInterrupt:
        ui.say()
        ui.warn("interrupted")
        return EXIT_INTERRUPTED
    except ConfigError as exc:
        ui.error(str(exc), "`autordp config show` lists what is remembered")
        return EXIT_CONFIG
    except ConnectionFailed as exc:
        ui.error(f"connection failed: {exc}",
                 "`autordp doctor` checks the port, the credentials and the "
                 "firewall in one go")
        return EXIT_CONNECTION
    except EmergencyStopped as exc:
        ui.error(f"emergency stop: {exc}",
                 "delete the stop file, or run `resume` at the rdp> prompt")
        return EXIT_STOPPED
    except SessionNotAcceptingInput as exc:
        ui.error(f"the session stopped accepting input: {exc}",
                 "the remote session was probably locked, disconnected or "
                 "signed out")
        return EXIT_REFUSED
    except InputError as exc:
        ui.error(f"input error: {exc}")
        return EXIT_INPUT
    except CodebaseError as exc:
        ui.error(f"repository problem: {exc}")
        return EXIT_REPOSITORY
    except BrokenPipeError:
        # `autordp keys | head` closes the pipe under us. Exiting quietly is the
        # correct behaviour; Python would otherwise print a confusing trace from
        # the interpreter's shutdown flush.
        try:
            sys.stdout.close()
        except OSError:
            pass
        return EXIT_OK
    except Exception as exc:  # noqa: BLE001 - last resort, keep it readable
        if args.verbose:
            raise
        ui.error(f"{type(exc).__name__}: {exc}",
                 "re-run with -v for the full traceback")
        return EXIT_INPUT


if __name__ == "__main__":
    sys.exit(main())
