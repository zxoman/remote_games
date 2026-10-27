#!/usr/bin/env bash
# Fedora setup for the PS4 side of Reverse Remote Play.
#
# Installs the build dependencies (needs sudo) and downloads the OpenOrbis
# PS4 toolchain into $TOOLCHAIN_DIR (no sudo needed for that part).
#
# Usage:
#     bash install/setup-fedora.sh
#
# Afterwards, build the app with:
#     source install/ps4env.sh
#     cd ps4 && make
set -euo pipefail

TOOLCHAIN_DIR="${OO_PS4_TOOLCHAIN:-$HOME/openorbis}"
TOOLCHAIN_VERSION="v0.5.4"
TARBALL_URL="https://github.com/OpenOrbis/OpenOrbis-PS4-Toolchain/releases/download/${TOOLCHAIN_VERSION}/toolchain-llvm-18.tar.gz"

echo "== Reverse Remote Play: PS4 toolchain setup =="
echo "   toolchain dir : $TOOLCHAIN_DIR"
echo "   version       : $TOOLCHAIN_VERSION"
echo

# --- 1. System dependencies (requires sudo) ---------------------------------
if command -v clang >/dev/null && command -v ld.lld >/dev/null && command -v make >/dev/null; then
    echo "[1/3] clang, lld, make already present — skipping dnf."
else
    echo "[1/3] Installing build dependencies via dnf (needs sudo)..."
    if command -v dnf >/dev/null; then
        sudo dnf install -y clang lld llvm make cmake git python3
    elif command -v apt >/dev/null; then
        sudo apt update && sudo apt install -y clang lld make cmake git python3
    else
        echo "Unknown package manager. Install clang, lld, make, git manually." >&2
    fi
fi

# --- 2. OpenOrbis toolchain --------------------------------------------------
if [ -d "$TOOLCHAIN_DIR/include/orbis" ]; then
    echo "[2/3] Toolchain already present at $TOOLCHAIN_DIR — skipping download."
else
    echo "[2/3] Downloading OpenOrbis toolchain (~150 MB)..."
    tmp="$(mktemp -d)"
    curl -L --fail -o "$tmp/toolchain.tar.gz" "$TARBALL_URL"
    echo "      extracting..."
    tar -xzf "$tmp/toolchain.tar.gz" -C "$tmp"
    mkdir -p "$(dirname "$TOOLCHAIN_DIR")"
    rm -rf "$TOOLCHAIN_DIR"
    mv "$tmp/OpenOrbis/PS4Toolchain" "$TOOLCHAIN_DIR"
    rm -rf "$tmp"
fi

# --- 3. Environment hint -----------------------------------------------------
echo "[3/3] Done."
echo
echo "Add this to ~/.bashrc so it persists (or just source it per shell):"
echo "    export OO_PS4_TOOLCHAIN=$TOOLCHAIN_DIR"
echo
echo "Then:"
echo "    source install/ps4env.sh"
echo "    cd ps4 && make"
