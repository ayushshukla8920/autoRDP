"""Turning a parsed command line into connection settings.

Resolution order, first match wins:

1. an explicit flag  (``--host``)
2. the environment   (``RDP_HOST``)
3. the saved profile (``autordp config show``)
4. a prompt, but only when there is a terminal to prompt at

Step 4 is the one worth being careful about. The old CLI decided whether to
prompt from whether a subcommand was given, which meant ``cli.py repo`` under
cron with a missing password would raise a config error, but ``cli.py`` under
cron would block forever on ``input()``. Here the test is whether stdin is
actually a terminal, so a pipe, a cron job and a CI runner all fail fast with a
message naming the flag to pass.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

from .. import credentials
from ..config import ConfigError, Settings
from . import ui

# Flag name -> (environment variable, profile key), for the fields a flag can
# set. `port` is deliberately absent: `--port` is the *live view* port now, so
# reading args.port here would make `-p 9000` connect to RDP on 9000. It is
# resolved from the environment and the saved connection only.
FIELDS = {
    "host": ("RDP_HOST", "host"),
    "username": ("RDP_USERNAME", "username"),
    "domain": ("RDP_DOMAIN", "domain"),
    "width": ("RDP_WIDTH", "width"),
    "height": ("RDP_HEIGHT", "height"),
}


def _flag(args: argparse.Namespace, name: str) -> str:
    value = getattr(args, name, None)
    return str(value).strip() if value not in (None, "") else ""


def resolve_password(args: argparse.Namespace, saved: dict,
                     interactive: bool) -> str:
    """Find a password, preferring the routes that keep it out of `ps`.

    ``--password`` exists because it is sometimes the only practical option,
    but it is checked *after* stdin and before nothing else by accident: an
    argument is visible in ``ps`` output and lands in shell history, so the
    order here quietly rewards ``--password-stdin``, ``RDP_PASSWORD`` and the
    encrypted profile.
    """
    if getattr(args, "password_stdin", False):
        secret = sys.stdin.readline().rstrip("\r\n")
        if not secret:
            raise ConfigError("--password-stdin was given but stdin was empty")
        return secret

    flag = (getattr(args, "password", "") or "").strip()
    if flag:
        return flag

    from_env = os.environ.get("RDP_PASSWORD", "")
    if from_env:
        return from_env
    if saved.get("password"):
        return saved["password"]
    if interactive:
        return _ask_password()
    return ""


def _ask_password() -> str:
    while True:
        try:
            secret = getpass.getpass("  Password (not echoed): ")
        except EOFError:
            raise ConfigError(
                "A password is required but stdin is closed. Set RDP_PASSWORD, "
                "pipe one in with --password-stdin, or save one with "
                "`autordp config set --remember-password`.") from None
        if secret:
            return secret
        ui.fail("required")


def resolve(args: argparse.Namespace, *, prompt_missing: bool | None = None) -> Settings:
    """Build :class:`Settings` for this invocation.

    ``prompt_missing`` overrides the terminal test -- the interactive menu sets
    it True so that it can re-ask for details even when a profile exists.
    """
    index = getattr(args, "conn", None)
    saved = credentials.load(index)
    if index is not None and not saved:
        total = credentials.count()
        raise ConfigError(
            f"No saved connection {index}. "
            + (f"There are {total}; run `autordp list` to see them."
               if total else "Nothing is saved yet -- run `autordp config set`."))

    interactive = ui.is_interactive() if prompt_missing is None else prompt_missing
    if getattr(args, "no_input", False):
        interactive = False

    resolved = {}
    for name, (env_name, profile_key) in FIELDS.items():
        resolved[name] = (_flag(args, name)
                          or os.environ.get(env_name, "").strip()
                          or saved.get(profile_key, ""))

    if saved:
        where = (f"saved connection {index} ({saved.label})" if index is not None
                 else f"remembered details from {credentials.profile_path()}")
        if interactive or index is not None:
            ui.note(where)

    # An explicitly chosen connection is a complete answer. Prompting for
    # fields it already supplies would make `--conn 2` slower than typing the
    # host out, which defeats the point of saving it.
    if interactive and index is None:
        resolved["host"] = ui.prompt("Host / IP", resolved["host"], required=True)
        resolved["username"] = ui.prompt("Username", resolved["username"],
                                         required=True)
        resolved["domain"] = ui.prompt("Domain (blank for none)", resolved["domain"])

    password = resolve_password(args, saved, interactive)

    missing = [name for name in ("host", "username") if not resolved[name]]
    if not password:
        missing.append("password")
    if missing:
        raise ConfigError(_missing_message(missing))

    # Settings.load reads the environment, so this is how the resolved values
    # reach it -- including the many tuning knobs (RDP_CHAR_DELAY and friends)
    # that have no flag and are only ever set this way.
    os.environ["RDP_HOST"] = resolved["host"]
    os.environ["RDP_USERNAME"] = resolved["username"]
    os.environ["RDP_DOMAIN"] = resolved["domain"]
    # No flag for this one: environment, saved connection, then the default.
    os.environ["RDP_PORT"] = (os.environ.get("RDP_PORT", "").strip()
                              or saved.get("port", "") or "3389")
    os.environ["RDP_PASSWORD"] = password
    for name in ("width", "height"):
        if resolved[name]:
            os.environ["RDP_" + name.upper()] = resolved[name]
    if getattr(args, "auth", None):
        os.environ["RDP_AUTH"] = args.auth
    if getattr(args, "log_file", None):
        os.environ["RDP_LOG_FILE"] = str(args.log_file)
    if getattr(args, "verbose", 0) > 1:
        os.environ["RDP_LOG_LEVEL"] = "DEBUG"

    return Settings.load(interactive=False)


def _missing_message(missing: list[str]) -> str:
    flags = {"host": "--host", "username": "--user", "password": "RDP_PASSWORD"}
    named = _join(missing)
    how = _join([flags[name] for name in missing])
    return (f"No {named} available, and there is no terminal to ask at.\n"
            f"        Pass {how}, or run `autordp config set` once from a "
            f"terminal to remember them.")


def _join(items: list[str]) -> str:
    """`a`, `a and b`, `a, b and c` -- an error message is prose, not a list."""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " or " + items[-1]
