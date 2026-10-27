#!/usr/bin/env bash
# Host/CI PS4 build: no podman required. Uses host clang/lld/cmake and Docker
# only for PkgTool.Core (needs OpenSSL 1.1).
#
# Usage (GitHub Actions or a Linux box with clang, lld, make, cmake, docker):
#     bash install/ci-build-ps4.sh
#
# Output: ps4/IV0000-BREW00300_00-REMOTEPLAYREV000.pkg
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PS4_DIR="$REPO_ROOT/ps4"
TOOLCHAIN_DIR="${OO_PS4_TOOLCHAIN:-$HOME/openorbis}"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/remote_games"
LD_WRAPPER="$REPO_ROOT/install/ps4-ld.sh"

CONTENT_ID="IV0000-BREW00300_00-REMOTEPLAYREV000"
TITLE="Reverse Remote Play"
TITLE_ID="BREW00300"
VERSION="1.00"
TOOLCHAIN_VERSION="v0.5.4"
TARBALL_URL="https://github.com/OpenOrbis/OpenOrbis-PS4-Toolchain/releases/download/${TOOLCHAIN_VERSION}/toolchain-llvm-18.tar.gz"

need() { command -v "$1" >/dev/null || { echo "missing required tool: $1" >&2; exit 1; }; }
need clang; need clang++; need ld.lld; need make; need cmake; need curl; need tar; need docker

export OO_PS4_TOOLCHAIN="$TOOLCHAIN_DIR"
export DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1
export PATH="$TOOLCHAIN_DIR/bin/linux:$PATH"

mkdir -p "$CACHE"

# --- toolchain --------------------------------------------------------------
if [ ! -d "$TOOLCHAIN_DIR/include/orbis" ]; then
    echo ">> downloading OpenOrbis ${TOOLCHAIN_VERSION}..."
    tmp="$(mktemp -d)"
    curl -L --fail --retry 3 -o "$tmp/toolchain.tar.gz" "$TARBALL_URL"
    tar -xzf "$tmp/toolchain.tar.gz" -C "$tmp"
    mkdir -p "$(dirname "$TOOLCHAIN_DIR")"
    rm -rf "$TOOLCHAIN_DIR"
    mv "$tmp/OpenOrbis/PS4Toolchain" "$TOOLCHAIN_DIR"
    rm -rf "$tmp"
fi

# --- libjpeg-turbo (static, no SIMD) ----------------------------------------
JPEG_DEST="$PS4_DIR/third_party/libjpeg"
JPEG_VER="3.0.4"
if [ ! -f "$JPEG_DEST/lib/libjpeg.a" ]; then
    echo ">> building libjpeg-turbo ${JPEG_VER}..."
    JPEG_TGZ="$CACHE/libjpeg-turbo-$JPEG_VER.tar.gz"
    JPEG_SRC="$CACHE/libjpeg-turbo-$JPEG_VER"
    JPEG_BUILD="$CACHE/libjpeg-build-$JPEG_VER"
    if [ ! -f "$JPEG_TGZ" ]; then
        curl -fL --retry 3 -o "$JPEG_TGZ" \
            "https://github.com/libjpeg-turbo/libjpeg-turbo/releases/download/$JPEG_VER/libjpeg-turbo-$JPEG_VER.tar.gz"
    fi
    [ -d "$JPEG_SRC" ] || tar xzf "$JPEG_TGZ" -C "$CACHE"
    rm -rf "$JPEG_BUILD"
    mkdir -p "$JPEG_BUILD" "$JPEG_DEST/include" "$JPEG_DEST/lib"
    cmake -G "Unix Makefiles" -S "$JPEG_SRC" -B "$JPEG_BUILD" \
        -DCMAKE_C_COMPILER=clang \
        -DCMAKE_SYSTEM_NAME=Generic \
        -DCMAKE_SYSTEM_PROCESSOR=x86_64 \
        -DCMAKE_TRY_COMPILE_TARGET_TYPE=STATIC_LIBRARY \
        -DCMAKE_C_FLAGS="--target=x86_64-pc-freebsd12-elf -fPIC -isysroot $TOOLCHAIN_DIR -isystem $TOOLCHAIN_DIR/include" \
        -DENABLE_SHARED=0 -DWITH_SIMD=0 -DWITH_TURBOJPEG=0 -DWITH_JAVA=0
    cmake --build "$JPEG_BUILD" --target jpeg-static -j"$(nproc)"
    cp "$JPEG_BUILD/libjpeg.a" "$JPEG_DEST/lib/libjpeg.a"
    cp "$JPEG_SRC/jpeglib.h" "$JPEG_SRC/jmorecfg.h" "$JPEG_SRC/jerror.h" "$JPEG_DEST/include/"
    cp "$JPEG_BUILD/jconfig.h" "$JPEG_DEST/include/"
fi

