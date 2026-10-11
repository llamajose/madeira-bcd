#!/usr/bin/env python3
"""A leaner ExitFunctionEC for exits from JIT code in the iOS ARM64EC module.

Every guest call into ARM64EC code leaves the JIT through ExitFunctionEC
(Module.S). On iOS that stub reads FPCR to clear AFP's NEP and AH bits, counts
the call with an LSE atomic on a shared IosXpFex shard (and puts every 64th
target of the shard into a ring), finds the CPU area through TPIDRRO_EL0, the
TEB's TSD slot and TEB->ChpeV2CpuAreaInfo, reads the suspend doorbell through
the CPU area, and translates the target through the alias table starting at a
process-wide last-hit pointer that every thread writes.

ExitFunctionECJit is the same stub for the JIT's own exits, where x28 holds
the thread's CpuStateFrame:
- no FPCR test: it is installed only when AFP is off, and then FEX never sets
  FPCR.NEP or FPCR.AH (FillSpecialRegs and SpillStaticRegs touch them only
  with AFP, the rounding-mode ops change RMode, FZ and, with AFP, FIZ);
- the CPU area pointer comes from the frame (IosCpuArea, stored by ThreadInit
  from the CPU area it sets up) and the doorbell from the frame's own
  SuspendDoorbell, which ChpeV2CpuAreaInfo->SuspendDoorbell points at; the
  InSimulation store still comes before the doorbell load;
- the call is counted in the frame and added to the shard 64 at a time with
  STADD, so the shared line is written once per 64 calls of a thread; the
  ring then samples every 64th call of the thread;
- the alias lookup starts at the thread's own last hit (IosAliasHint) and the
  walk stores its hit there. Live alias entries never overlap (a new image
  retires every entry it overlaps), so the entry found is the same.
The suspend point, the entry-thunk dispatch and everything after the
translation are ExitFunctionEC's own code.

On the common path that is 31 instructions instead of 44, without the
mrs FPCR, the mrs TPIDRRO_EL0 and the atomic, and the doorbell is one load
from x28 instead of the end of a five-load chain.

The [xp-api] x64->EC rate lags by fewer than 64 calls per thread. The frame
offsets the stub reads are fixed in Module.S and checked by static_asserts
in Module.cpp. MADEIRA_FEX_EC_EXIT_LEAN=0 at launch keeps ExitFunctionEC.
Applied by tools/build-xtajit64.sh after patch-fex-ios-ec-bitmap-cache.py;
the pinned submodule is never updated.
"""
from pathlib import Path
import sys

MARKER = "ec-exit-lean-v1 madeira-bcd"
SUMMARY = 'FEX ARM64EC: JIT exits to ARM64EC code through ExitFunctionECJit (MADEIRA_FEX_EC_EXIT_LEAN=0 keeps ExitFunctionEC)'

