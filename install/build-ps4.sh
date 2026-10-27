#!/usr/bin/env bash
# Build the PS4 app and produce an installable .pkg — WITHOUT root, using podman.
#
# Why containers? OpenOrbis needs clang + ld.lld, and its PkgTool.Core is an old
# self-contained .NET app that needs OpenSSL 1.1 (missing on modern Fedora).
# The build uses:
#   * localhost/rpbuilder:clang        (alpine + clang/lld/make)   -> compile + link
#   * localhost/rppackager:ubuntu20    (ubuntu 20.04 + libssl1.1)  -> make the PKG
#
# Usage:
#     bash install/build-ps4.sh
#
# Output: ps4/IV0000-BREW00300_00-REMOTEPLAYREV000.pkg
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PS4_DIR="$REPO_ROOT/ps4"
TOOLCHAIN_DIR="${OO_PS4_TOOLCHAIN:-$HOME/openorbis}"

BUILDER_IMAGE="localhost/rpbuilder:clang"
PACKAGER_IMAGE="localhost/rppackager:ubuntu20"

CONTENT_ID="IV0000-BREW00300_00-REMOTEPLAYREV000"
TITLE="Reverse Remote Play"
TITLE_ID="BREW00300"
VERSION="1.00"

# --- sanity -----------------------------------------------------------------
if ! command -v podman >/dev/null; then
    echo "podman is required. Install it (e.g. sudo dnf install podman)." >&2
    exit 1
fi
if [ ! -d "$TOOLCHAIN_DIR/include/orbis" ]; then
    echo "OpenOrbis toolchain not found at $TOOLCHAIN_DIR" >&2
    echo "Run: bash install/setup-fedora.sh   (downloads it, no root needed)" >&2
    exit 1
fi

ensure_image() {
    local img="$1" name="$2" base="$3" setup="$4"
    if podman image exists "$img"; then return; fi
    echo ">> building image $img (one-time, downloads packages)..."
    podman run --name "$name" "$base" sh -c "$setup"
    podman commit "$name" "$img" >/dev/null
    podman rm "$name" >/dev/null
}

# --- build env images (cached after first run) ------------------------------
ensure_image "$BUILDER_IMAGE" rpbuilder_boot \
    docker.io/library/alpine:latest \
    "apk add --no-cache clang lld make"

ensure_image "$PACKAGER_IMAGE" rppackager_boot \
    docker.io/library/ubuntu:20.04 \
    "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq libssl1.1 libicu66"

PODMAN_MOUNTS=(--security-opt label=disable
    -v "$TOOLCHAIN_DIR:/opt/openorbis:ro"
    -v "$REPO_ROOT:/work")

# --- 0. static libjpeg-turbo for the PS4 (built once, cached in the repo) ----
if [ ! -f "$PS4_DIR/third_party/libjpeg/lib/libjpeg.a" ]; then
    echo ">> static libjpeg-turbo missing; building it first..."
    bash "$REPO_ROOT/install/build-libjpeg-ps4.sh"
fi

# --- 0b. minimal FFmpeg H.264 decoder (built once, cached in the repo) -------
if [ ! -f "$PS4_DIR/third_party/ffmpeg/lib/libavcodec.a" ]; then
    echo ">> static FFmpeg H.264 decoder missing; building it first..."
    bash "$REPO_ROOT/install/build-ffmpeg-ps4.sh"
fi

# --- 1. compile + link + create-fself ---------------------------------------
echo ">> compiling (clang) and creating eboot.bin..."
podman run --rm "${PODMAN_MOUNTS[@]}" \
    -e OO_PS4_TOOLCHAIN=/opt/openorbis -e PATH=/usr/bin:/bin:/opt/openorbis/bin/linux \
    -w /work/ps4 "$BUILDER_IMAGE" \
    sh -c 'make clean >/dev/null 2>&1 || true; make eboot.bin'

# --- 2. package (needs glibc + OpenSSL 1.1) ---------------------------------
echo ">> generating param.sfo and building the PKG..."
podman run --rm "${PODMAN_MOUNTS[@]}" \
    -e DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 \
    -w /work/ps4 "$PACKAGER_IMAGE" \
    bash -c '
      set -e
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
