"""What each subcommand actually does.

Every command follows the same shape: resolve settings, open one client, do the
work, and let :mod:`autordp.cli.main` map any exception to an exit code. None of
them catch the domain exceptions themselves -- keeping that mapping in exactly
one place is what makes the exit codes in ``--help`` trustworthy.

``repo`` re-renders what the session layer prints. ``session.py`` narrates with
``print`` so that its output reads correctly in a log file or a cron mail;
:func:`_restyled_output` intercepts that stream and repaints it for a terminal,
which keeps one narration in the code and gives both audiences the right one.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import socket
import sys
import time
from pathlib import Path

from .. import __version__, codebase, credentials, session, shell, webview
from ..client import RdpClient
from ..config import ConfigError, Settings, setup_logging
from ..input import KEY_ALIASES
from ..loop import LoopThread
from . import context, ui

# `[3] Waiting for the remote desktop` -- the session layer's stage markers.
_STAGE = re.compile(r"^\[(\d+)\] (.*)$")
# `[7/40] src/thing.py (120 lines, 3400 chars)` -- its per-file header.
_FILE = re.compile(r"^\[(\d+)/(\d+)\] (.*)$")


# --------------------------------------------------------------- shared plumbing

@contextlib.contextmanager
def _restyled_output(total_stages: int):
    """Repaint the session layer's ``print`` output in the CLI's own style."""

    def sink(line: str) -> None:
        text = line.rstrip()
        if not text:
            return
        stage = _STAGE.match(text)
        if stage:
            ui.step(int(stage.group(1)), total_stages, stage.group(2))
            return
        if _FILE.match(text):
            # The progress bar already shows which file is being typed, and far
            # more usefully. Keep it for -v, drop it otherwise.
            ui.detail(text)
            return
        if text.startswith("      "):
            ui.detail(text.strip())
        elif text.startswith("    "):
            ui.note(text.strip())
        else:
            ui.say(text)

    original = sys.stdout
    sys.stdout = ui.Capture(sink)
    try:
        yield
    finally:
        sys.stdout.flush()
        sys.stdout = original


@contextlib.contextmanager
def _client(settings: Settings):
    """One connected-and-then-cleanly-closed client, on its own event loop.

    The loop lives on a worker thread so that Ctrl+C is delivered to the main
    thread while a keystroke is in flight; see :mod:`autordp.loop`.
    """
    client = RdpClient(settings)
    if settings.stop_file.exists():
        ui.warn(f"clearing a stale stop file at {settings.stop_file}")
        client.stop.reset()

    loop = LoopThread()
    loop.start()
    try:
        yield client, loop
    finally:
        try:
            loop.run(client.disconnect())
        except Exception as exc:  # noqa: BLE001 - already on the way out
            ui.warn(f"unclean disconnect: {exc}")
        loop.close()


@contextlib.contextmanager
def _live_view(client: RdpClient, args: argparse.Namespace):
    """Start the browser view if --view was given; always a no-op otherwise."""
    if args.view is None:
        yield None
        return

    port = webview.free_port(args.view, args.view_host)
    view = webview.LiveView(client, port=port, host=args.view_host)
    try:
        url = view.start()
    except OSError as exc:
        # The view is a convenience. Losing it must not cost the run.
        ui.warn(f"could not start the live view on {args.view_host}:{port}: {exc}")
        yield None
        return

    if port != args.view:
        ui.note(f"port {args.view} was busy, using {port}")
    ui.ok(f"live view at {ui.style(url, 'bright_cyan', 'underline')}")
    if view.exposed:
        ui.warn(f"the view is bound to {args.view_host}, so anyone who can "
                f"reach this machine can watch the remote desktop")
        ui.note(f"it is unauthenticated. Prefer an SSH tunnel: "
                f"ssh -L {port}:127.0.0.1:{port} <this-host>")
    try:
        yield view
    finally:
        view.stop()


def _connect(client: RdpClient, loop: LoopThread) -> None:
    target = client.settings.target
    with ui.spinner(f"Connecting to {target}"):
        loop.run(client.connect(), on_interrupt=lambda: client.stop.trip("Ctrl+C"))


def _stop_banner(settings: Settings) -> None:
    ui.note(f"emergency stop: press Ctrl+C, or create {settings.stop_file}")


def _prepare(args: argparse.Namespace) -> Settings:
    settings = context.resolve(args)
    setup_logging(settings)
    return settings


# ------------------------------------------------------------------- connect

def _script_lines(args: argparse.Namespace) -> list[str]:
    """The commands to run without a prompt, from -c and --script combined."""
    lines = list(args.run_commands)
    if args.script:
        if args.script == "-":
            text = sys.stdin.read()
        else:
            path = Path(args.script)
            if not path.is_file():
                raise ConfigError(f"No such script file: {path}")
            text = path.read_text(encoding="utf-8")
        for raw in text.splitlines():
            stripped = raw.strip()
            if stripped and not stripped.startswith("#"):
                lines.append(stripped)
    return lines


def connect(args: argparse.Namespace) -> int:
    settings = _prepare(args)
    scripted = _script_lines(args)

    with _client(settings) as (client, loop):
        _connect(client, loop)
        # Only ever remember details that actually worked.
        settings.remember(remember_password=args.remember)
        ui.kv("remote screen", f"{settings.width}x{settings.height}")

        with _live_view(client, args):
            _stop_banner(settings)
            if scripted:
                code = _run_scripted(loop, client, settings, scripted)
                if not args.keep_open:
                    return code
            return _repl(loop, client, settings)


def _run_scripted(loop: LoopThread, client: RdpClient, settings: Settings,
                  lines: list[str]) -> int:
    """Run a fixed list of rdp> commands, stopping at the first failure.

    Stopping early is the right default for a script: the commands are almost
    always a sequence where each one assumes the last worked, so carrying on
    after a failed click would type into whatever happened to have focus.
    """
    ui.heading(f"Running {len(lines)} command(s)")
    for index, line in enumerate(lines, start=1):
        ui.say(f"  {ui.style(f'{index:>3}', 'grey')} {ui.style(line, 'cyan')}")
        command, _, argument = line.partition(" ")
        if command.lower() in ("quit", "exit"):
            break
        if not shell.dispatch(loop, client, settings, command.lower(),
                              argument.strip(), line):
            ui.error(f"unknown command {command!r} in the script",
                     "run `autordp connect` and type `help` for the list")
            return 5
    return 0


def _repl(loop: LoopThread, client: RdpClient, settings: Settings) -> int:
    """The interactive prompt.

    Command handling is delegated to :func:`autordp.shell.dispatch`, which the
    scripted path also calls, so a command works identically both ways and is
    only written once. Only the prompt, the banner and the error styling live
    here.
    """
    _readline()
    ui.say()
    ui.note("type `help` for commands, `quit` to leave")
    prompt = ui.style("rdp", "bold", "cyan") + ui.style("> ", "grey")

    while True:
        if not client.is_alive:
            ui.error("the RDP session disconnected")
            return 1
        try:
            line = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            ui.say()
            return 0
        if not line:
            continue

        command, _, argument = line.partition(" ")
        command = command.lower()
        if command in ("quit", "exit", "q"):
            return 0
        if command in ("help", "?"):
            ui.say()
            for name, description in shell.COMMANDS:
                ui.say("  " + ui.style(name.ljust(30), "cyan") + description)
            ui.say()
            continue

        try:
            if not shell.dispatch(loop, client, settings, command,
                                  argument.strip(), line):
                ui.fail(f"unknown command {command!r} -- try `help`")
        except KeyboardInterrupt:
            if settings.stop_file.exists():
                ui.warn(f"{settings.stop_file} is present; `resume` clears it")
            else:
                client.stop.reset()
                ui.warn("command stopped; the session is still connected")
        except Exception as exc:  # noqa: BLE001 - a bad command must not exit
            ui.fail(f"{type(exc).__name__}: {exc}")


def _readline() -> None:
    """Turn on history and tab completion where they are available.

    Not on Windows without pyreadline3, which is why this is best effort: the
    prompt works either way, it just loses arrow-key history.
    """
    try:
        import readline
    except ImportError:
        return
    try:
        readline.parse_and_bind("tab: complete")
        names = sorted(name.split()[0] for name, _ in shell.COMMANDS)

        def complete(text: str, state: int):
            matches = [n for n in names if n.startswith(text)]
            return matches[state] + " " if state < len(matches) else None

        readline.set_completer(complete)
    except Exception:  # noqa: BLE001 - never worth failing to start over
        pass


# ---------------------------------------------------------------------- repo

def repo(args: argparse.Namespace) -> int:
    if args.dry_run:
        return _repo_dry_run(args)

    settings = _prepare(args)
    ui.title("Typing a codebase", f"{args.editor} on {settings.host}")
    if args.minutes > 0:
        ui.note(f"budget {args.minutes:g} min -- pass --minutes 0 for no limit")

    with _client(settings) as (client, loop):
        state: dict = {"bar": None, "typed": 0, "files": 0}

        def on_plan(chosen, dropped, estimate) -> None:
            # Sized in characters, not files: file sizes in a repository vary
            # by two orders of magnitude, so a per-file bar would sit at 3/40
            # for twenty minutes and then jump. The +f.lines accounts for the
            # newline each line costs.
            total = sum(f.characters + f.lines for f in chosen)
            state["files"] = len(chosen)
            state["bar"] = ui.Progress(total, label="typing").__enter__()

        def on_file(index: int, total: int, source) -> None:
            if state["bar"] is not None:
                state["bar"].set_status(f"{index}/{total}  {source.relative}")
            state["typed"] = index

        def on_chars(count: int) -> None:
            if state["bar"] is not None:
                state["bar"].advance(count)

        started = time.monotonic()
        with _live_view(client, args):
            _stop_banner(settings)
            try:
                with _restyled_output(session.TOTAL_STAGES):
                    loop.run(session.run_codebase_session(
                        client, args, args.url, args.minutes * 60.0,
                        on_file=on_file, on_chars=on_chars, on_plan=on_plan),
                        on_interrupt=lambda: client.stop.trip("Ctrl+C"))
            finally:
                if state["bar"] is not None:
                    state["bar"].finish()

        _summary("Codebase typed", [
            ("repository", args.url),
            ("files", f"{state['typed']}/{state['files']}"),
            ("elapsed", ui.duration(time.monotonic() - started)),
        ])
    return 0


def _repo_dry_run(args: argparse.Namespace) -> int:
    """Clone, select and estimate without connecting to anything.

    Worth its own path rather than a flag deep inside the session: it needs no
    credentials at all, so it is the thing to run before committing an evening
    to a repository that turns out to be 90% lock files.
    """
    settings = Settings(host="dry-run", username="dry-run", password="")
    editor_safe = session.EDITORS[args.editor]["editor_safe"]

    with ui.spinner(f"Cloning {args.url}") as spin:
        checkout = codebase.clone(args.url)
        spin.update(f"Cloned {args.url}")

    try:
        with ui.spinner("Selecting files"):
            files, skipped = codebase.collect(
                checkout, max_file_bytes=args.max_file_bytes)
        if not files:
            raise codebase.CodebaseError(
                "No typable source files found. The repository may hold only "
                "binaries, oversized files, or extensions this does not type.")

        chosen, dropped, estimate = codebase.plan(
            files, settings, editor_safe, args.minutes * 60.0)
        whole = sum(codebase.seconds_for(f, settings, editor_safe) for f in files)

        if args.json:
            ui.emit(json.dumps({
                "url": args.url,
                "editor": args.editor,
                "budget_minutes": args.minutes,
                "candidates": len(files),
                "characters": sum(f.characters for f in files),
                "whole_repository_seconds": round(whole, 1),
                "selected": [{"path": f.relative, "lines": f.lines,
                              "characters": f.characters,
                              "seconds": round(codebase.seconds_for(
                                  f, settings, editor_safe), 1)}
                             for f in chosen],
                "dropped": [f.relative for f in dropped],
                "skipped": skipped,
                "estimated_seconds": round(estimate, 1),
            }, indent=2))
            return 0

        ui.title("Plan", f"{args.url}  ({args.editor})")
        ui.kv("candidates", f"{len(files)} file(s), "
                            f"{sum(f.characters for f in files):,} characters")
        ui.kv("whole repository", codebase.human_time(whole))
        ui.kv("budget", f"{args.minutes:g} min" if args.minutes > 0 else "no limit")
        ui.kv("this run", f"{len(chosen)} file(s), {codebase.human_time(estimate)}",
              value_style="bright_green")

        ui.heading("Files that would be typed")
        ui.table([(f.relative, f"{f.lines:,}", f"{f.characters:,}",
                   codebase.human_time(
                       codebase.seconds_for(f, settings, editor_safe)))
                  for f in chosen[:40]],
                 headers=("path", "lines", "chars", "time"))
        if len(chosen) > 40:
            ui.note(f"... and {len(chosen) - 40} more")

        if dropped:
            ui.heading(f"Left out by the budget ({len(dropped)})")
            for file in dropped[:10]:
                ui.bullet(ui.style(file.relative, "grey"))
            if len(dropped) > 10:
                ui.note(f"... and {len(dropped) - 10} more")
            ui.note("raise --minutes, or set it to 0, to include these")
        if skipped and ui.TERM.verbose:
            ui.heading(f"Skipped ({len(skipped)})")
            for line in skipped[:20]:
                ui.bullet(ui.style(line, "grey"))
        return 0
    finally:
        # codebase.clone puts the checkout inside a fresh temporary directory.
        shutil.rmtree(checkout.parent, ignore_errors=True)


# -------------------------------------------------------------------- config

_ENV_VARS = [
    ("RDP_HOST", "server hostname or IP"),
    ("RDP_USERNAME", "account to sign in as"),
    ("RDP_PASSWORD", "password; preferred over saving one"),
    ("RDP_DOMAIN", "Windows domain, if the account needs one"),
    ("RDP_PORT", "RDP port (default 3389)"),
    ("RDP_AUTH", "ntlm | kerberos | plain (default ntlm)"),
    ("RDP_WIDTH", "remote screen width (default 1280)"),
    ("RDP_HEIGHT", "remote screen height (default 800)"),
    ("RDP_PROFILE", "override where the saved profile lives"),
    ("RDP_CONNECT_TIMEOUT", "seconds per connection attempt (default 20)"),
    ("RDP_CONNECT_RETRIES", "attempts before giving up (default 3)"),
    ("RDP_RETRY_DELAY", "seconds between attempts (default 3)"),
    ("RDP_CHAR_DELAY", "seconds between characters (default 0.012)"),
    ("RDP_KEY_DELAY", "seconds between key down and up (default 0.03)"),
    ("RDP_ACTION_DELAY", "seconds after a click or chord (default 0.25)"),
    ("RDP_TYPE_MODE", "unicode | scancode (default unicode)"),
    ("RDP_KEYBOARD_LAYOUT", "layout for scancode mode (default enus)"),
    ("RDP_SCREENSHOT_DIR", "where screenshots are written"),
    ("RDP_STOP_FILE", "path to the emergency stop sentinel"),
    ("RDP_LOG_FILE", "also write the log here"),
    ("RDP_LOG_LEVEL", "DEBUG | INFO | WARNING | ERROR (default INFO)"),
    ("NO_COLOR", "set to anything to disable colour"),
]


def config(args: argparse.Namespace) -> int:
    action = getattr(args, "config_command", None) or "show"
    return {
        "show": _config_show,
        "path": _config_path,
        "env": _config_env,
        "set": _config_set,
        "forget": _config_forget,
    }[action](args)


def _config_show(args: argparse.Namespace) -> int:
    saved = credentials.load()
    has_password = credentials.has_saved_password()
    if args.json:
        payload = {key: saved.get(key, "") for key in credentials.PROFILE_FIELDS}
        payload["password_saved"] = has_password
        payload["path"] = str(credentials.profile_path())
        ui.emit(json.dumps(payload, indent=2))
        return 0

    path = credentials.profile_path()
    if not saved and not path.exists():
        ui.warn(f"nothing saved at {path}")
        ui.note("run `autordp config set` to remember a connection")
        return 0

    ui.title("Saved profile", str(path))
    for key in credentials.PROFILE_FIELDS:
        value = saved.get(key, "")
        ui.kv(key, value if value else ui.style("(not set)", "grey"))
    ui.kv("password", ui.style("saved, DPAPI-encrypted", "green") if has_password
          else ui.style("not saved", "grey"))
    return 0


def _config_path(args: argparse.Namespace) -> int:
    ui.emit(str(credentials.profile_path()))
    return 0


def _config_env(args: argparse.Namespace) -> int:
    if args.json:
        ui.emit(json.dumps({name: {"description": why, "set": name in os.environ}
                            for name, why in _ENV_VARS}, indent=2))
        return 0
    ui.title("Environment variables")
    ui.table([(name,
               ui.style("set", "green") if name in os.environ
               else ui.style("-", "grey"),
               why)
              for name, why in _ENV_VARS],
             headers=("variable", "", "meaning"))
    return 0


def _config_set(args: argparse.Namespace) -> int:
    """Prompt for the fields, then verify them by connecting before saving.

    Saving first and connecting later would be the easier code and the worse
    tool: a typo in the host would be remembered, and every later command would
    inherit it. Nothing is written unless the credentials actually work.
    """
    settings = context.resolve(args, prompt_missing=not args.no_input)
    setup_logging(settings)
    ui.say()
    with _client(settings) as (client, loop):
        _connect(client, loop)
        remember = args.remember_password or args.remember
        path = settings.remember(remember_password=remember)

    if path is None:
        ui.error("the connection worked but the profile could not be written")
        return 2
    ui.ok(f"saved to {path}")
    if remember:
        if credentials.has_saved_password():
            ui.note("password saved, encrypted for this Windows user (DPAPI)")
        else:
            ui.warn("password not saved: DPAPI is Windows-only. "
                    "Use RDP_PASSWORD elsewhere.")
    else:
        ui.note("password not saved -- pass --remember-password to keep it, "
                "or set RDP_PASSWORD")
    return 0


def _config_forget(args: argparse.Namespace) -> int:
    path = credentials.profile_path()
    if credentials.forget():
        ui.ok(f"removed {path}")
    else:
        ui.note(f"nothing saved at {path}")
    return 0


# -------------------------------------------------------------------- doctor

def doctor(args: argparse.Namespace) -> int:
    """Check everything a run depends on, and say what to do about each gap.

    The value is in the ordering: this walks the same path a real run takes --
    runtime, RDP stack, git, credentials, writable paths, then the network --
    so the first failure it reports is the first one a run would have hit.
    """
    checks: list[dict] = []

    def record(name: str, state: str, detail: str, fix: str = "") -> None:
        checks.append({"name": name, "status": state, "detail": detail, "fix": fix})

    frozen = getattr(sys, "frozen", False)
    record("runtime", "ok",
           f"autordp {__version__}, "
           + ("bundled binary" if frozen else f"python {sys.version.split()[0]}"))
    record("platform", "ok", f"{sys.platform} ({os.name})")

    try:
        import aardwolf  # noqa: F401

        record("rdp stack", "ok", f"aardwolf {_package_version('aardwolf')}")
    except ImportError as exc:
        record("rdp stack", "fail", str(exc), "pip install -r requirements.txt")

    # The single most valuable check in a packaged build. aardwolf reaches its
    # 400-odd keyboard layouts through importlib, which PyInstaller's static
    # analysis cannot see, so a binary missing them starts perfectly, connects
    # perfectly, and then fails on the first keystroke -- twenty minutes into a
    # run. Loading one here turns that into a one-second answer.
    layout_name = os.environ.get("RDP_KEYBOARD_LAYOUT", "enus")
    ok_layout, layout_detail = _check_layout(layout_name)
    record("keyboard layout", "ok" if ok_layout else "fail", layout_detail,
           "" if ok_layout else
           "the build is missing aardwolf's layout modules; rebuild with "
           "autordp.spec, which collects them explicitly")

    git = shutil.which("git")
    record("git", "ok" if git else "warn", git or "not on PATH",
           "" if git else "only needed by `autordp repo`")

    saved = credentials.load()
    if saved:
        record("profile", "ok",
               f"{saved.get('username', '?')}@{saved.get('host', '?')}"
               + (" (password saved)" if credentials.has_saved_password() else ""))
    else:
        record("profile", "warn", f"nothing at {credentials.profile_path()}",
               "run `autordp config set`")

    settings = None
    try:
        settings = context.resolve(args, prompt_missing=False)
        record("credentials", "ok", settings.target)
    except ConfigError as exc:
        record("credentials", "warn", str(exc).splitlines()[0],
               "pass --host/--user, or set RDP_PASSWORD")

    for label, path in (
        ("screenshot dir", settings.screenshot_dir if settings
         else Settings.screenshot_dir),
        ("stop file dir", settings.stop_file.parent if settings
         else Settings.stop_file.parent),
    ):
        writable, why = _writable(Path(path))
        record(label, "ok" if writable else "fail",
               f"{path}" + ("" if writable else f" -- {why}"),
               "" if writable else
               "set RDP_SCREENSHOT_DIR / RDP_STOP_FILE somewhere writable")

    if settings and settings.stop_file.exists():
        record("emergency stop", "fail",
               f"{settings.stop_file} exists, so all input would be blocked",
               f"delete {settings.stop_file}")

    # Probed from the host and port alone, not from resolved Settings. A missing
    # password makes Settings unbuildable, but it says nothing about whether the
    # server is reachable -- and "can I even see port 3389" is the single most
    # useful thing this command can answer.
    host, port = _target_from_any(args, saved)
    if not args.no_network and host:
        reachable, detail_text = _probe(host, port, 10.0)
        record("rdp port", "ok" if reachable else "fail", detail_text,
               "" if reachable else
               "check the server is up, RDP is enabled, and the firewall "
               f"allows TCP {port}")
    elif not host:
        record("rdp port", "warn", "no host to probe",
               "pass --host, or save one with `autordp config set`")

    record("terminal", "ok",
           f"colour {'on' if ui.TERM.color else 'off'}, "
           f"{'unicode' if ui.TERM.unicode else 'ascii'}, "
           f"{ui.TERM.width} columns")

    failures = [c for c in checks if c["status"] == "fail"]
    if args.json:
        ui.emit(json.dumps({"ok": not failures, "checks": checks}, indent=2))
        return 1 if failures else 0

    ui.title("Environment check")
    marks = {"ok": ui.style(ui.glyph("tick"), "green"),
             "warn": ui.style(ui.glyph("warn"), "yellow"),
             "fail": ui.style(ui.glyph("cross"), "red")}
    for check in checks:
        ui.say(f"  {marks[check['status']]} "
               f"{ui.style(check['name'].ljust(16), 'bold')} {check['detail']}")
        if check["fix"]:
            ui.note("  " + check["fix"])

    ui.say()
    if failures:
        ui.fail(f"{len(failures)} problem(s) would stop a run")
        return 1
    ui.ok("ready")
    return 0


def _check_layout(name: str) -> tuple[bool, str]:
    """Load a keyboard layout and use it, the way a real keystroke would."""
    try:
        from aardwolf.keyboard.layoutmanager import KeyboardLayoutManager

        layout = KeyboardLayoutManager().get_layout_by_shortname(name)
        if layout is None:
            return False, f"{name!r} is not a known layout short name"
        # Resolving one scancode proves the table is populated, not just that
        # the module object exists.
        layout.vk_to_scancode("VK_RETURN")
        return True, f"{name} loaded, tables populated"
    except Exception as exc:  # noqa: BLE001 - this is the check
        return False, f"{type(exc).__name__}: {exc}"


def _package_version(name: str) -> str:
    """The installed version of a dependency, or a best guess.

    ``importlib.metadata`` is the reliable route, but a PyInstaller build only
    carries distribution metadata for the packages the spec collected, so fall
    back to whatever attribute the module happens to expose.
    """
    try:
        from importlib.metadata import version

        return version(name)
    except Exception:  # noqa: BLE001 - includes PackageNotFoundError
        module = sys.modules.get(name)
        return str(getattr(module, "__version__", "version unknown"))


def _target_from_any(args: argparse.Namespace, saved: dict) -> tuple[str, int]:
    """Host and port from flags, environment or profile -- no password needed."""
    host = (getattr(args, "host", "") or os.environ.get("RDP_HOST", "")
            or saved.get("host", "")).strip()
    raw_port = (getattr(args, "port", "") or os.environ.get("RDP_PORT", "")
                or saved.get("port", "") or "3389")
    try:
        port = int(str(raw_port).strip())
    except ValueError:
        port = 3389
    return host, port


def _writable(path: Path) -> tuple[bool, str]:
    """Whether we could actually create files here, tested by doing it."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".autordp-write-test"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True, ""
    except OSError as exc:
        return False, exc.strerror or str(exc)


