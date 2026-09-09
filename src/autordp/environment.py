"""What a child process should inherit from a frozen build.

PyInstaller's one-file bootloader unpacks the archive into a temporary
directory and edits the environment so the program it starts finds the bundled
copy of everything. Those edits are right for *this* process and wrong for
every process it starts, in two different ways -- so both a re-exec of
ourselves (``daemon.spawn``) and a call out to a system binary
(``codebase.clone``) want the environment put back first.

Nothing here has any effect from a source checkout or a pip install: there is
no bootloader, so there is nothing to undo.
"""

from __future__ import annotations

import os
import sys

# The bootloader sets these to tell a *second* stage "already unpacked, reuse
# it". A detached child that inherits them skips extraction and runs out of the
# parent's directory -- which the parent deletes when it exits a second later.
# The child then dies on the next lazy import with
#
#     FileNotFoundError: /tmp/_MEIxxxxxx/base_library.zip
#
# It is a race, so it looks intermittent: whether it survives depends on
# whether anything still needed importing after the parent went away.
#
# `_MEIPASS2` is the pre-6.x name, the `_PYI_*` ones are current. Clearing all
# of them makes the child unpack its own copy, which it then owns and cleans up
# itself. They mean nothing to a child that is not itself a PyInstaller build,
# so clearing them unconditionally costs nothing.
_BOOTLOADER_VARS = (
    "_MEIPASS2",
    "_PYI_ARCHIVE_FILE",
    "_PYI_APPLICATION_HOME_DIR",
    "_PYI_PARENT_PROCESS_LEVEL",
    "_PYI_SPLASH_IPC",
)

# The bootloader also points the dynamic linker at its temporary directory,
# which holds the libraries bundled with this build -- libcrypto and libssl
# among them, pulled in by `cryptography` and `Cryptodome`.
#
# Two things go wrong if a child inherits that. A re-exec is aimed at a
# directory that is about to be deleted. Worse, a *system* binary is handed
# the bundled libraries instead of the ones it was built against: that is what
# breaks `autordp codebase` on a Linux or macOS binary, because git runs
# git-remote-https, which loads the bundled libcrypto, fails a symbol lookup
# and dies before it speaks a word of protocol. git reports only the silence:
#
#     fatal: remote helper 'https' aborted session
#
# The bootloader stashes whatever was there before under `<NAME>_ORIG`, so
# undoing the edit is exact rather than a guess. Windows is unaffected -- the
# bundled directory goes on the DLL search path, not into the environment --
# but the restore is harmless there and the list stays platform-agnostic.
_LIBRARY_PATH_VARS = ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH",
                      "DYLD_FRAMEWORK_PATH", "LIBPATH")


def system_environment() -> dict[str, str]:
    """This process's environment as it was before the bootloader touched it.

    The environment to give any child process: another copy of this program,
    or a system binary such as ``git``.
    """
    environment = dict(os.environ)
    if not getattr(sys, "frozen", False):
        return environment

    for name in _BOOTLOADER_VARS:
        environment.pop(name, None)
    for name in _LIBRARY_PATH_VARS:
        original = environment.pop(f"{name}_ORIG", None)
        if original is not None:
            environment[name] = original
        else:
            environment.pop(name, None)
    return environment
