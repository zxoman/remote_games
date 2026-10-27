#!/usr/bin/env bash
# Cross-compile libjpeg-turbo for the OpenOrbis PS4 toolchain.
#
# Produces, inside the repo:
#   ps4/third_party/libjpeg/include/{jpeglib.h,jconfig.h,jmorecfg.h,jerror.h}
#   ps4/third_party/libjpeg/lib/libjpeg.a
#
# Why a container? We need cmake + clang + lld + binutils for the PS4 target, and
# we do not want to install those on the host. No root required (rootless podman).
#
# Notes:
#   * SIMD is DISABLED (WITH_SIMD=0): libjpeg-turbo's x86 SIMD needs NASM output
#     for the freebsd12-elf target, which is fiddly to cross-assemble. The plain
#     C path is plenty for a 720p30 MJPEG prototype.
#   * Only the static library target is built; the cjpeg/djpeg tools are skipped
#     because they cannot link as PS4 executables.
#
# Usage:  bash install/build-libjpeg-ps4.sh [--force]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$REPO_ROOT/ps4/third_party/libjpeg"
TOOLCHAIN_DIR="${OO_PS4_TOOLCHAIN:-$HOME/openorbis}"

VER="3.0.4"
IMAGE="localhost/rpbuilder:cmake"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/remote_games"
SRC_TARBALL="$CACHE/libjpeg-turbo-$VER.tar.gz"
SRC_DIR="$CACHE/libjpeg-turbo-$VER"
BUILD_DIR="$CACHE/libjpeg-build-$VER"

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

if [ "$FORCE" -eq 0 ] && [ -f "$DEST/lib/libjpeg.a" ]; then
    echo ">> $DEST/lib/libjpeg.a already exists (use --force to rebuild)"
    exit 0
fi

if ! command -v podman >/dev/null; then
    echo "podman is required." >&2
    exit 1
fi
if [ ! -d "$TOOLCHAIN_DIR/include/orbis" ]; then
    echo "OpenOrbis toolchain not found at $TOOLCHAIN_DIR" >&2
    echo "Run: bash install/setup-fedora.sh" >&2
    exit 1
fi

mkdir -p "$CACHE"

if [ ! -f "$SRC_TARBALL" ]; then
    echo ">> downloading libjpeg-turbo $VER ..."
    curl -fL --retry 3 -o "$SRC_TARBALL" \
        "https://github.com/libjpeg-turbo/libjpeg-turbo/releases/download/$VER/libjpeg-turbo-$VER.tar.gz"
fi
[ -d "$SRC_DIR" ] || tar xzf "$SRC_TARBALL" -C "$CACHE"

if ! podman image exists "$IMAGE"; then
    echo ">> building image $IMAGE (one-time)..."
    podman run --name rpb_cmake_boot docker.io/library/alpine:latest \
        sh -c 'apk add --no-cache clang lld make cmake nasm binutils'
    podman commit rpb_cmake_boot "$IMAGE" >/dev/null
    podman rm rpb_cmake_boot >/dev/null
fi

mkdir -p "$BUILD_DIR" "$DEST/include" "$DEST/lib"

echo ">> configuring + building libjpeg.a (static, no SIMD)..."
podman run --rm --security-opt label=disable \
    -v "$TOOLCHAIN_DIR:/opt/openorbis:ro" \
    -v "$SRC_DIR:/src:ro" \
    -v "$BUILD_DIR:/build" \
    -e OO_PS4_TOOLCHAIN=/opt/openorbis \
    -w /build \
    "$IMAGE" \
    sh -c '
        set -e
        rm -rf /build/* /build/.[!.]* 2>/dev/null || true
        cmake -G "Unix Makefiles" /src \
            -DCMAKE_C_COMPILER=clang \
            -DCMAKE_SYSTEM_NAME=Generic \
            -DCMAKE_SYSTEM_PROCESSOR=x86_64 \
            -DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY \
            -DCMAKE_C_FLAGS="--target=x86_64-pc-freebsd12-elf -fPIC -isysroot /opt/openorbis -isystem /opt/openorbis/include" \
            -DENABLE_SHARED=0 -DWITH_SIMD=0 -DWITH_TURBOJPEG=0 \
            -DWITH_JAVA=0 \
            -DCMAKE_INSTALL_PREFIX=/build/install >/dev/null
        make -j"$(nproc)" jpeg-static >/dev/null
        echo "built $(ls -l /build/libjpeg.a | awk "{print \$5}") bytes"
    '

cp "$BUILD_DIR/libjpeg.a" "$DEST/lib/libjpeg.a"
cp "$SRC_DIR/jpeglib.h" "$SRC_DIR/jmorecfg.h" "$SRC_DIR/jerror.h" "$DEST/include/"
cp "$BUILD_DIR/jconfig.h" "$DEST/include/"

echo ">> installed:"
ls -la "$DEST/lib/libjpeg.a" "$DEST/include"
