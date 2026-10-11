#!/bin/bash
# madeira-bcd FORK-ONLY (owner's decision, 2026-10-11): nothing under bcd-smoke/
# ever goes into a pull request to upstream (willfaust/madeira).
#
# Builds the D3D9 and D3D11 smoke tests with llvm-mingw and puts them where the
# app starts them by name (HomeView's "Test programs"):
#   bcd-smoke-d3d9.exe       i386    -> app/Madeira/i386-windows    (C:\windows\syswow64)
#   bcd-smoke-d3d11-x64.exe  x86-64  -> app/Madeira/arm64ec-windows (C:\windows\system32)
# The D3D11 test draws with upstream's precompiled cube shaders
# (tests/dxmt/out-x64/cube/cube_blobs.c), so nothing here compiles HLSL.
#
# Usage: bash bcd-smoke/build.sh [llvm-mingw bin dir] [--no-install]
#   --no-install  build into bcd-smoke/out only (also builds an x86-64 copy of
#                 the D3D9 test, bcd-smoke-d3d9-x64check.exe, for a 64-bit-only
#                 Wine on a host).
set -euo pipefail

R="$(cd "$(dirname "$0")/.." && pwd)"
TC="$R/toolchains/llvm-mingw-20260421-ucrt-macos-universal/bin"
INSTALL=1
for a in "$@"; do
    case "$a" in
        --no-install) INSTALL=0 ;;
        *) TC="$a" ;;
    esac
done
OUT="$R/bcd-smoke/out"
BLOBS="$R/tests/dxmt/out-x64/cube/cube_blobs.c"
CFLAGS=(-O2 -std=gnu11 -Wall -Wextra -Werror -mwindows)

[ -x "$TC/i686-w64-mingw32-clang" ] && [ -x "$TC/x86_64-w64-mingw32-clang" ] ||
    { echo "bcd-smoke: llvm-mingw not found at $TC" >&2; exit 1; }
mkdir -p "$OUT"

# Upstream's cube shaders as plain C arrays, without the C++ lookup table the
# file carries for DXMT's own tests.
sed -n -e '/^unsigned char cube_vs\[\] = {/,/^unsigned int cube_vs_len = /p' \
       -e '/^unsigned char cube_ps\[\] = {/,/^unsigned int cube_ps_len = /p' "$BLOBS" > "$OUT/cube_dxbc.c"
grep -q '^unsigned int cube_vs_len = ' "$OUT/cube_dxbc.c" && grep -q '^unsigned int cube_ps_len = ' "$OUT/cube_dxbc.c" ||
    { echo "bcd-smoke: the cube shaders were not found in $BLOBS" >&2; exit 1; }

"$TC/i686-w64-mingw32-clang" "${CFLAGS[@]}" -o "$OUT/bcd-smoke-d3d9.exe" "$R/bcd-smoke/smoke_d3d9.c" -ld3d9
"$TC/x86_64-w64-mingw32-clang" "${CFLAGS[@]}" -o "$OUT/bcd-smoke-d3d11-x64.exe" \
    "$R/bcd-smoke/smoke_d3d11.c" "$OUT/cube_dxbc.c" -ld3d11 -ldxgi -luuid
[ "$INSTALL" = 1 ] || "$TC/x86_64-w64-mingw32-clang" "${CFLAGS[@]}" -o "$OUT/bcd-smoke-d3d9-x64check.exe" \
    "$R/bcd-smoke/smoke_d3d9.c" -ld3d9

# The PE machine of each: the app picks WoW64 or the x86-64 path from it.
python3 - "$OUT/bcd-smoke-d3d9.exe" 0x14c "$OUT/bcd-smoke-d3d11-x64.exe" 0x8664 <<'EOF'
import struct, sys
args = sys.argv[1:]
for path, want in zip(args[0::2], args[1::2]):
    data = open(path, 'rb').read()
    pe = struct.unpack_from('<I', data, 0x3c)[0]
    assert data[pe:pe + 4] == b'PE\0\0', path + ': not a PE image'
    machine = struct.unpack_from('<H', data, pe + 4)[0]
    subsystem = struct.unpack_from('<H', data, pe + 24 + 68)[0]
    assert machine == int(want, 16), '%s: machine %#x, want %s' % (path, machine, want)
    assert subsystem == 2, '%s: subsystem %d, want 2 (GUI)' % (path, subsystem)
    print('bcd-smoke: %s: %d bytes, machine %#x, GUI' % (path.rsplit('/', 1)[-1], len(data), machine))
EOF

if [ "$INSTALL" = 1 ]; then
    mkdir -p "$R/app/Madeira/i386-windows" "$R/app/Madeira/arm64ec-windows"
    cp "$OUT/bcd-smoke-d3d9.exe" "$R/app/Madeira/i386-windows/bcd-smoke-d3d9.exe"
    cp "$OUT/bcd-smoke-d3d11-x64.exe" "$R/app/Madeira/arm64ec-windows/bcd-smoke-d3d11-x64.exe"
    echo "bcd-smoke: installed into app/Madeira/i386-windows and app/Madeira/arm64ec-windows"
fi
