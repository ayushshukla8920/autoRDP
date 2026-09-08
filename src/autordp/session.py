"""The codebase typing flow: clone a repository and type it into a remote editor.

One public coroutine, :func:`run_codebase_session`. It connects, waits for the
remote shell to be usable, and then drives an editor through as many of the
repository's files as the time budget allows. Disconnecting is left to the
caller, which owns the client.

Progress is reported two ways at once, and both matter. ``print`` gives a
running commentary that reads correctly in a log file or a cron mail; the
optional callbacks give a front end the numbers it needs to draw a progress
bar. Neither is required.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import time

from . import codebase
from .client import RdpClient
from .config import Settings

DEFAULT_REMOTE_DIR = r"C:\Users\Public\Documents"

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
        # VS Code opens a path that does not exist yet and creates it on save,
        # so no pre-creation is needed and Ctrl+S needs no dialog.
        #
        # --disable-extensions stops IntelliSense and auto-formatting from
        # rewriting text as it arrives; --disable-workspace-trust skips the
        # dialog that would otherwise swallow the first keystrokes. Both only
        # take effect when VS Code starts a NEW instance -- if it is already
        # running in the remote session, close it first.
        #
        # Note -r (--reuse-window), never -n. Every -n is another Electron
        # window at ~150 MB, so a hundred files would exhaust the server's
        # memory. -r sends the file to the window that is already open, and
        # Ctrl+W closes just that tab afterwards: one window, one tab at a
        # time, and no cold start per file.
        "launch": ('cmd /c code --disable-extensions '
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

TOTAL_STAGES = 6


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


async def run_codebase_session(client: RdpClient, args: argparse.Namespace,
                               url: str, budget_seconds: float,
                               on_file=None, on_chars=None,
                               on_plan=None) -> None:
    """Clone a repository, pick what fits the budget, connect, and type it.

    The callbacks are how a front end draws something better than the running
    commentary. All are optional:

    ``on_plan(chosen, dropped, estimate)``  once, after file selection
    ``on_file(index, total, source)``       before each file is typed
    ``on_chars(count)``                     per line typed, for a progress bar
    """
    settings = client.settings
    profile = EDITORS[args.editor]

    step(1, f"Cloning {url}")
    # Cloning and walking the tree are blocking work; keep them off the loop.
    checkout = await asyncio.to_thread(codebase.clone, url)
    print(f"    into {checkout}")

    step(2, "Selecting files")
    files, skipped = await asyncio.to_thread(
        codebase.collect, checkout, getattr(args, "max_file_bytes", 20_000))
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
    if on_plan is not None:
        on_plan(chosen, dropped, estimate)

    step(4, f"Connecting to {settings.target}")
    await client.connect()
    print("    Connected successfully")
    settings.remember(remember_password=args.remember)

    # Nothing may be typed until the shell is up: Win+R is handled by Explorer,
    # so on a session this young it would simply be dropped. A brand-new
    # session takes seconds to paint a desktop at all.
    step(5, "Waiting for the remote desktop to finish starting up")
    settled = await client.wait_for_desktop(timeout=args.startup_timeout)
    print("    desktop has settled" if settled
          else "    gave up waiting; continuing on the configured delays")
    await _settle(args.startup_wait)

    remote_root = f"{args.remote_dir.rstrip(chr(92))}\\{codebase.slug(url)}"
    step(6, f"Typing into {remote_root}")
    await type_codebase(client, args, chosen, remote_root,
                        on_file=on_file, on_chars=on_chars)


async def type_codebase(client: RdpClient, args: argparse.Namespace,
                        files: list, remote_root: str,
                        on_file=None, on_chars=None) -> None:
    """Type each file of a cloned repository into the remote editor."""
    settings = client.settings
    sender = client.input
    profile = EDITORS[args.editor]
    editor_wait = (args.editor_wait if args.editor_wait is not None
                   else profile["wait"])
    made_dirs: set[str] = set()
    done = 0

    for index, source in enumerate(files, start=1):
        remote_path = f"{remote_root}\\{source.remote_relative}"
        parent = remote_path.rsplit("\\", 1)[0]

        print(f"\n[{index}/{len(files)}] {source.relative} "
              f"({source.lines} lines, {source.characters} chars)")
        if on_file is not None:
            on_file(index, len(files), source)

        # Notepad's Save As cannot create missing folders, so make them first.
        # VS Code creates them itself when saving, so it needs nothing here.
        if profile["save_as"] and parent not in made_dirs:
            await run_via_run_dialog(sender, f'cmd /c mkdir "{parent}"',
                                     args.dialog_wait)
            made_dirs.add(parent)
            await _settle(1.0)

        # The first file pays the editor's cold start; after that the launch
        # command reuses the instance that is already running, so it only needs
        # a moment to bring the file up.
        launch = profile["launch"].format(path=remote_path)
        wait = editor_wait if index == 1 else profile["relaunch_wait"]
        # Log what goes into the Run dialog: it is fire-and-forget, so this is
        # the only record of what was actually asked for if a launch fails.
        if index == 1:
            print(f"      Win+R, then: {launch}")
        await run_via_run_dialog(sender, launch, args.dialog_wait)
        await _settle(wait)

        if profile["focus_click"]:
            await sender.click(int(settings.width * 0.55),
                               int(settings.height * 0.5))
        if profile["select_all"]:
            await sender.chord("ctrl+a")
            await sender.key("DELETE")

        started = time.monotonic()
        with pacing(settings, args.action_delay):
            await sender.type_lines(source.text,
                                    editor_safe=profile["editor_safe"],
                                    on_progress=on_chars)
        print(f"      typed in {time.monotonic() - started:.0f}s, saving")

        await sender.chord("ctrl+s")
        if profile["save_as"]:
            await _settle(args.dialog_wait + 1.0)
            # The file name box has focus when the dialog opens. Quoting the
            # path stops the dialog appending .txt from the "Text Documents"
            # filter.
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