# --- FFmpeg H.264 decoder + swscale -----------------------------------------
FF_DEST="$PS4_DIR/third_party/ffmpeg"
FF_VER="${FFMPEG_VERSION:-n5.1.6}"
if [ ! -f "$FF_DEST/lib/libavcodec.a" ]; then
    echo ">> building FFmpeg ${FF_VER} (H.264 decoder)..."
    FF_TGZ="$CACHE/ffmpeg-$FF_VER.tar.gz"
    FF_SRC="$CACHE/FFmpeg-$FF_VER"
    FF_BUILD="$CACHE/ffmpeg-build-$FF_VER"
    if [ ! -f "$FF_TGZ" ]; then
        curl -fL --retry 3 -o "$FF_TGZ" \
            "https://github.com/FFmpeg/FFmpeg/archive/refs/tags/${FF_VER}.tar.gz"
    fi
    [ -d "$FF_SRC" ] || tar xzf "$FF_TGZ" -C "$CACHE"
    rm -rf "$FF_BUILD"
    mkdir -p "$FF_BUILD"
    chmod +x "$LD_WRAPPER"
    (
        cd "$FF_BUILD"
        "$FF_SRC/configure" \
            --prefix="$FF_BUILD/install" \
            --enable-cross-compile \
            --arch=x86_64 --target-os=freebsd \
            --cc=clang --cxx=clang++ \
            --ld="$LD_WRAPPER" \
            --ar=ar --ranlib=ranlib --nm=nm --strip=strip \
            --extra-cflags="--target=x86_64-pc-freebsd12-elf -fPIC -funwind-tables -isysroot $TOOLCHAIN_DIR -isystem $TOOLCHAIN_DIR/include -D__PS4__ -D__ORBIS__ -O2" \
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
            --disable-pthreads --disable-w32threads --disable-os2threads
        sed -i 's|#define HAVE_SYSCTL 1|//#define HAVE_SYSCTL 1|' config.h || true
        make -j"$(nproc)"
        make install
    )
    mkdir -p "$FF_DEST/lib" "$FF_DEST/include"
    cp "$FF_BUILD"/install/lib/*.a "$FF_DEST/lib/"
    rm -rf "$FF_DEST/include/libavcodec" "$FF_DEST/include/libavutil" "$FF_DEST/include/libswscale"
    cp -r "$FF_BUILD"/install/include/libavcodec "$FF_DEST/include/"
    cp -r "$FF_BUILD"/install/include/libavutil "$FF_DEST/include/"
    cp -r "$FF_BUILD"/install/include/libswscale "$FF_DEST/include/"
fi

# --- compile + link ---------------------------------------------------------
echo ">> compiling eboot.bin..."
make -C "$PS4_DIR" clean >/dev/null 2>&1 || true
make -C "$PS4_DIR" eboot.bin

# --- package (OpenSSL 1.1 via Ubuntu 20.04) ---------------------------------
echo ">> packaging PKG (docker ubuntu:20.04)..."
docker run --rm \
    -e DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 \
    -v "$TOOLCHAIN_DIR:/opt/openorbis:ro" \
    -v "$REPO_ROOT:/work" \
    -w /work/ps4 \
    ubuntu:20.04 \
    bash -c '
      set -e
      apt-get update -qq
      DEBIAN_FRONTEND=noninteractive apt-get install -y -qq libssl1.1 libicu66 >/dev/null
      P=/opt/openorbis/bin/linux/PkgTool.Core
      rm -f sce_sys/param.sfo pkg.gp4 *.pkg
      $P sfo_new sce_sys/param.sfo
      $P sfo_setentry sce_sys/param.sfo APP_TYPE --type Integer --maxsize 4 --value 1
      $P sfo_setentry sce_sys/param.sfo APP_VER --type Utf8 --maxsize 8 --value "'"$VERSION"'"
      $P sfo_setentry sce_sys/param.sfo ATTRIBUTE --type Integer --maxsize 4 --value 0
      $P sfo_setentry sce_sys/param.sfo CATEGORY --type Utf8 --maxsize 4 --value gd
      $P sfo_setentry sce_sys/param.sfo CONTENT_ID --type Utf8 --maxsize 48 --value "'"$CONTENT_ID"'"
      $P sfo_setentry sce_sys/param.sfo DOWNLOAD_DATA_SIZE --type Integer --maxsize 4 --value 0
      $P sfo_setentry sce_sys/param.sfo SYSTEM_VER --type Integer --maxsize 4 --value 0
      $P sfo_setentry sce_sys/param.sfo TITLE --type Utf8 --maxsize 128 --value "'"$TITLE"'"
      $P sfo_setentry sce_sys/param.sfo TITLE_ID --type Utf8 --maxsize 12 --value "'"$TITLE_ID"'"
      $P sfo_setentry sce_sys/param.sfo VERSION --type Utf8 --maxsize 8 --value "'"$VERSION"'"
      /opt/openorbis/bin/linux/create-gp4 -out pkg.gp4 --content-id='"$CONTENT_ID"' \
        --files "eboot.bin sce_sys/about/right.sprx sce_sys/param.sfo sce_sys/icon0.png sce_module/libc.prx sce_module/libSceFios2.prx"
      $P pkg_build pkg.gp4 .
    '

echo
echo ">> done. PKG:"
ls -la "$PS4_DIR"/*.pkg