def _probe(host: str, port: int, timeout: float) -> tuple[bool, str]:
    """A plain TCP connect, to separate "unreachable" from "auth failed".

    Worth doing before a long run: a refused connection and a rejected password
    look similar in aardwolf's error, and this tells them apart in a second.
    """
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=min(timeout, 10.0)):
            elapsed = (time.monotonic() - started) * 1000
            return True, f"{host}:{port} answered in {elapsed:.0f}ms"
    except OSError as exc:
        return False, f"{host}:{port} -- {exc.strerror or exc}"


# ---------------------------------------------------------------------- keys

def keys(args: argparse.Namespace) -> int:
    names = sorted(KEY_ALIASES)
    if args.pattern:
        needle = args.pattern.lower()
        names = [n for n in names if needle in n.lower()]
        if not names:
            ui.warn(f"no key name contains {args.pattern!r}")
            return 0
    if args.json:
        ui.emit(json.dumps({n: KEY_ALIASES[n] for n in names}, indent=2))
        return 0

    ui.title("Key names", f"{len(names)} available")
    columns = max(1, ui.TERM.width // 14)
    for start in range(0, len(names), columns):
        ui.say("  " + "".join(ui.style(n.ljust(14), "cyan")
                              for n in names[start:start + columns]).rstrip())
    ui.say()
    ui.note("combine with '+' for a chord: ctrl+s, ctrl+shift+p, alt+F4")
    return 0


# ------------------------------------------------------------------- summary

def _summary(heading: str, rows: list[tuple[str, str]]) -> None:
    width = max(len(label) for label, _ in rows)
    ui.say()
    ui.box([f"{ui.style(label.ljust(width), 'grey')}  {value}"
            for label, value in rows], heading_text=heading)
