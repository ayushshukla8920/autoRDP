# PyInstaller spec for the `autordp` binary -- the artifact the npm platform
# packages ship.
#
#   python -m PyInstaller autordp.spec --noconfirm
#
# One file, always. npm packages are far easier to reason about when they hold
# exactly one thing, and a CLI's startup is dominated by the RDP handshake
# anyway, so the archive unpacking a one-file build does on every launch is not
# the bottleneck it would be for a GUI.
#
# The hidden imports below are not optional, and the build silently half-works
# without them:
#
# 1. aardwolf loads keyboard layouts with
#        importlib.import_module('aardwolf.keyboard.layouts.layout_KBDUSX')
#    A dynamic import like that is invisible to PyInstaller's static analysis,
#    so none of the 400-odd layout modules would be bundled. The binary would
#    start, connect, and then fail on the first keystroke with "Unknown
#    keyboard layout 'enus'" -- possibly twenty minutes into a run.
#    collect_submodules fixes it, and `autordp doctor` asserts it afterwards.
#
# 2. unicrypto chooses a crypto backend at import time, also with __import__:
#        unicrypto.backends.pycryptodomex -> from Cryptodome.Util import Counter
#    Without Cryptodome collected, the binary dies on startup with
#    "cannot import name 'Counter' from 'Cryptodome.Util'".
#
# 3. cryptography, arc4 and aardwolf's Rust RLE extension ship compiled
#    binaries; collect_all picks up their DLLs and metadata along with the
#    Python modules. The metadata is also what lets `doctor` report the
#    aardwolf version rather than "unknown".

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [], [], []

for package in ("aardwolf", "asyauth", "asysocks", "minikerberos", "unicrypto",
                "cryptography", "arc4", "asn1tools", "bitstruct", "Cryptodome"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

hiddenimports += collect_submodules("aardwolf.keyboard.layouts")
hiddenimports += collect_submodules("unicrypto.backends")

# collect_all is thorough to a fault: it also hands us every test suite and
# example inside those packages. Dropping them here rather than through
# `excludes` avoids fighting the hidden imports it just added.
DEAD_WEIGHT = ("Cryptodome.SelfTest", "aardwolf.examples", "aardwolf.develstuff")
hiddenimports = [m for m in hiddenimports if not m.startswith(DEAD_WEIGHT)]
datas = [d for d in datas
         if not any(part in d[1].replace("\\", "/") for part in
                    ("Cryptodome/SelfTest", "aardwolf/examples"))]

analysis = Analysis(
    ["src/autordp/__main__.py"],
    # src layout: the package is not next to the spec, so the analysis has to
    # be told where to find it.
    pathex=[str(Path(SPECPATH, "src").resolve())],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # `unittest` is deliberately absent from this list even though it looks
    # like obvious dead weight: asn1tools -> pyparsing -> pyparsing.testing
    # imports it at module scope, so excluding it breaks the build at startup
    # with ModuleNotFoundError. These are safe.
    #
    # tkinter goes, and with it several megabytes from every platform package:
    # there is no GUI any more, and nothing here can import it.
    #
    # PIL stays. client.py asks aardwolf for VIDEO_FORMAT.PIL frames, so every
    # screenshot and every live-view frame goes through it.
    excludes=["tkinter", "tkinter.test", "pydoc_data", "PIL.ImageTk",
              "PIL._tkinter_finder", "test"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

executable = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    name="autordp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX is off deliberately: packed executables are flagged by antivirus far
    # more often, and this one is downloaded by `npm install` onto machines
    # whose owners did not choose to trust it.
    upx=False,
    console=True,
    runtime_tmpdir=None,
    disable_windowed_traceback=False,
    # Only Windows uses the .ico, and only Windows has one to use.
    icon="assets/autordp.ico" if sys.platform == "win32" else None,
)
