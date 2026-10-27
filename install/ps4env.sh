#!/usr/bin/env bash
# Source this before building the PS4 app:
#     source install/ps4env.sh
#
# Sets OO_PS4_TOOLCHAIN and puts the toolchain's Linux tools on PATH.
# Override the location with:  OO_PS4_TOOLCHAIN=/your/path source install/ps4env.sh

: "${OO_PS4_TOOLCHAIN:=/home/ziad/openorbis}"
export OO_PS4_TOOLCHAIN

if [ ! -d "$OO_PS4_TOOLCHAIN" ]; then
    echo "ps4env: toolchain dir not found: $OO_PS4_TOOLCHAIN" >&2
    echo "ps4env: run install/setup-fedora.sh first, or set OO_PS4_TOOLCHAIN." >&2
    return 1 2>/dev/null || exit 1
fi

export PATH="$OO_PS4_TOOLCHAIN/bin/linux:$PATH"

# PkgTool.Core is an old self-contained .NET app: it needs ICU (modern distros
# ship an incompatible version) and OpenSSL 1.1 for pkg_build. These two lines
# make the sfo_* verbs work on modern Fedora; pkg_build still needs libssl1.1
# (use install/build-ps4.sh, which does packaging in a container).
export DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1

echo "OO_PS4_TOOLCHAIN=$OO_PS4_TOOLCHAIN"
command -v clang  >/dev/null || echo "ps4env: WARNING: clang not found (install clang, or use install/build-ps4.sh)"
command -v ld.lld >/dev/null || echo "ps4env: WARNING: ld.lld not found (install lld, or use install/build-ps4.sh)"
