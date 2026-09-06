#!/usr/bin/env bash
# One-line installer for the autordp Linux binary.
#
#   curl -fsSL https://raw.githubusercontent.com/bewithsnehasish/autoRDP/main/install.sh | bash
#
# Downloads the latest release's `autordp-linux`, installs it as `autordp` on
# your PATH, and makes it executable. `autordp` runs the CLI; `autordp --gui`
# opens the desktop app (needs a display).
set -euo pipefail

REPO="bewithsnehasish/autoRDP"
ASSET="autordp-linux"
URL="https://github.com/${REPO}/releases/latest/download/${ASSET}"

arch="$(uname -m)"
if [ "$arch" != "x86_64" ]; then
  echo "autordp releases are built for x86_64; found '$arch'. Build from source instead." >&2
  exit 1
fi

# Prefer a system-wide dir if we can write it (with sudo), else a user dir.
if [ -w /usr/local/bin ] || command -v sudo >/dev/null 2>&1; then
  DEST="/usr/local/bin"; SUDO=""
  [ -w "$DEST" ] || SUDO="sudo"
else
  DEST="$HOME/.local/bin"; SUDO=""
  mkdir -p "$DEST"
fi

echo "Downloading autordp from the latest release..."
tmp="$(mktemp)"
curl -fSL "$URL" -o "$tmp"
chmod +x "$tmp"
$SUDO install -m 0755 "$tmp" "$DEST/autordp"
rm -f "$tmp"

echo
echo "Installed: $DEST/autordp"
if ! command -v autordp >/dev/null 2>&1; then
  echo "NOTE: $DEST is not on your PATH. Add it, e.g.:"
  echo "  echo 'export PATH=\"$DEST:\$PATH\"' >> ~/.bashrc && source ~/.bashrc"
fi
echo "Try:  autordp --help      (CLI)"
echo "      autordp --gui       (desktop app, needs a display)"
