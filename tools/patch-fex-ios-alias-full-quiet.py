#!/usr/bin/env python3
"""No logging from FEX's alias registration when its table is full.

BTCpu64IosAddAliasMapping (Source/Windows/ARM64EC/IosJitAlias.cpp) is called
as a native callback from inside unix-side syscalls (ios_jit_add_mapping and
the alias drain in build/ntdll-unix). When the 256-entry table is full its
ml1106 branch logged through LogMan, and that output goes through the PE
ntdll as a second unix call nested in the running NtMapViewOfSection: the
syscall frame is corrupted and the thread returns into it. GTA V Enhanced
build 367 (2026-10-03 22:37 log): both RockstarService.exe service processes
died while mapping win32u.dll with pc equal to their own syscall frame
address, right after "IosAliasEntries FULL".

A refused alias is harmless by itself (calls into that image fault-redirect),
so the branch now only counts. Fork-local build patch for xtajit64.dll
(tools/build-xtajit64.sh); not a contribution to FEX.

Usage: patch-fex-ios-alias-full-quiet.py FEX/Source/Windows/ARM64EC/IosJitAlias.cpp
Idempotent; fails by name if the anchor moved.
"""
import sys

path = sys.argv[1]
s = open(path).read()
if "madeira-bcd: no logging here" in s:
    print("already patched"); sys.exit(0)

old = """    static int said = 0;
    if (said++ < 8) {
      LogMan::Msg::EFmt("[jit-alias] ml1106 IosAliasEntries FULL ({} entries): image 0x{:x}+0x{:x} NOT registered -- its calls will fault-redirect", count, PeBase, Size);
    }
    return;
"""
new = """    /* madeira-bcd: no logging here -- this runs inside unix-side syscalls
     * (ios_jit_add_mapping, the alias drain), and LogMan output is a nested
     * unix call that corrupted the running syscall's frame (gta-2237:
     * RockstarService died at pc == its syscall frame). Count only
     * (tools/patch-fex-ios-alias-full-quiet.py). */
    static uint64_t refused = 0;
    __atomic_fetch_add(&refused, 1, __ATOMIC_RELAXED);
    return;
"""
if s.count(old) != 1:
    print("::error::patch-fex-ios-alias-full-quiet: anchor not found exactly once -- IosJitAlias.cpp changed")
    sys.exit(1)
open(path, "w").write(s.replace(old, new, 1))
print("patched")
