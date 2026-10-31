#!/bin/sh
# qy-wine64 / qy-wine32 / qy-wine-auto: deterministic Wine entry points.
#
# Debian's /usr/bin/wine prefers the 32-bit loader whenever wine32 is installed
# (see /usr/bin/wine: "if test -x $wine32; then wine=$wine32"), regardless of
# WINEARCH. These wrappers pin the loader, the architecture and the prefix so a
# win64 prefix stays 64-bit and a win32 prefix stays 32-bit.
set -eu

name=$(basename "$0")

case "$name" in
    qy-wine64)
        loader=/usr/lib/wine/wine64
        arch=win64
        prefix=${WINEPREFIX64:-/workspace/work/.wine64}
        ;;
    qy-wine32)
        loader=/usr/lib/wine/wine
        arch=win32
        prefix=${WINEPREFIX32:-/workspace/work/.wine32}
        ;;
    qy-wine-auto)
        # wine_run passes the PE first; wine_debug passes "winedbg <pe>".
        if [ "${1:-}" = "winedbg" ]; then
            target=${2:-}
        else
            target=${1:-}
        fi
        case "$(file -b "$target" 2>/dev/null || true)" in
            PE32+*) exec /usr/local/bin/qy-wine64 "$@" ;;
            PE32*)  exec /usr/local/bin/qy-wine32 "$@" ;;
            *)      exec /usr/local/bin/qy-wine64 "$@" ;;
        esac
        ;;
    *)
        echo "qy-wine: invoke as qy-wine64, qy-wine32 or qy-wine-auto" >&2
        exit 2
        ;;
esac

export WINEARCH="$arch"
export WINEPREFIX="$prefix"
export WINELOADER="$loader"
mkdir -p "$WINEPREFIX"
# The sandbox mounts /tmp noexec; wineserver32 fails to create its temp dir
# there. The Windows profile sets TMPDIR below /workspace/work, so make sure it
# exists before the first wineboot.
if [ -n "${TMPDIR:-}" ]; then
    mkdir -p "$TMPDIR" 2>/dev/null || true
fi

exec "$loader" "$@"
