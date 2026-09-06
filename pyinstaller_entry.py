"""PyInstaller entry point for the windowed build.

The GUI now lives in the ``rdpauto.gui`` package (under ``src/``), and
PyInstaller needs a plain script to start from. This is that script; the spec
adds ``src`` to the path so ``rdpauto`` imports.
"""

import sys

from rdpauto.gui import main

if __name__ == "__main__":
    sys.exit(main())
