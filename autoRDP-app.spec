# Combined binary: ONE `autordp` that is the CLI by default and the GUI with
# `--gui`/`--live` (see pyinstaller_app_entry.py). Bundles tkinter + the logo so
# the desktop path works; console=True so the CLI path prints normally.
#
#   Windows:  python -m PyInstaller autoRDP-app.spec --noconfirm   -> dist\autordp.exe
#   Linux:    pyinstaller autoRDP-app.spec --noconfirm             -> dist/autordp
#             (needs tkinter at build time: apt-get install -y python3-tk)
#
# The two dynamic-import gotchas are handled exactly as in autoRDP.spec:
#   1. aardwolf keyboard layouts (collect_submodules) else "Unknown layout 'enus'"
#   2. unicrypto/Cryptodome backend (collect_all) else dies on startup

ONEFILE = True

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [], [], []

for package in ("aardwolf", "asyauth", "asysocks", "minikerberos", "unicrypto",
                "cryptography", "arc4", "asn1tools", "bitstruct",
                "Cryptodome"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

hiddenimports += collect_submodules("aardwolf.keyboard.layouts")
hiddenimports += collect_submodules("unicrypto.backends")

DEAD_WEIGHT = ("Cryptodome.SelfTest", "aardwolf.examples", "aardwolf.develstuff")
hiddenimports = [m for m in hiddenimports if not m.startswith(DEAD_WEIGHT)]
datas = [d for d in datas
         if not any(part in d[1].replace("\\", "/") for part in
                    ("Cryptodome/SelfTest", "aardwolf/examples"))]

# The GUI path needs these (imported at runtime) and the logo the windows load.
hiddenimports += ["PIL.ImageTk", "PIL._tkinter_finder"]
datas += [("assets/autoRDP-256.png", "assets")]

analysis = Analysis(
    ["pyinstaller_app_entry.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["pydoc_data", "tkinter.test", "unittest.test", "lib2to3"],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

common = dict(
    name="autordp",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # CLI prints; GUI still opens its own window
    disable_windowed_traceback=False,
    icon="assets/autoRDP.ico",
)

if ONEFILE:
    executable = EXE(pyz, analysis.scripts, analysis.binaries, analysis.datas,
                     runtime_tmpdir=None, **common)
else:
    executable = EXE(pyz, analysis.scripts, exclude_binaries=True, **common)
    collected = COLLECT(executable, analysis.binaries, analysis.datas,
                        strip=False, upx=False, name="autordp")