EDITS = {
    "FEXCore/include/FEXCore/Core/CoreState.h": (
        (
"""  uint64_t IosEcCodeBitMap {};
#endif
};

""",
"""  uint64_t IosEcCodeBitMap {};
#endif

#if defined(FEX_IOS_HOST) && defined(ARCHITECTURE_arm64ec)
  // Read by ExitFunctionECJit in ARM64EC/Module.S at fixed offsets (checked in ARM64EC/Module.cpp): this
  // thread's ChpeV2CpuAreaInfo, its x64 -> ARM64EC calls not yet added to IosXpFex, and the alias table
  // entry its last translation used.
  uint64_t IosCpuArea {};
  uint64_t IosXpCallCount {};
  uint64_t IosAliasHint {};
#endif
};

""",
        ),
    ),
    "Source/Windows/ARM64EC/Module.S": (
        (
"""  br x16 // DispatcherLoopTopEnterECFillSRA(SingleInst:x10, CPUArea:x17)

  // Called into by FEXCore
  // Expects the target code address in x9
""",
"""  br x16 // DispatcherLoopTopEnterECFillSRA(SingleInst:x10, CPUArea:x17)

#ifdef FEX_IOS_HOST
// FEXCore::Core::CpuStateFrame offsets read below; ARM64EC/Module.cpp checks them with static_asserts.
#define IOS_FRAME_SUSPEND_DOORBELL 1512
#define IOS_FRAME_CPU_AREA 4080
#define IOS_FRAME_XP_COUNT 4088
#define IOS_FRAME_ALIAS_HINT 4096

  // ExitFunctionEC for exits from JIT code, where x28 is this thread's CpuStateFrame; installed as
  // Pointers.ExitFunctionEC when AFP is off, so FPCR.NEP and FPCR.AH are never set and there is
  // nothing to clear. The CPU area and the suspend doorbell come from the frame, the x64->EC call
  // is counted per thread and added to IosXpFex 64 at a time (each 64th target goes to the ring),
  // and the alias lookup starts at this thread's last hit. Same contract and clobbers as
  // ExitFunctionEC, whose tail it shares.
.global ExitFunctionECJit
ExitFunctionECJit:
  ldr x16, [x28, #IOS_FRAME_XP_COUNT]
  add x16, x16, #1
  str x16, [x28, #IOS_FRAME_XP_COUNT]
  tst x16, #63
  b.ne 2f
  adrp x16, IosXpFex
  add x16, x16, #:lo12:IosXpFex
  mov x23, sp
  ubfx x23, x23, #16, #4
  add x17, x16, x23, lsl #6
  add x17, x17, #64
  mov x4, #64
  stadd x4, [x17]
  add x17, x16, #3136
  mov x4, #1
  ldadd x4, x23, [x17]
  and x23, x23, #4095
  add x17, x16, #3200
  str x9, [x17, x23, lsl #3]
2:
  ldr x17, [x28, #IOS_FRAME_CPU_AREA]
  strb wzr, [x17, #0x0] // ChpeV2CpuAreaInfo->InSimulation
  ldr w17, [x28, #IOS_FRAME_SUSPEND_DOORBELL] // *ChpeV2CpuAreaInfo->SuspendDoorbell
  cbnz w17, ExitFunctionSuspendPoint
  ldr x16, [x28, #IOS_FRAME_ALIAS_HINT]
  cbz x16, ios_ec_jit_xlate_walk
  ldr x23, [x16]        // Entry.PeBase
  sub x23, x9, x23
  ldr x4, [x16, #16]    // Entry.Size
  cmp x23, x4
  b.lo ios_ec_xlate_hit
ios_ec_jit_xlate_walk:
  adrp x17, IosAliasCount
  ldr w17, [x17, #:lo12:IosAliasCount]
  adrp x16, IosAliasEntries
  add x16, x16, #:lo12:IosAliasEntries
  add x16, x16, x17, lsl #5   // one past the last entry
ios_ec_jit_xlate_loop:
  cbz w17, ios_ec_xlate_done
  sub x16, x16, #32
  sub w17, w17, #1
  ldr x23, [x16]        // Entry.PeBase
  sub x23, x9, x23
  ldr x4, [x16, #16]    // Entry.Size
  cmp x23, x4
  b.hs ios_ec_jit_xlate_loop
  str x16, [x28, #IOS_FRAME_ALIAS_HINT]
  b ios_ec_xlate_hit
#endif

  // Called into by FEXCore
  // Expects the target code address in x9
""",
        ),
    ),
    "Source/Windows/ARM64EC/Module.cpp": (
        (
""" *   3136  ring index (every 64th x64->EC call of a shard appends its target)
 *   3200  ring[4096] of target addresses (as the x64 caller called them)
 * Keep in sync with Module.S and build/ntdll-unix/server_ios.c (ios_xp_fex_*). */
extern "C" __attribute__((aligned(64))) uint64_t IosXpFex[4496];
__attribute__((aligned(64))) uint64_t IosXpFex[4496] {};
#endif // FEX_IOS_HOST

""",
""" *   3136  ring index (every 64th x64->EC call of a shard appends its target)
 *   3200  ring[4096] of target addresses (as the x64 caller called them)
 * ExitFunctionECJit adds its thread's x64->EC calls 64 at a time and rings every
 * 64th call of the thread instead.
 * Keep in sync with Module.S and build/ntdll-unix/server_ios.c (ios_xp_fex_*). */
extern "C" __attribute__((aligned(64))) uint64_t IosXpFex[4496];
__attribute__((aligned(64))) uint64_t IosXpFex[4496] {};
/* Non-zero when JIT exits use ExitFunctionECJit (Module.S) instead of ExitFunctionEC; set in ProcessInit. */
static uint32_t IosEcExitLean = 0;
#endif // FEX_IOS_HOST

""",
        ),
        (
"""
extern void* ExitFunctionEC;
extern void* CheckCall;
extern void* ExitFunctionSuspendPoint;
""",
"""
extern void* ExitFunctionEC;
#ifdef FEX_IOS_HOST
extern void* ExitFunctionECJit;
#endif
extern void* CheckCall;
extern void* ExitFunctionSuspendPoint;
""",
        ),
        (
"""    auto HostFeatures = FEX::Windows::CPUFeatures::FetchHostFeatures(IsWine, FEXCore::HostFeatures::HostTypeEnum::Arm64ec);
    CTX = FEXCore::Context::Context::CreateNewContext(HostFeatures);
  }

""",
"""    auto HostFeatures = FEX::Windows::CPUFeatures::FetchHostFeatures(IsWine, FEXCore::HostFeatures::HostTypeEnum::Arm64ec);
    CTX = FEXCore::Context::Context::CreateNewContext(HostFeatures);
#ifdef FEX_IOS_HOST
    /* ExitFunctionECJit (Module.S) leaves out ExitFunctionEC's FPCR test, so it is used only without AFP,
     * when FEX never sets FPCR.NEP or FPCR.AH. MADEIRA_FEX_EC_EXIT_LEAN=0 keeps ExitFunctionEC. */
    const char* LeanOpt = getenv("MADEIRA_FEX_EC_EXIT_LEAN");
    IosEcExitLean = !HostFeatures.SupportsAFP && !(LeanOpt && strcmp(LeanOpt, "0") == 0);
    LogMan::Msg::IFmt("FEX: ec-exit-lean-v1 madeira-bcd enabled={} afp={}", IosEcExitLean != 0, HostFeatures.SupportsAFP);
#endif
  }

""",
        ),
        (
'  IosTiLog("[FEX-iOS] TI:callret\\n");\n#endif\n  Thread->CurrentFrame->Pointers.ExitFunctionEC = reinterpret_cast<uintptr_t>(&ExitFunctionEC);\n  CPUArea.StateFrame() = Thread->CurrentFrame;\n\n',
'  IosTiLog("[FEX-iOS] TI:callret\\n");\n#endif\n#ifdef FEX_IOS_HOST\n  static_assert(offsetof(FEXCore::Core::CpuStateFrame, SuspendDoorbell) == 1512, "IOS_FRAME_SUSPEND_DOORBELL in Module.S");\n  static_assert(offsetof(FEXCore::Core::CpuStateFrame, IosCpuArea) == 4080, "IOS_FRAME_CPU_AREA in Module.S");\n  static_assert(offsetof(FEXCore::Core::CpuStateFrame, IosXpCallCount) == 4088, "IOS_FRAME_XP_COUNT in Module.S");\n  static_assert(offsetof(FEXCore::Core::CpuStateFrame, IosAliasHint) == 4096, "IOS_FRAME_ALIAS_HINT in Module.S");\n  // ExitFunctionECJit reads the CPU area from the frame; the doorbell it reads is the frame\'s own\n  // SuspendDoorbell, which CPUArea.Area->SuspendDoorbell points at (set below).\n  Thread->CurrentFrame->IosCpuArea = reinterpret_cast<uint64_t>(CPUArea.Area);\n  Thread->CurrentFrame->Pointers.ExitFunctionEC = reinterpret_cast<uintptr_t>(IosEcExitLean ? &ExitFunctionECJit : &ExitFunctionEC);\n#else\n  Thread->CurrentFrame->Pointers.ExitFunctionEC = reinterpret_cast<uintptr_t>(&ExitFunctionEC);\n#endif\n  CPUArea.StateFrame() = Thread->CurrentFrame;\n\n',
        ),
    ),
}


