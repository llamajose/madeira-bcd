#!/usr/bin/env python3
"""Keep the host return-address predictor balanced across x64 -> ARM64EC calls.

A guest CALL into ARM64EC code leaves the JIT for ExitFunctionEC; the callee
returns through __os_arm64x_dispatch_ret (RetToEntryThunk in Module.S),
enter_jit and the dispatcher's EnterEC entry, which branched to the loop top:
EC code bitmap check, trap flag check, L1 probe, then `br` to the block.

Calls through the import table, the usual way into system DLLs, are indirect
CALLs. An ARM64EC target never hits L1, so they reach the dispatcher with
`blr`, which pushes a return-address predictor entry. Nothing popped it: the
returned-to block was entered with `br`. Every such call left one entry
behind, so the caller's own RET, and each RET above it, was predicted from an
entry belonging to a different call.

With IosEcReturnWithRet set:
- the direct CALL path into ARM64EC code leaves with `blr` too (ExitFunctionEC
  loads LR itself on every path that uses it), so every guest CALL into
  ARM64EC code pushes one entry; JMPs keep `br`;
- EnterEC probes L1 itself and enters a hit with `ret`, popping that entry.
  The trap flag, a miss and an empty entry take the loop top as before.

An L1 entry is only created for an address the loop top has already found
outside the EC code bitmap, and unmapping a range drops its entries; the
JIT's own indirect branches already enter an L1 hit without the bitmap check.
A hit is never ARM64EC code, so the check the loop top runs first cannot
change the outcome there. The returned-to path drops from 22 instructions
(among them an mrs and four dependent loads for the bitmap) to 10, and its
indirect branch becomes a predicted return.

EC -> x64 callbacks (ExitToX64) and __os_arm64x_x64_jump also enter through
EnterEC; they now pop an entry they did not push. They are rare next to x64
-> ARM64EC calls and only cost predictions.

MADEIRA_FEX_EC_RET=0 at launch keeps `br` and the loop-top re-entry, byte for
byte. Applied by tools/build-xtajit64.sh after patch-fex-ios-callret-elide.py;
the pinned submodule is never updated.
"""
from pathlib import Path
import sys

MARKER = "ec-ret-v1 madeira-bcd"
SUMMARY = 'FEX ARM64EC: calls into ARM64EC code return with RET (MADEIRA_FEX_EC_RET=0 keeps BR)'

