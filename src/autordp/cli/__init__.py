"""The command line front end.

``main.main`` is the entry point for all three ways in: the ``autordp`` script
installed by pip, ``python -m autordp``, and the single-file binary the npm
packages ship.
"""

from .main import main

__all__ = ["main"]
