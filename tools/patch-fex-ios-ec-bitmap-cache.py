#!/usr/bin/env python3
"""Load the EC code bitmap pointer from the thread's frame at the dispatcher loop top.

Every guest call into ARM64EC code through the import table misses L1 and
reaches the dispatcher loop top, which tests the EC code bitmap before it
looks for a block. On iOS the test found the bitmap through TPIDRRO_EL0, the
TEB's TSD slot, TEB->Peb and PEB->EcCodeBitMap: an mrs and three dependent
loads before the bitmap word itself.

PEB->EcCodeBitMap is set when the first ARM64EC image is mapped (Wine's
alloc_arm64ec_map), before this module loads, and never changes. The frontend
reads it once in ProcessInit through the same TEB and PEB offsets and stores it
in each thread's CpuStateFrame at ThreadInit (IosEcCodeBitMap, appended after
the existing fields); the loop top loads it with one LDR from STATE. The
bitmap test itself is unchanged. That is four instructions less per pass,
among them the mrs and two of the dependent loads.

If the pointer is still null at ProcessInit, the walk is emitted as before.
MADEIRA_FEX_EC_BITMAP_CACHE=0 at launch keeps the walk too, byte for byte.
Applied by tools/build-xtajit64.sh after patch-fex-ios-ec-ret.py; the pinned
submodule is never updated.
"""
from pathlib import Path
import sys

MARKER = "ec-bitmap-cache-v1 madeira-bcd"
SUMMARY = 'FEX ARM64EC: loop-top EC code check loads the bitmap pointer from the frame (MADEIRA_FEX_EC_BITMAP_CACHE=0 keeps the TEB walk)'

