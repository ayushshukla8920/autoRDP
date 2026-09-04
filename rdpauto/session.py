"""The session flows both front ends drive.

``run_session`` types a generated file into one editor; ``run_codebase_session``
clones a repository and types its files. Both connect, wait for the remote
shell, and report progress with ``print`` so the console and the GUI log pane
show the same thing. Disconnecting is left to the caller.

The whole sequence runs inside the RDP session this process opened. The local
desktop is never touched.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
import time
from datetime import datetime

from . import codebase
from .code_generator import generate
from .config import Settings
from .input_events import EmergencyStopped, InputError, SessionNotAcceptingInput
from .console import LoopThread
from .rdp_client import ConnectionFailed, RdpClient

DEFAULT_REMOTE_DIR = r"C:\Users\Public\Documents"


def default_filename() -> str:
    """A fresh name per run, so Save As never hits an overwrite prompt."""
    return "automation_demo_" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".py"


# Launch commands are kept as simple as possible, because everything typed here
# goes through the Run dialog and there is no way to see an error from it. An
# earlier version pre-created the file with
#     cmd /c type nul > "{path}" && notepad "{path}"
# which is a trap: if the redirection fails (no write permission on the folder,
# say) the && short-circuits, the editor never launches, and the cmd window
# flashes and closes leaving nothing on screen to explain why.
EDITORS = {
    "notepad": {
        # Just `notepad` -- no path, no cmd, no redirection. This opens an
        # Untitled buffer, and Ctrl+S then gives a Save As dialog where the
        # full path is typed. Passing a path that does not exist would instead
        # raise a "cannot find the file, create it?" prompt whose wording and
        # buttons vary between Windows builds.
        "launch": "notepad",
        "launch_repeat": "notepad",
        "save_as": True,
        # Notepad does not auto-indent or auto-close brackets, so the
        # Home/Shift+End guard is unnecessary -- and skipping it removes four
        # keystrokes per line, which makes typing markedly faster.
        "editor_safe": False,
        # Notepad opens with focus already in the text area. Clicking blind is
        # the riskier option: if the window is smaller than expected the click
        # lands in whatever is behind it, and the typing goes there instead.
        "focus_click": False,
        "select_all": False,
        # Notepad is cheap to start, so closing it after each file and opening
        # a fresh Untitled is both simple and tidy.
        "close_after": True,
        "close_tab": False,
        "wait": 4.0,
        "relaunch_wait": 2.5,
    },
    "code": {
        # VS Code happily opens a path that does not exist yet and creates it
        # on save, so no pre-creation is needed and Ctrl+S needs no dialog.
        #
        # --disable-extensions stops IntelliSense and auto-formatting from
        # rewriting text as it arrives; --disable-workspace-trust skips the
        # dialog that would otherwise swallow the first keystrokes. Both only
        # take effect when VS Code starts a NEW instance -- if it is already
        # running in the remote session, close it first.
        "launch": ('cmd /c code --disable-extensions --disable-workspace-trust '
                   '-n -g "{path}"'),
        # Typing a whole repository must NOT use -n. Every -n is another
        # Electron window at ~150 MB, so a hundred files would exhaust the
        # server's memory. -r (--reuse-window) sends the file to the window
        # that is already open, and Ctrl+W closes just that tab afterwards --
        # one window, one tab at a time, and no cold start per file.
        "launch_repeat": ('cmd /c code --disable-extensions '
                          '--disable-workspace-trust -r -g "{path}"'),
        "save_as": False,
        "editor_safe": True,
        "focus_click": True,
        "select_all": True,
        # Never Alt+F4 the window: that would kill the instance and force a
        # 12-second cold start for the next file. Close the tab instead.
        "close_after": False,
        "close_tab": True,
        "wait": 12.0,
        "relaunch_wait": 3.0,
    },
}


def step(number: int, text: str) -> None:
    print(f"[{number}] {text}", flush=True)


@contextlib.contextmanager
def pacing(settings: Settings, action_delay: float):
    """Temporarily tighten the post-action delay.

    Menus and dialogs need the configured settle time, but the per-line editor
    keystrokes do not -- keeping the slow delay there would make typing a
    40-line file take minutes.
    """
    original = settings.action_delay
    settings.action_delay = action_delay
    try:
        yield
    finally:
        settings.action_delay = original


async def _settle(seconds: float) -> None:
    """Give the remote UI time to catch up before the next keystroke."""
    await asyncio.sleep(seconds)


async def run_via_run_dialog(sender, command: str, settle: float) -> None:
    """Launch a command in the remote session through Win+R."""
    await sender.chord("win+r")
    await _settle(settle)
    await sender.type_text(command)
    await sender.key("ENTER")


async def run_session(client: RdpClient, args: argparse.Namespace) -> None:
    """Connect, wait for the session to be usable, then run the demo.

    Everything is reported with ``print``, so any front end that captures
    stdout -- a console or the tkinter log window -- shows the same progress.
    Disconnecting is left to the caller, which owns the client.
    """
    settings = client.settings

    step(1, f"Connecting to {settings.target}")
    await client.connect()
    print("    Connected successfully")
    settings.remember(remember_password=args.remember)

    # Nothing may be typed until the shell is up: Win+R is handled by Explorer,
    # so on a session this young it would simply be dropped. A brand-new
    # session takes seconds to paint a desktop at all.
    step(2, "Waiting for the remote desktop to finish starting up")
    settled = await client.wait_for_desktop(timeout=args.startup_timeout)
    print("    desktop has settled" if settled
          else "    gave up waiting; continuing on the configured delays")
    await _settle(args.startup_wait)

    step(3, "Capturing the initial screen")
    if args.screenshots:
        await client.screenshot(settings.screenshot_dir / "demo-0-before.png")
    else:
        print("    skipped (--no-screenshots)")

    await run_demo(client, args)


async def run_codebase_session(client: RdpClient, args: argparse.Namespace,
                               url: str, budget_seconds: float) -> None:
    """Clone a repository, pick what fits the budget, connect, and type it.

    Shared by both front ends: the GUI runs it on its loop thread, the CLI on
    the main one. Everything is reported with ``print`` so both see the same
    progress.
    """
    settings = client.settings
    profile = EDITORS[args.editor]

    step(1, f"Cloning {url}")
    # Cloning and walking the tree are blocking work; keep them off the loop.
    checkout = await asyncio.to_thread(codebase.clone, url)
    print(f"    into {checkout}")

    step(2, "Selecting files")
    files, skipped = await asyncio.to_thread(codebase.collect, checkout)
    if not files:
        raise codebase.CodebaseError(
            "No typable source files found. The repository may contain only "
            "binaries, very large files, or unsupported extensions.")
    print(f"    {len(files)} candidate file(s), "
          f"{sum(f.characters for f in files):,} characters")
    if skipped:
        print(f"    {len(skipped)} skipped, first few:")
        for line in skipped[:5]:
            print(f"      - {line}")

    chosen, dropped, estimate = codebase.plan(
        files, settings, profile["editor_safe"], budget_seconds)
    whole = sum(codebase.seconds_for(f, settings, profile["editor_safe"])
                for f in files)
    step(3, "Estimating")
    print(f"    Typing runs at roughly 20-25 characters a second, so the whole "
          f"repository would take {codebase.human_time(whole)}.")
    if budget_seconds > 0:
        print(f"    Budget is {budget_seconds / 60:.0f} min: typing "
              f"{len(chosen)} file(s), about {codebase.human_time(estimate)}.")
    else:
        print(f"    No budget set: typing all {len(chosen)} file(s), "
              f"about {codebase.human_time(estimate)}.")
    if dropped:
        print(f"    {len(dropped)} file(s) left out; raise the budget "
              f"(or set it to 0) to include more.")

    step(4, f"Connecting to {settings.target}")
    await client.connect()
    print("    Connected successfully")
    settings.remember(remember_password=args.remember)

    step(5, "Waiting for the remote desktop to finish starting up")
    settled = await client.wait_for_desktop(timeout=args.startup_timeout)
    print("    desktop has settled" if settled
          else "    gave up waiting; continuing on the configured delays")
    await _settle(args.startup_wait)

    remote_root = f"{args.remote_dir.rstrip(chr(92))}\\{codebase.slug(url)}"
    step(6, f"Typing into {remote_root}")
    await type_codebase(client, args, chosen, remote_root)


async def type_codebase(client: RdpClient, args: argparse.Namespace,
                        files: list, remote_root: str) -> None:
    """Type each file of a cloned repository into the remote editor."""
    settings = client.settings
    sender = client.input
    profile = EDITORS[args.editor]
    editor_wait = args.editor_wait if args.editor_wait is not None else profile["wait"]
    made_dirs: set[str] = set()
    done = 0

    for index, source in enumerate(files, start=1):
        remote_path = f"{remote_root}\\{source.remote_relative}"
        parent = remote_path.rsplit("\\", 1)[0]

        print(f"\n[{index}/{len(files)}] {source.relative} "
              f"({source.lines} lines, {source.characters} chars)")

        # Notepad's Save As cannot create missing folders, so make them first.
        # VS Code creates them itself when saving, so it needs nothing here.
        if profile["save_as"] and parent not in made_dirs:
            await run_via_run_dialog(sender, f'cmd /c mkdir "{parent}"', args.dialog_wait)
            made_dirs.add(parent)
            await _settle(1.0)

        # The first file pays the editor's cold start; after that the launch
        # command reuses the instance that is already running, so it only needs
        # a moment to bring the file up.
        launch = profile["launch_repeat"].format(path=remote_path)
        wait = editor_wait if index == 1 else profile["relaunch_wait"]
        # Log what goes into the Run dialog: it is fire-and-forget, so this is
        # the only record of what was actually asked for if a launch fails.
        if index == 1:
            print(f"      Win+R, then: {launch}")
        await run_via_run_dialog(sender, launch, args.dialog_wait)
        await _settle(wait)

        if profile["focus_click"]:
            await sender.click(int(settings.width * 0.55), int(settings.height * 0.5))
        if profile["select_all"]:
            await sender.chord("ctrl+a")
            await sender.key("DELETE")

        started = time.monotonic()
        with pacing(settings, args.action_delay):
            await sender.type_lines(source.text, editor_safe=profile["editor_safe"])
        print(f"      typed in {time.monotonic() - started:.0f}s, saving")

        await sender.chord("ctrl+s")
        if profile["save_as"]:
            await _settle(args.dialog_wait + 1.0)
            await sender.type_text(f'"{remote_path}"')
            await sender.key("ENTER")
        await _settle(2.0)

        # Leave nothing behind before the next file. Which of these is right
        # depends on the editor: closing a Notepad window costs nothing, but
        # closing a VS Code window would kill the instance and make every
        # subsequent file pay a cold start, so there we close only the tab.
        if profile["close_tab"]:
            await sender.chord("ctrl+w")
            await _settle(1.0)
        elif profile["close_after"]:
            await sender.chord("alt+F4")
            await _settle(1.5)

        done += 1
        if args.screenshots and (index == 1 or index == len(files)):
            await client.screenshot(
                settings.screenshot_dir / f"codebase-{index:03d}.png")

    print(f"\nTyped {done} file(s) into {remote_root}")


async def run_demo(client: RdpClient, args: argparse.Namespace) -> None:
    settings = client.settings
    sender = client.input
    profile = EDITORS[args.editor]
    filename = args.filename or default_filename()
    remote_path = f"{args.remote_dir.rstrip(chr(92))}\\{filename}"
    editor_wait = args.editor_wait if args.editor_wait is not None else profile["wait"]

    step(4, f"Generating a harmless Python module (seed={args.seed})")
    source = generate(seed=args.seed, how_many=args.functions)
    line_count = source.count("\n")
    print(f"    {line_count} lines, {len(source)} characters")

    launch = profile["launch"].format(path=remote_path)
    step(5, f"Launching {args.editor} via the Run dialog")
    print(f"    Win+R, then: {launch}")
    await run_via_run_dialog(sender, launch, args.dialog_wait)
    print(f"    waiting {editor_wait:g}s for the editor to come up")
    await _settle(editor_wait)
    if args.screenshots:
        await client.screenshot(settings.screenshot_dir / "demo-1-editor.png")

    if profile["focus_click"] or profile["select_all"]:
        step(6, "Focusing the editor and clearing the buffer")
        if profile["focus_click"]:
            await sender.click(int(settings.width * 0.55), int(settings.height * 0.5))
        if profile["select_all"]:
            await sender.chord("ctrl+a")
            await sender.key("DELETE")
    else:
        step(6, "Editor already has focus on an empty file")

    step(7, f"Typing {line_count} lines into the editor")
    started = time.monotonic()
    with pacing(settings, args.action_delay):
        await sender.type_lines(source, editor_safe=profile["editor_safe"])
    print(f"    typed in {time.monotonic() - started:.1f}s")

    if profile["save_as"]:
        step(8, f"Saving via the Save As dialog to {remote_path}")
        await sender.chord("ctrl+s")
        await _settle(args.dialog_wait + 1.0)
        # The file name box has focus when the dialog opens. Quoting the path
        # stops the dialog appending .txt from the "Text Documents" filter.
        await sender.type_text(f'"{remote_path}"')
        await sender.key("ENTER")
        await _settle(2.0)
    else:
        step(8, "Saving the file (Ctrl+S)")
        await sender.chord("ctrl+s")
        await _settle(1.5)

    if args.screenshots:
        await client.screenshot(settings.screenshot_dir / "demo-2-typed.png")

    if args.run:
        step(9, "Running the file")
        # cmd /k keeps the window open so the output stays on screen for the
        # screenshot. VS Code gets its integrated terminal via the palette.
        if args.editor == "code":
            await sender.chord("ctrl+shift+p")
            await _settle(1.0)
            await sender.type_text("Terminal: Create New Terminal")
            await _settle(0.8)
            await sender.key("ENTER")
            await _settle(args.terminal_wait)
            await sender.type_text(f'python "{remote_path}"')
            await sender.key("ENTER")
        else:
            await run_via_run_dialog(
                sender, f'cmd /k python "{remote_path}"', args.dialog_wait)
        await _settle(args.terminal_wait)
        if args.screenshots:
            await client.screenshot(settings.screenshot_dir / "demo-3-output.png")
            print("    check demo-3-output.png for the program output")
    else:
        step(9, "Not running the file (pass --run to execute it)")

    print(f"\n    the typed file is at {remote_path} in the remote session")
    step(10, "Done")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Type generated Python into an editor inside a remote RDP session.")
    parser.add_argument("--editor", choices=sorted(EDITORS), default="notepad",
                        help="which remote editor to drive (default notepad)")
    parser.add_argument("--seed", type=int, default=None,
                        help="seed for the generated code, for a repeatable run")
    parser.add_argument("--functions", type=int, default=3,
                        help="how many functions to generate (default 3)")
    parser.add_argument("--remote-dir", default=DEFAULT_REMOTE_DIR,
                        help=f"remote directory for the file (default {DEFAULT_REMOTE_DIR})")
    parser.add_argument("--filename", default=None,
                        help="remote file name (default automation_demo_<timestamp>.py)")
    parser.add_argument("--run", action="store_true",
                        help="execute the file after saving it (off by default)")
    parser.add_argument("--no-screenshots", dest="screenshots", action="store_false",
                        help="skip the progress screenshots")
    parser.add_argument("--editor-wait", type=float, default=None,
                        help="seconds to wait for the editor to start "
                             "(default: 4 for notepad, 12 for code)")
    parser.add_argument("--startup-timeout", type=float, default=45.0,
                        help="max seconds to wait for the remote shell to settle "
                             "(default 45)")
    parser.add_argument("--startup-wait", type=float, default=2.0,
                        help="extra seconds after the desktop settles (default 2)")
    parser.add_argument("--dialog-wait", type=float, default=1.5,
                        help="seconds to wait for the Run dialog (default 1.5)")
    parser.add_argument("--terminal-wait", type=float, default=6.0,
                        help="seconds to wait for the run to finish (default 6)")
    parser.add_argument("--action-delay", type=float, default=0.05,
                        help="post-keystroke delay while typing code (default 0.05)")
    parser.add_argument("--gui", action="store_true",
                        help="collect the connection details in a tkinter form")
    parser.add_argument("--remember", action="store_true",
                        help="also remember the password (encrypted for this Windows user)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    print("rdp-background-automation - Phase 2 editor demo")
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

    print(f"Emergency stop: create {settings.stop_file.name} here, or press Ctrl+C\n")

    loop = LoopThread()
    loop.start()
    trip = lambda: client.stop.trip("Ctrl+C")  # noqa: E731
    try:
        loop.run(run_session(client, args), on_interrupt=trip)
        print("\nDemo finished.")
        return 0

    except KeyboardInterrupt:
        print("\nInterrupted. Disconnecting.", file=sys.stderr)
        return 130
    except ConnectionFailed as exc:
        print(f"\nConnection failed: {exc}", file=sys.stderr)
        return 1
    except EmergencyStopped as exc:
        print(f"\nEmergency stop: {exc}", file=sys.stderr)
        return 3
    except SessionNotAcceptingInput as exc:
        print(f"\nThe session stopped accepting input: {exc}", file=sys.stderr)
        return 4
    except InputError as exc:
        print(f"\nInput error: {exc}", file=sys.stderr)
        return 5
    finally:
        step(11, "Disconnecting")
        try:
            loop.run(client.disconnect())
        except Exception as exc:  # noqa: BLE001
            print(f"Warning: unclean disconnect: {exc}", file=sys.stderr)
        loop.close()


if __name__ == "__main__":
    raise SystemExit(
        "rdpauto/session.py is part of the application, not an entry point. "
        "Start it with:  python gui.py   (desktop)  or  python cli.py   (server)"
    )
