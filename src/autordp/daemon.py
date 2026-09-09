"""Running detached, so a session outlives the shell that started it.

``autordp connect -d`` re-launches itself in the background and returns the
prompt immediately. The child has no controlling terminal, so closing the SSH
connection does not take it with it, and its output goes to a log file instead
of a screen nobody is watching.

This is what removes the need for pm2 or nohup for an ad-hoc run. It is *not* a
substitute for a service manager: a detached process does not come back after a
reboot, and nothing restarts it if it dies. ``deploy/autordp.service`` covers
that case.

**Re-launch, not fork.** The obvious implementation is the classic double-fork,
and it only works on POSIX. Spawning a fresh copy of the program with
platform-appropriate creation flags works identically on Linux, macOS and
Windows, and it sidesteps the whole class of bugs where a forked child inherits
a half-initialised event loop or an open RDP socket -- both of which this
program has by the time it could decide to detach.

Detachment is one flag apart per platform:

    POSIX     start_new_session=True -> setsid(), no controlling terminal,
              so the SIGHUP sent when the SSH session ends is never delivered
    Windows   DETACHED_PROCESS, plus CREATE_NEW_PROCESS_GROUP so a Ctrl+C in
              the parent console is not delivered to the child as well

That difference decides how the daemon is stopped, too: POSIX takes SIGTERM,
Windows takes the emergency stop file, because a process with no console
cannot receive a console control event. :func:`terminate` explains why.
"""

from __future__ import annotations

import errno
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .environment import system_environment

# The environment marker that tells a child it is the detached copy. Without
# it the child would read `-d` from its own argv and detach again, forever.
MARKER = "AUTORDP_DETACHED"

DEFAULT_PID_NAME = "autordp.pid"
DEFAULT_LOG_NAME = "autordp.log"


def is_child() -> bool:
    return os.environ.get(MARKER) == "1"


def pid_path(explicit: str | None = None) -> Path:
    """Where the running daemon records itself. Beside STOP, in the cwd."""
    return Path(explicit) if explicit else Path.cwd() / DEFAULT_PID_NAME


def log_path(explicit: str | None = None) -> Path:
    return Path(explicit) if explicit else Path.cwd() / DEFAULT_LOG_NAME


# ------------------------------------------------------------------- spawning

