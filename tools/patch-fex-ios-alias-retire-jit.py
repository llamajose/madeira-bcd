#!/usr/bin/env python3
"""Retire alias entries whose JIT range a new image copy reuses.

BTCpu64IosAddAliasMapping (Source/Windows/ARM64EC/IosJitAlias.cpp) retires an
entry only when the incoming image overlaps its PE range. A pool range is
recycled after its process dies, though, so a new image copy can land on the
JIT range of an entry that is still in some emulator's table, at a different
PE base. IosJitReverseTranslate walks the table oldest-first, finds the dead
entry and maps the new copy's guest RIPs to the dead image's PE addresses.

GTA V Enhanced build 376 (2026-10-04 10:29): RockstarService.exe's "start"
instance mapped its exe (0x1231e0000) before its emulator registered, the
push went to the Rockstar Games Launcher's emulator, the instance exited and
its range was reused for the launcher's cryptnet.dll; the launcher's first
call into cryptnet's delay-load stub was redirected to 0x1231f678a
("[pool-rip-fix] ... alias of PE 0x1231f678a", "[iOS-xquery] MISS",
NoExec), the AV went to the launcher's crash handler and it exited
0xffff7001 ("Unable to launch game").

A live image proves any entry covering the same JIT range is dead (one pool
range holds one copy), so such entries are retired like PE overlaps. A
re-registration of the same copy (same PE and JIT base) still returns early
before this. Fork-local build patch for xtajit64.dll (tools/build-xtajit64.sh);
not a contribution to FEX.

Usage: patch-fex-ios-alias-retire-jit.py FEX/Source/Windows/ARM64EC/IosJitAlias.cpp
Idempotent; fails by name if the anchor moved.
"""
import sys

path = sys.argv[1]
s = open(path).read()
if "madeira-bcd: a reused JIT range" in s:
    print("already patched"); sys.exit(0)

old = """    const uint64_t pb = g_Entries[i].PeBase;
    const uint64_t sz = g_Entries[i].Size;
    if (!sz) {
      continue;
    }
    if (pb < PeBase + Size && PeBase < pb + sz) {
"""
new = """    const uint64_t pb = g_Entries[i].PeBase;
    const uint64_t sz = g_Entries[i].Size;
    const uint64_t jb = g_Entries[i].JitBase;
    if (!sz) {
      continue;
    }
    /* madeira-bcd: a reused JIT range retires the dead entry too
     * (tools/patch-fex-ios-alias-retire-jit.py). */
    if ((pb < PeBase + Size && PeBase < pb + sz) || (jb < JitBase + Size && JitBase < jb + sz)) {
"""
if s.count(old) != 1:
    print("::error::patch-fex-ios-alias-retire-jit: anchor not found exactly once -- IosJitAlias.cpp changed")
    sys.exit(1)
open(path, "w").write(s.replace(old, new, 1))
print("patched")
