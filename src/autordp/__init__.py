"""autordp: RDP input automation over a client-owned session.

This opens its own RDP connection and drives the desktop inside it by sending
keyboard and mouse input events. It is a client in its own right, a peer of
``mstsc.exe`` -- not a robot moving the local pointer -- so the machine running
it is never touched and can be used normally while a run is in progress.

The command line front end is :mod:`autordp.cli`, reachable as ``autordp``
after installing, as ``python -m autordp`` from a checkout, or as the
single-file binary the npm packages ship. Everything else here is library code
underneath it.
"""

# The one place the version is written down. setuptools reads it via
# pyproject.toml's dynamic metadata, the CLI prints it, and scripts/build-npm.mjs
# stamps it into every package.json -- so a release is a one-line edit here.
__version__ = "0.2.1"

__all__ = ["__version__"]