EDITS = {
    "FEXCore/include/FEXCore/Core/CoreState.h": (
        (
"""    struct Edge { uint64_t Source, Target, Block, Hint; } Edges[8] {};
  } IosGuestBranchHistory {};
};

""",
"""    struct Edge { uint64_t Source, Target, Block, Hint; } Edges[8] {};
  } IosGuestBranchHistory {};

#if defined(FEX_IOS_HOST) && defined(ARCHITECTURE_arm64ec)
  // PEB->EcCodeBitMap for the dispatcher's EC code check, stored by the frontend at thread init. The
  // pointer does not change once the first ARM64EC image is mapped, before any thread enters the JIT.
  uint64_t IosEcCodeBitMap {};
#endif
};

""",
        ),
        (
"""static_assert(offsetof(CpuStateFrame, IosGuestBranchHistory) % 8 == 0);
static_assert(offsetof(CpuStateFrame, IosGuestBranchHistory) + 272 <= 32760);
#endif
static_assert(offsetof(CpuStateFrame, State) == 0, "CPUState must be first member in CpuStateFrame");
""",
"""static_assert(offsetof(CpuStateFrame, IosGuestBranchHistory) % 8 == 0);
static_assert(offsetof(CpuStateFrame, IosGuestBranchHistory) + 272 <= 32760);
static_assert(offsetof(CpuStateFrame, IosEcCodeBitMap) % 8 == 0 && offsetof(CpuStateFrame, IosEcCodeBitMap) <= 32760,
              "IosEcCodeBitMap is loaded with a scaled LDR");
#endif
static_assert(offsetof(CpuStateFrame, State) == 0, "CPUState must be first member in CpuStateFrame");
""",
        ),
    ),
    "FEXCore/Source/Interface/Core/Dispatcher/Dispatcher.cpp": (
        (
"""#include <bit>
#include <cstring>

namespace FEXCore::CPU {
""",
"""#include <bit>
#include <cstring>

#if defined(FEX_IOS_HOST) && defined(ARCHITECTURE_arm64ec)
// Set by ARM64EC/Module.cpp before the dispatcher is emitted, once every thread's frame carries
// PEB->EcCodeBitMap (CpuStateFrame::IosEcCodeBitMap): the loop top's EC code check loads it from there.
extern "C" uint32_t IosEcCodeBitMapCached = 0;
#endif

namespace FEXCore::CPU {
""",
        ),
        (
"""  ARMEmitter::ForwardLabel l_NotECCode;
#ifdef FEX_IOS_HOST
  // iOS clobbers x18 — read TEB from TPIDRRO_EL0+TSD slot 275 instead.
  mrs(TMP1, ARMEmitter::SystemRegister::TPIDRRO_EL0);
  and_(ARMEmitter::Size::i64Bit, TMP1, TMP1, ~7ULL);
  ldr(TMP1, TMP1, IosTebTsdOffset);
  ldr(TMP1, TMP1, TEB_PEB_OFFSET);
#else
  ldr(TMP1, ARMEmitter::XReg::x18, TEB_PEB_OFFSET);
#endif
  ldr(TMP1, TMP1, PEB_EC_CODE_BITMAP_OFFSET);

  lsr(ARMEmitter::Size::i64Bit, TMP2, RipReg, 18);
""",
"""  ARMEmitter::ForwardLabel l_NotECCode;
#ifdef FEX_IOS_HOST
  if (IosEcCodeBitMapCached) {
    // PEB->EcCodeBitMap as stored in this thread's frame: one load instead of the TEB/PEB walk below.
    ldr(TMP1, STATE_PTR(CpuStateFrame, IosEcCodeBitMap));
  } else {
    // iOS clobbers x18 — read TEB from TPIDRRO_EL0+TSD slot 275 instead.
    mrs(TMP1, ARMEmitter::SystemRegister::TPIDRRO_EL0);
    and_(ARMEmitter::Size::i64Bit, TMP1, TMP1, ~7ULL);
    ldr(TMP1, TMP1, IosTebTsdOffset);
    ldr(TMP1, TMP1, TEB_PEB_OFFSET);
    ldr(TMP1, TMP1, PEB_EC_CODE_BITMAP_OFFSET);
  }
#else
  ldr(TMP1, ARMEmitter::XReg::x18, TEB_PEB_OFFSET);
  ldr(TMP1, TMP1, PEB_EC_CODE_BITMAP_OFFSET);
#endif

  lsr(ARMEmitter::Size::i64Bit, TMP2, RipReg, 18);
""",
        ),
    ),
    "Source/Windows/ARM64EC/Module.cpp": (
        (
"""extern "C" uint64_t IosSubfloorToLow(uint64_t Addr);    // ml951
extern "C" uint64_t IosSubfloorClipSize(uint64_t LowAddr, uint64_t Size);  // ml951
/* ml316: FFS-bypass diagnostics, written by ExitToX64's bypass path in Module.S:
 * [0] = native short-circuits taken, [1] = last EC target,
""",
"""extern "C" uint64_t IosSubfloorToLow(uint64_t Addr);    // ml951
extern "C" uint64_t IosSubfloorClipSize(uint64_t LowAddr, uint64_t Size);  // ml951
/* Defined in FEXCore Dispatcher.cpp; read when the dispatcher is emitted. */
extern "C" uint32_t IosEcCodeBitMapCached;
/* PEB->EcCodeBitMap as read in ProcessInit; ThreadInit stores it in every thread's frame. */
static uint64_t IosEcCodeBitMapValue = 0;
/* ml316: FFS-bypass diagnostics, written by ExitToX64's bypass path in Module.S:
 * [0] = native short-circuits taken, [1] = last EC target,
""",
        ),
        (
"""  }

  CTX->SetSignalDelegator(SignalDelegator.get());
  CTX->SetSyscallHandler(SyscallHandler.get());
""",
"""  }

#ifdef FEX_IOS_HOST
  {
    /* PEB->EcCodeBitMap is set when the first ARM64EC image is mapped, before this module loads, and
     * never moves, so every thread's frame carries it (ThreadInit) and the dispatcher's EC code check
     * loads it from there. Set before InitCore, which emits the dispatcher.
     * MADEIRA_FEX_EC_BITMAP_CACHE=0 keeps the TPIDRRO_EL0 -> TEB -> PEB walk. */
    static_assert(offsetof(__TEB, Peb) == 0x60 && offsetof(__PEB, EcCodeBitMap) == 0x368,
                  "the TEB_PEB_OFFSET and PEB_EC_CODE_BITMAP_OFFSET the dispatcher walks");
    const char* BitMapOpt = getenv("MADEIRA_FEX_EC_BITMAP_CACHE");
    IosEcCodeBitMapValue = reinterpret_cast<uint64_t>(reinterpret_cast<__TEB*>(IOSLoadTEB())->Peb->EcCodeBitMap);
    IosEcCodeBitMapCached = IosEcCodeBitMapValue && !(BitMapOpt && strcmp(BitMapOpt, "0") == 0);
    LogMan::Msg::IFmt("FEX: ec-bitmap-cache-v1 madeira-bcd enabled={} map={:#x}", IosEcCodeBitMapCached != 0,
                      IosEcCodeBitMapValue);
  }
#endif

  CTX->SetSignalDelegator(SignalDelegator.get());
  CTX->SetSyscallHandler(SyscallHandler.get());
""",
        ),
        (
"""  Frame->State.cs_idx = FEXCore::Core::CPUState::DEFAULT_USER_CS << 3;
  Frame->State.cs_cached = FEXCore::Core::CPUState::CalculateGDTBase(GDT);

#ifdef FEX_IOS_HOST
""",
"""  Frame->State.cs_idx = FEXCore::Core::CPUState::DEFAULT_USER_CS << 3;
  Frame->State.cs_cached = FEXCore::Core::CPUState::CalculateGDTBase(GDT);
#ifdef FEX_IOS_HOST
  // For the dispatcher's EC code check (CpuStateFrame::IosEcCodeBitMap).
  Frame->IosEcCodeBitMap = IosEcCodeBitMapValue;
#endif

#ifdef FEX_IOS_HOST
""",
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