def relaunch_command(argv: list[str]) -> list[str]:
    """The command that runs this program again with ``argv``.

    A PyInstaller build is its own interpreter, so ``sys.executable`` is the
    whole command. From a checkout or a pip install it is a Python that needs
    ``-m autordp`` to find its way back here.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, *argv]
    return [sys.executable, "-m", "autordp", *argv]


def child_argv(argv: list[str], drop: tuple[str, ...], add: list[str]) -> list[str]:
    """``argv`` with the detach flags removed and the daemon flags added."""
    kept = [token for token in argv if token not in drop]
    return kept + [token for token in add if token not in kept]


def child_environment() -> dict[str, str]:
    """The environment for the detached copy of this program.

    ``system_environment`` undoes the frozen build's edits to the environment,
    without which the child either races the parent's temporary directory to
    deletion or is aimed at it after the fact. The marker is what stops the
    child reading `-d` from its own argv and detaching again, forever.
    """
    environment = system_environment()
    environment[MARKER] = "1"
    return environment


def spawn(argv: list[str], log: Path) -> int:
    """Start the detached child and return its pid.

    The log is opened in append mode by *this* process and handed over as the
    child's stdout and stderr. Letting the child open it instead would lose
    whatever it printed before it got that far -- which is exactly the part
    worth reading when a detached run fails immediately.
    """
    log.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log, "ab", buffering=0)  # noqa: SIM115 - closed below
    try:
        handle.write(f"\n=== autordp detached at {time.strftime('%Y-%m-%d %H:%M:%S')} "
                     f"===\n".encode())

        environment = child_environment()
        keywords: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": handle,
            "stderr": subprocess.STDOUT,
            "env": environment,
            "cwd": str(Path.cwd()),
            "close_fds": True,
        }
        if os.name == "nt":
            # DETACHED_PROCESS: no console at all, so it does not die with
            # the one that started it. CREATE_NEW_PROCESS_GROUP: so a Ctrl+C
            # in the parent's console is not delivered to it as well.
            #
            # Having no console is also why `stop` cannot signal it on
            # Windows and uses the stop file instead -- see terminate().
            keywords["creationflags"] = (0x00000008 | 0x00000200)
        else:
            # setsid(): a new session with no controlling terminal, so the
            # SIGHUP that ends an SSH login never reaches it.
            keywords["start_new_session"] = True

        child = subprocess.Popen(relaunch_command(argv), **keywords)
        return child.pid
    finally:
        handle.close()


# ------------------------------------------------------------------ liveness

#: Options whose value must never be written down or printed back.
_SECRET_OPTIONS = ("--password",)


def redact(argv: list[str]) -> list[str]:
    """``argv`` with secret values replaced, for storing and for display.

    The recorded command line is genuinely useful -- it is how `status` tells
    you which run this is -- but recording it verbatim put `--password hunter2`
    in a plain JSON file and printed it back on screen. Both forms argparse
    accepts have to be handled: the separate value and the ``--password=x``
    spelling.
    """
    safe: list[str] = []
    skip_next = False
    for token in argv:
        if skip_next:
            safe.append("***")
            skip_next = False
            continue
        if token in _SECRET_OPTIONS:
            safe.append(token)
            skip_next = True
            continue
        matched = next((o for o in _SECRET_OPTIONS if token.startswith(o + "=")),
                       None)
        if matched:
            safe.append(f"{matched}=***")
            continue
        safe.append(token)
    return safe


def write_record(path: Path, pid: int, argv: list[str], log: Path,
                 view: int | None = None) -> None:
    """Record the daemon, as JSON rather than a bare pid.

    A pid alone is not enough to be sure: pids are recycled, so a stale file
    can name a process that now belongs to something else entirely. Storing
    the start time and the command line lets :func:`is_running` tell the
    difference before it reports -- or worse, kills -- the wrong thing.

    The command line is redacted on the way in, not on the way out, so a
    secret never reaches the disk in the first place.
    """
    record = {
        "pid": pid,
        "started": time.time(),
        "argv": redact(argv),
        "log": str(log),
        "view": view,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    # The record can still name a host and a username, and it sits in whatever
    # directory the run was started from.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def read_record(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("pid") else None


def is_running(pid: int) -> bool:
    """Whether this pid is a live process we could signal."""
    if pid <= 0:
        return False
    if os.name == "nt":
        return _running_windows(pid)
    try:
        os.kill(pid, 0)  # signal 0 tests for existence and permission
    except OSError as exc:
        # EPERM means it exists but belongs to someone else, which still counts
        # as running -- reporting "not running" there would be a lie.
        return exc.errno == errno.EPERM
    return True


def _running_windows(pid: int) -> bool:
    import ctypes

    SYNCHRONIZE = 0x00100000
    STILL_ACTIVE = 259
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.OpenProcess(SYNCHRONIZE | 0x0400, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return True
    finally:
        kernel32.CloseHandle(handle)


def clear(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


# ------------------------------------------------------------------ stopping

def terminate(pid: int, stop_file: Path | None = None,
              timeout: float = 20.0) -> bool:
    """Ask the daemon to stop, and wait for it. True once it is gone.

    Both routes end in the same clean shutdown -- the RDP session is
    disconnected rather than abandoned -- but which one is available depends
    entirely on the platform:

    **POSIX** gets SIGTERM, which ``--hold`` traps directly.

    **Windows** cannot be signalled at all here, and it is worth being precise
    about why. ``os.kill`` with ``CTRL_BREAK_EVENT`` is a call to
    ``GenerateConsoleCtrlEvent``, which only reaches processes attached to the
    *same console* -- and the child was deliberately created with
    ``DETACHED_PROCESS``, so it has no console for the event to travel through.
    Detachment and console signals are mutually exclusive. So Windows uses the
    emergency stop file, which ``--hold`` polls once a second and which unwinds
    through exactly the same disconnect.
    """
    if not is_running(pid):
        return True

    delivered = False
    if os.name != "nt":
        delivered = _signal(pid)
    if not delivered and stop_file is not None:
        stop_file.parent.mkdir(parents=True, exist_ok=True)
        stop_file.write_text("stop requested by `autordp stop`\n", encoding="utf-8")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_running(pid):
            return True
        time.sleep(0.25)
    return not is_running(pid)


def _signal(pid: int) -> bool:
    """Deliver SIGTERM. False if it could not be sent. POSIX only."""
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except (OSError, AttributeError, ValueError):
        return False


def kill(pid: int) -> bool:
    """Last resort, when the daemon will not wind itself down."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, check=False)
        else:
            os.kill(pid, signal.SIGKILL)
        return True
    except (OSError, AttributeError, ValueError):
        return False
