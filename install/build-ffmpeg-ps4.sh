#!/usr/bin/env bash
# Cross-compile a minimal FFmpeg (H.264 decoder only) for the OpenOrbis PS4
# toolchain. This backs the Phase 3 H.264 video path (ps4/src/h264_dec.c).
#
# Produces, inside the repo:
#   ps4/third_party/ffmpeg/include/{libavcodec,libavutil,libswscale}/*.h
#   ps4/third_party/ffmpeg/lib/{libavcodec.a,libavutil.a,libswscale.a}
#
# Why a container? FFmpeg's build needs clang + lld + make for the PS4 target,
# and we do not want to install those on the host. No root required (podman).
#
# Notes:
#   * Only the H.264 decoder + parser and libswscale (YUV420/NV12 -> RGB24) are
#     enabled. Everything else (muxers, demuxers, filters, network, ...) is off,
#     which keeps the static archives small.
#   * x86 asm is DISABLED: NASM output for the freebsd12-elf target is fiddly to
#     cross-assemble. The plain C path is enough for a 720p prototype.
#   * pthreads are disabled to match the proven Moonlight-ps4 build; the decoder
#     therefore runs single-threaded (fine at 720p30).
#
# Usage:  bash install/build-ffmpeg-ps4.sh [--force]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$REPO_ROOT/ps4/third_party/ffmpeg"
TOOLCHAIN_DIR="${OO_PS4_TOOLCHAIN:-$HOME/openorbis}"
LD_WRAPPER="$REPO_ROOT/install/ps4-ld.sh"

VER="${FFMPEG_VERSION:-n5.1.6}"
IMAGE="localhost/rpbuilder:ffmpeg"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/remote_games"
SRC_TARBALL="$CACHE/ffmpeg-$VER.tar.gz"
SRC_DIR="$CACHE/FFmpeg-$VER"
BUILD_DIR="$CACHE/ffmpeg-build-$VER"

FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

if [ "$FORCE" -eq 0 ] && [ -f "$DEST/lib/libavcodec.a" ]; then
    echo ">> $DEST/lib/libavcodec.a already exists (use --force to rebuild)"
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
    echo ">> downloading FFmpeg $VER ..."
    curl -fL --retry 3 -o "$SRC_TARBALL" \
        "https://github.com/FFmpeg/FFmpeg/archive/refs/tags/${VER}.tar.gz"
fi
[ -d "$SRC_DIR" ] || tar xzf "$SRC_TARBALL" -C "$CACHE"

if ! podman image exists "$IMAGE"; then
    echo ">> building image $IMAGE (one-time)..."
    podman run --name rpb_ffmpeg_boot docker.io/library/alpine:latest \
        sh -c 'apk add --no-cache clang lld make cmake nasm bash perl pkgconf binutils'
    podman commit rpb_ffmpeg_boot "$IMAGE" >/dev/null
    podman rm rpb_ffmpeg_boot >/dev/null
fi

mkdir -p "$BUILD_DIR"

echo ">> configuring + building FFmpeg (H.264 decoder + libswscale, no asm)..."
podman run --rm --security-opt label=disable \
    -v "$TOOLCHAIN_DIR:/opt/openorbis:ro" \
    -v "$SRC_DIR:/src:ro" \
    -v "$BUILD_DIR:/build" \
    -v "$LD_WRAPPER:/usr/local/bin/ps4-ld:ro" \
    -e OO_PS4_TOOLCHAIN=/opt/openorbis \
    -w /build \
    "$IMAGE" \
    bash -c '
        set -e
        rm -rf /build/* /build/.[!.]* 2>/dev/null || true
        /src/configure \
            --prefix=/build/install \
            --enable-cross-compile \
            --arch=x86_64 --target-os=freebsd \
            --cc=clang --cxx=clang++ \
            --ld=/usr/local/bin/ps4-ld \
            --ar=ar --ranlib=ranlib --nm=nm --strip=strip \
            --extra-cflags="--target=x86_64-pc-freebsd12-elf -fPIC -funwind-tables -isysroot /opt/openorbis -isystem /opt/openorbis/include -D__PS4__ -D__ORBIS__ -O2" \
            --disable-shared --enable-static \
            --disable-programs --disable-doc --disable-debug \
            --disable-avdevice --disable-avformat --disable-avfilter \
            --disable-swresample --disable-postproc --disable-network \
            --disable-iconv --disable-zlib --disable-bzlib \
            --disable-everything \
            --enable-avcodec --enable-avutil --enable-swscale \
            --enable-decoder=h264 --enable-parser=h264 \
            --disable-asm --disable-x86asm --disable-inline-asm \
            --enable-pic \
            --disable-pthreads --disable-w32threads --disable-os2threads >/build/configure.log 2>&1 || { tail -40 /build/configure.log; exit 1; }
        sed -i "s|#define HAVE_SYSCTL 1|//#define HAVE_SYSCTL 1|" config.h || true
        make -j"$(nproc)" >/build/make.log 2>&1 || { tail -40 /build/make.log; exit 1; }
        make install >/build/install.log 2>&1
    '

mkdir -p "$DEST/lib" "$DEST/include"
cp "$BUILD_DIR"/install/lib/*.a "$DEST/lib/"
rm -rf "$DEST/include/libavcodec" "$DEST/include/libavutil" "$DEST/include/libswscale"
cp -r "$BUILD_DIR"/install/include/libavcodec "$DEST/include/"
cp -r "$BUILD_DIR"/install/include/libavutil "$DEST/include/"
cp -r "$BUILD_DIR"/install/include/libswscale "$DEST/include/"

echo ">> installed:"
ls -la "$DEST/lib"/*.a