def patch(sources):
    """Patch {relative path: text}; returns the patched texts.

    A file whose every replacement is already present is left alone; a file
    with some but not all of them, or with a moved anchor, raises ValueError.
    A replacement counts as present only where its anchor is gone or is part
    of it, so an insertion whose anchor survives inside it is not applied twice."""
    results = {}
    for rel, edits in EDITS.items():
        text = sources[rel]
        done = [text.count(new) == 1 and (old in new or old not in text) for old, new in edits]
        if all(done):
            results[rel] = text
            continue
        for (old, new), present in zip(edits, done):
            if present or text.count(old) != 1 or (new in text and new not in old):
                first = old.strip().splitlines()[0] if old.strip() else old
                raise ValueError(f"{rel}: anchor changed or partial overlay: {first[:80]!r}")
        for old, new in edits:
            text = text.replace(old, new, 1)
        if not all(text.count(new) == 1 for _, new in edits):
            raise ValueError(f"{rel}: replacements overlap")
        results[rel] = text
    return results


def main(argv):
    if len(argv) != 2:
        print(f"usage: {argv[0]} <FEX source root>", file=sys.stderr)
        return 2
    root = Path(argv[1])
    sources = {rel: (root / rel).read_text() for rel in EDITS}
    try:
        results = patch(sources)
    except ValueError as error:
        print(f"{Path(argv[0]).name}: {error}", file=sys.stderr)
        return 1
    for rel, text in results.items():
        if text != sources[rel]:
            (root / rel).write_text(text)
    print(SUMMARY)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
