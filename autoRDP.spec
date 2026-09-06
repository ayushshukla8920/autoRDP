# PyInstaller spec for autoRDP.exe -- the windowed GUI build.
#
# The entry point is pyinstaller_entry.py (which calls rdpauto.gui.main), so the
# console `rdpauto.cli` is unreachable and not bundled: the packaged first-party
# code is the `rdpauto` package under src/. On a headless server you run
# `python -m rdpauto.cli` from a checkout instead.
#
#   .\.venv\Scripts\python.exe -m PyInstaller autoRDP.spec --noconfirm
#
# Two things here are not optional, and the build silently half-works without
# them:
#
# 1. aardwolf loads keyboard layouts with
#        importlib.import_module('aardwolf.keyboard.layouts.layout_KBDUSX')
#    A dynamic import like that is invisible to PyInstaller's static analysis,
#    so none of the 400-odd layout modules would be bundled. The app would
#    start, connect, and then fail on the first keystroke with "Unknown
#    keyboard layout 'enus'". collect_submodules fixes it.
#
# 2. unicrypto chooses a crypto backend at import time, also with __import__:
#        unicrypto.backends.pycryptodomex -> from Cryptodome.Util import Counter
#    Without Cryptodome collected, the exe dies on startup with
#    "cannot import name 'Counter' from 'Cryptodome.Util'" -- long before any
#    RDP traffic, because gui.py imports rdp_client at module scope.
#
# 3. cryptography, arc4 and aardwolf's Rust RLE extension ship compiled
#    binaries; collect_all picks up their DLLs and metadata along with the
#    Python modules.

# One file or one folder?
#
#   True  -> a single 32 MB autoRDP.exe you can hand to anyone. Windows unpacks
#            the whole archive to a temp directory on every launch: measured
#            ~12 seconds to first window (3 runs, 13.9/11.0/11.2).
#   False -> dist/autoRDP/autoRDP.exe plus its DLLs in a 66 MB folder, and
#            ~1 second to first window (1.2/0.9/1.1). Ship the folder, or zip
#            it. This is the better choice unless a single file really matters.
ONEFILE = True

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [], [], []

# Everything aardwolf needs, including the dynamically imported layouts.
for package in ("aardwolf", "asyauth", "asysocks", "minikerberos", "unicrypto",
                "cryptography", "arc4", "asn1tools", "bitstruct",
                "Cryptodome"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

hiddenimports += collect_submodules("aardwolf.keyboard.layouts")
hiddenimports += collect_submodules("unicrypto.backends")

# collect_all is thorough to a fault: it also hands us every test suite and
# example inside those packages. Dropping them here (rather than via
# `excludes`, which would then fight the hidden imports it just added) keeps
# the archive smaller and the build log readable.
DEAD_WEIGHT = ("Cryptodome.SelfTest", "aardwolf.examples", "aardwolf.develstuff")
hiddenimports = [m for m in hiddenimports if not m.startswith(DEAD_WEIGHT)]
datas = [d for d in datas
         if not any(part in d[1].replace("\\", "/") for part in
                    ("Cryptodome/SelfTest", "aardwolf/examples"))]

# Imported at runtime by our own code rather than at module scope.
hiddenimports += ["PIL.ImageTk", "PIL._tkinter_finder"]

# The logo PNG the tkinter windows load at runtime (the titlebar .ico is set
# separately below, via the `icon=` option).
datas += [("assets/autoRDP-256.png", "assets")]

analysis = Analysis(
    ["pyinstaller_entry.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    # Keep this list careful. `unittest` looks like dead weight in a GUI app,
    # but asn1tools -> pyparsing -> pyparsing.testing imports it at module
    # scope, so excluding it breaks the build with ModuleNotFoundError at
    # startup. These four are safe: they are test suites and sample code that
    # collect_all drags in, and nothing here imports them.
    # A few test suites / build tooling that no runtime path imports. Kept
    # conservative: `unittest`, `distutils` and `setuptools` are pulled in by
    # pyparsing/cryptography and excluding them breaks the build.
    excludes=["pydoc_data", "tkinter.test", "unittest.test", "lib2to3"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

common = dict(
    name="autoRDP",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX-packed exes get flagged by antivirus far more often
    console=False,      # windowed: no console flashes behind the GUI
    disable_windowed_traceback=False,
    icon="assets/autoRDP.ico",
)

if ONEFILE:
    executable = EXE(pyz, analysis.scripts, analysis.binaries, analysis.datas,
                     runtime_tmpdir=None, **common)
else:
    executable = EXE(pyz, analysis.scripts, exclude_binaries=True, **common)
    collected = COLLECT(executable, analysis.binaries, analysis.datas,
                        strip=False, upx=False, name="autoRDP")
