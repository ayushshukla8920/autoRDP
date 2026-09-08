"""Entry point for ``python -m autordp``, and for the PyInstaller build.

The import is absolute rather than ``from .cli.main import ...`` on purpose.
PyInstaller runs this file as a top-level script, not as a submodule, so a
relative import raises "attempted relative import with no known parent package"
and the binary dies before printing anything. The absolute form works under
both ``-m`` and the frozen build.
"""

import sys

from autordp.cli.main import main

if __name__ == "__main__":
    sys.exit(main())
