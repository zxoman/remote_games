#!/usr/bin/env bash
# Translate clang-style linker invocations into an OpenOrbis ld.lld call.
#
# FFmpeg's `./configure` probes the compiler by linking tiny programs, and its
# Makefiles link with clang by default. clang cannot link for this target (it
# looks for crtbegin/libgcc that OpenOrbis does not ship), so we hand the object
# files to ld.lld with the toolchain's link script + crt1.o instead.
#
# Mirrors Moonlight-ps4's scripts/ps4-ld.sh. Requires OO_PS4_TOOLCHAIN.
set -euo pipefail

OO="${OO_PS4_TOOLCHAIN:?OO_PS4_TOOLCHAIN must be set}"

args=()
for a in "$@"; do
    case "$a" in
        -fuse-ld=*|--target=*|-pie|-shared) ;; # clang-only flags ld.lld rejects
        -Wl,*)
            IFS=',' read -ra parts <<< "${a#-Wl,}"
            for p in "${parts[@]}"; do args+=("$p"); done
            ;;
        *) args+=("$a") ;;
    esac
done

exec ld.lld -m elf_x86_64 --eh-frame-hdr \
    --script "$OO/link.x" "$OO/lib/crt1.o" \
    -L"$OO/lib" "${args[@]}" -lc -lkernel