EDITS = {
    "FEXCore/Source/Interface/Core/ArchHelpers/Arm64Emitter.h": (
        (
"""// against zero is a bug, not a fallback -- see the check at its import site.
extern "C" uint32_t IosTebTsdOffset;
#endif

""",
"""// against zero is a bug, not a fallback -- see the check at its import site.
extern "C" uint32_t IosTebTsdOffset;

// Keeps the host return-address predictor balanced across guest calls into ARM64EC code. The indirect
// CALL path reaches such a callee through the dispatcher with BLR (an ARM64EC target never hits L1),
// while the callee's return re-entered the JIT through the loop top with BR, so every such call left an
// entry behind that later guest RETs were predicted from. When set, the direct CALL path leaves with BLR
// as well and EnterEC enters an L1 hit with RET: each call pushes one entry and its return pops it.
// Set by the frontend before the dispatcher is emitted; 0 keeps BR and the loop-top re-entry.
extern "C" uint32_t IosEcReturnWithRet;
#endif

""",
        ),
    ),
    "FEXCore/Source/Interface/Core/ArchHelpers/Arm64Emitter.cpp": (
        (
"""#include <tuple>
#include <utility>

#ifdef FEX_IOS_HOST
""",
"""#include <tuple>
#include <utility>

#if defined(FEX_IOS_HOST) && defined(ARCHITECTURE_arm64ec)
// Set by ARM64EC/Module.cpp before the dispatcher is emitted. See IosEcReturnWithRet in Arm64Emitter.h.
extern "C" uint32_t IosEcReturnWithRet = 0;
#endif

#ifdef FEX_IOS_HOST
""",
        ),
    ),
    "FEXCore/Source/Interface/Core/JIT/BranchOps.cpp": (
        (
"""      InsertGuestRIPMove(EC_CALL_CHECKER_PC_REG, NewRIP);
      ldr(TMP2, STATE_PTR(CpuStateFrame, Pointers.ExitFunctionEC));
      br(TMP2);
    } else {
#endif
""",
"""      InsertGuestRIPMove(EC_CALL_CHECKER_PC_REG, NewRIP);
      ldr(TMP2, STATE_PTR(CpuStateFrame, Pointers.ExitFunctionEC));
#ifdef FEX_IOS_HOST
      if (Op->Hint == IR::BranchHint::Call && IosEcReturnWithRet) {
        // Push the return-address predictor entry the RET in EnterEC pops (IosEcReturnWithRet in
        // Arm64Emitter.h). ExitFunctionEC loads LR itself on every path that uses it.
        blr(TMP2);
      } else {
        br(TMP2);
      }
#else
      br(TMP2);
#endif
    } else {
#endif
""",
        ),
    ),
    "FEXCore/Source/Interface/Core/Dispatcher/Dispatcher.cpp": (
        (
"""   * in feedback_fex_dispatcher_guard_regression.md). Reverted. The host-PC
   * leak needs a different angle. */
  (void)b(&LoopTop);
#else
  // As ARM64EC uses this as an entrypoint for both guest calls and host returns, opportunistically try to return
""",
"""   * in feedback_fex_dispatcher_guard_regression.md). Reverted. The host-PC
   * leak needs a different angle. */
  if (IosEcReturnWithRet) {
    // An L1 hit is a block compiled for this address, never ARM64EC code: enter it with RET, which pops the
    // predictor entry the guest CALL pushed when it left the JIT with BLR. The trap flag, a miss and an
    // ARM64EC target (never in L1) take the loop top as before.
    ldrb(TMP1, STATE_PTR(CpuStateFrame, State.flags[X86State::RFLAG_TF_RAW_LOC]));
    (void)cbnz(ARMEmitter::Size::i32Bit, TMP1, &LoopTop);
    ldp<ARMEmitter::IndexType::OFFSET>(TMP1, TMP2, STATE, offsetof(FEXCore::Core::CpuStateFrame, State.L1Pointer));
    and_(ARMEmitter::Size::i64Bit, TMP2, TMP2, EC_CALL_CHECKER_PC_REG, ARMEmitter::ShiftType::LSL,
         FEXCore::ilog2(sizeof(LookupCache::LookupCacheEntry)));
    add(TMP1, TMP1, TMP2);
    ldp<ARMEmitter::IndexType::OFFSET>(TMP4, TMP2, TMP1, 0);
    sub(TMP2, TMP2, EC_CALL_CHECKER_PC_REG);
    (void)cbnz(ARMEmitter::Size::i64Bit, TMP2, &LoopTop);
    (void)cbz(ARMEmitter::Size::i64Bit, TMP4, &LoopTop);
    ret(TMP4);
  } else {
    (void)b(&LoopTop);
  }
#else
  // As ARM64EC uses this as an entrypoint for both guest calls and host returns, opportunistically try to return
""",
        ),
    ),
    "Source/Windows/ARM64EC/Module.cpp": (
        (
""" * target. Read by the [ffs-bypass] reporter in Core.cpp's CompileBlock. */
extern "C" uint64_t IosFfsBypassLog[4];

/* Raw TSD byte offset (from TPIDRRO_EL0 & ~7) of the slot holding the TEB.
""",
""" * target. Read by the [ffs-bypass] reporter in Core.cpp's CompileBlock. */
extern "C" uint64_t IosFfsBypassLog[4];
/* Defined in FEXCore Arm64Emitter.cpp; read when the dispatcher and blocks are emitted. */
extern "C" uint32_t IosEcReturnWithRet;

/* Raw TSD byte offset (from TPIDRRO_EL0 & ~7) of the slot holding the TEB.
""",
        ),
        (
"""  CTX->SetSignalDelegator(SignalDelegator.get());
  CTX->SetSyscallHandler(SyscallHandler.get());

#ifdef FEX_IOS_HOST
""",
"""  CTX->SetSignalDelegator(SignalDelegator.get());
  CTX->SetSyscallHandler(SyscallHandler.get());

#ifdef FEX_IOS_HOST
  {
    /* Guest calls into ARM64EC code leave the JIT with BLR and their returns re-enter it with RET, so the
     * host return-address predictor stays balanced (IosEcReturnWithRet in Arm64Emitter.h). Set before
     * InitCore, which emits the dispatcher. MADEIRA_FEX_EC_RET=0 keeps BR and the loop-top re-entry. */
    const char* EcRetOpt = getenv("MADEIRA_FEX_EC_RET");
    IosEcReturnWithRet = !(EcRetOpt && strcmp(EcRetOpt, "0") == 0);
    LogMan::Msg::IFmt("FEX: ec-ret-v1 madeira-bcd enabled={}", IosEcReturnWithRet != 0);
  }
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
