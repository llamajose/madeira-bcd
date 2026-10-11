#!/usr/bin/env python3
"""Stop maintaining the call-ret shadow stack in the iOS ARM64EC JIT.

FEX keeps a shadow stack of {guest return RIP, host label} pairs as a
return-address predictor: a guest CALL pushes, a guest RET pops and, upstream,
branches to the host label when the guest half matches. The iOS port turned
both consumers off for the ARM64EC module: the RET-side shortcut is compiled
out under FEX_IOS_HOST (BranchOps.cpp) and EnterEC branches straight to the
dispatcher loop top instead of popping the stack (Dispatcher.cpp). What is
left is the bookkeeping: every guest CALL reloads x17 from the frame, checks
it against the window, pushes 16 bytes and writes x17 back (9 instructions),
every guest RET does the same around a pop whose result is discarded (9), and
every dispatcher exit and fill stores or reloads x17. Nothing reads it.

The iOS WOW64 module already builds without the stack (FEX_CALLRET_STACK_UNUSED
in Arm64Emitter.h). This makes the same emission selectable at run time for
the ARM64EC module: CallRetStackUsed() is constexpr for every other build (so
their emission is unchanged) and reads IosCallRetStackElided on iOS ARM64EC,
which the frontend sets before InitCore emits the dispatcher.

With the stack gone, a linked CALL is `adr TMP1, <return label>; bl <thunk>`,
the layout ExitFunctionLink knows from FEX_CALLRET_STACK_UNUSED, so a linked
call is backpatched as `bl` (upstream's behaviour; the old iOS ARM64EC layout,
`adr; stp; str; bl`, never matched the marker and was linked as `b`). Guest
control flow is unchanged: a RET still finds its target through the L1 cache.

The guest call chains the unix-side fault reports print from the stack
([x86_callret], ml1002) are not recorded while the stack is elided.

MADEIRA_FEX_CALLRET_ELIDE=0 at launch keeps the stack, byte for byte.
Applied by tools/build-xtajit64.sh after the other ARM64EC overlays (its
anchors are their output); the pinned submodule is never updated.
"""
from pathlib import Path
import sys

MARKER = "callret-elide-v1 madeira-bcd"
SUMMARY = 'FEX ARM64EC: call-ret shadow stack not maintained on iOS (MADEIRA_FEX_CALLRET_ELIDE=0 keeps it)'

EDITS = {
    "FEXCore/Source/Interface/Core/ArchHelpers/Arm64Emitter.h": (
        (
"""#if defined(FEX_IOS_HOST) && !defined(ARCHITECTURE_arm64ec)
#define FEX_CALLRET_STACK_UNUSED 1
#endif

""",
"""#if defined(FEX_IOS_HOST) && !defined(ARCHITECTURE_arm64ec)
#define FEX_CALLRET_STACK_UNUSED 1
#endif

// The iOS ARM64EC module does not read the shadow stack either: the RET-side shortcut is compiled out
// on iOS and EnterEC branches straight to the loop top instead of popping it. Its pushes and pops only
// maintain a structure nothing reads, so the frontend may elide them before the dispatcher is emitted
// (IosCallRetStackElided != 0 selects the FEX_CALLRET_STACK_UNUSED sequence at emission time; 0 keeps
// the shadow stack byte for byte).
#if defined(FEX_IOS_HOST) && defined(ARCHITECTURE_arm64ec)
extern "C" uint32_t IosCallRetStackElided;
inline bool CallRetStackUsed() {
  return IosCallRetStackElided == 0;
}
#elif defined(FEX_CALLRET_STACK_UNUSED)
constexpr bool CallRetStackUsed() {
  return false;
}
#else
constexpr bool CallRetStackUsed() {
  return true;
}
#endif

""",
        ),
    ),
    "FEXCore/Source/Interface/Core/ArchHelpers/Arm64Emitter.cpp": (
        (
"""// ARM64EC/Module.cpp at process init. See IosTebTsdOffset in Arm64Emitter.h.
extern "C" uint32_t IosTebTsdOffset = 0;
#endif

""",
"""// ARM64EC/Module.cpp at process init. See IosTebTsdOffset in Arm64Emitter.h.
extern "C" uint32_t IosTebTsdOffset = 0;
#endif
#if defined(FEX_IOS_HOST) && defined(ARCHITECTURE_arm64ec)
// Set by ARM64EC/Module.cpp before the dispatcher is emitted. See CallRetStackUsed in Arm64Emitter.h.
extern "C" uint32_t IosCallRetStackElided = 0;
#endif

""",
        ),
        (
"""  Options.GPRSpillMask &= ~PFAFSpillMask;

#ifndef FEX_CALLRET_STACK_UNUSED
  str(REG_CALLRET_SP, STATE.R(), offsetof(FEXCore::Core::CpuStateFrame, State.callret_sp));
#endif

  for (size_t i = 0; i < StaticRegisters.size(); i += 2) {
""",
"""  Options.GPRSpillMask &= ~PFAFSpillMask;

  if (CallRetStackUsed()) {
    str(REG_CALLRET_SP, STATE.R(), offsetof(FEXCore::Core::CpuStateFrame, State.callret_sp));
  }

  for (size_t i = 0; i < StaticRegisters.size(); i += 2) {
""",
        ),
        (
"""#endif

#ifndef FEX_CALLRET_STACK_UNUSED
  ldr(REG_CALLRET_SP, STATE.R(), offsetof(FEXCore::Core::CpuStateFrame, State.callret_sp));
#endif

#ifdef FEX_GUEST_WINDOW
""",
"""#endif

  if (CallRetStackUsed()) {
    ldr(REG_CALLRET_SP, STATE.R(), offsetof(FEXCore::Core::CpuStateFrame, State.callret_sp));
  }

#ifdef FEX_GUEST_WINDOW
""",
        ),
    ),
    "FEXCore/Source/Interface/Core/JIT/BranchOps.cpp": (
        (
"""#ifdef ARCHITECTURE_arm64ec
    if (NewRIP < EC_CODE_BITMAP_MAX_ADDRESS && RtlIsEcCode(NewRIP)) {
      str(REG_CALLRET_SP, STATE_PTR(CpuStateFrame, State.callret_sp));
      add(ARMEmitter::Size::i64Bit, ARMEmitter::Reg::rsp, StaticRegisters[X86State::REG_RSP], 0);
      InsertGuestRIPMove(EC_CALL_CHECKER_PC_REG, NewRIP);
""",
"""#ifdef ARCHITECTURE_arm64ec
    if (NewRIP < EC_CODE_BITMAP_MAX_ADDRESS && RtlIsEcCode(NewRIP)) {
      if (CallRetStackUsed()) {
        str(REG_CALLRET_SP, STATE_PTR(CpuStateFrame, State.callret_sp));
      }
      add(ARMEmitter::Size::i64Bit, ARMEmitter::Reg::rsp, StaticRegisters[X86State::REG_RSP], 0);
      InsertGuestRIPMove(EC_CALL_CHECKER_PC_REG, NewRIP);
""",
        ),
        (
"""      if (Op->Hint == IR::BranchHint::Call) {
#ifdef ARCHITECTURE_arm64ec
        // iOS-Madeira 2026-05-13: REG_CALLRET_SP (x17) gets clobbered when a
        // previous BLR returns from native ARM64EC code (DXMT vtable methods,
        // ARM64EC entry thunks). Reload from State.callret_sp before pushing
        // the call-return frame so we don't stp to wherever x17 was left.
        ldr(REG_CALLRET_SP, STATE, offsetof(FEXCore::Core::CpuStateFrame, State.callret_sp));
        // iOS-Madeira 2026-05-18: inline bounds-guard (Tier-2). iOS doesn't
        // honor PAGE_NOACCESS on the callret stack's guard pages, so the
        // SEH-driven HandleAccessViolation never resets the stack on
        // underflow. Detect-and-reset inline before the `stp` to keep stack
        // pointer in-range. Uses TMP1 as scratch; adr below re-initializes it.
        {
          ARMEmitter::ForwardLabel l_callret_ok;
          /* iOS-Madeira ml263: the >>24 test only fires when the pointer leaves the
""",
"""      if (Op->Hint == IR::BranchHint::Call) {
#ifdef ARCHITECTURE_arm64ec
        if (CallRetStackUsed()) {
          // iOS-Madeira 2026-05-13: REG_CALLRET_SP (x17) gets clobbered when a
          // previous BLR returns from native ARM64EC code (DXMT vtable methods,
          // ARM64EC entry thunks). Reload from State.callret_sp before pushing
          // the call-return frame so we don't stp to wherever x17 was left.
          ldr(REG_CALLRET_SP, STATE, offsetof(FEXCore::Core::CpuStateFrame, State.callret_sp));
          // iOS-Madeira 2026-05-18: inline bounds-guard (Tier-2). iOS doesn't
          // honor PAGE_NOACCESS on the callret stack's guard pages, so the
          // SEH-driven HandleAccessViolation never resets the stack on
          // underflow. Detect-and-reset inline before the `stp` to keep stack
          // pointer in-range. Uses TMP1 as scratch; adr below re-initializes it.
          ARMEmitter::ForwardLabel l_callret_ok;
          /* iOS-Madeira ml263: the >>24 test only fires when the pointer leaves the
""",
        ),
        (
"""        }
#endif
#ifdef FEX_CALLRET_STACK_UNUSED
        // Nothing reads the call-ret shadow stack in this module (see FEX_CALLRET_STACK_UNUSED in
        // Arm64Emitter.h), so there is no push. The adr stays: it is the known-call marker that
        // ExitFunctionLink looks for to relink this callsite as a `bl`.
        if (!Op->CallReturnBlock.IsInvalid()) {
          PendingCallReturnTargetLabel = &CallReturnTargets.try_emplace(Op->CallReturnBlock.ID()).first->second;
          (void)adr(TMP1, &l_CallReturn);
        }
#else
        if (!Op->CallReturnBlock.IsInvalid()) {
          auto CallReturnAddressReg = GetReg(Op->CallReturnAddress).X();
          PendingCallReturnTargetLabel = &CallReturnTargets.try_emplace(Op->CallReturnBlock.ID()).first->second;
""",
"""        }
#endif
        if (!CallRetStackUsed()) {
          // Nothing reads the call-ret shadow stack in this module (see CallRetStackUsed in
          // Arm64Emitter.h), so there is no push. The adr stays: it is the known-call marker that
          // ExitFunctionLink looks for to relink this callsite as a `bl`.
          if (!Op->CallReturnBlock.IsInvalid()) {
            PendingCallReturnTargetLabel = &CallReturnTargets.try_emplace(Op->CallReturnBlock.ID()).first->second;
            (void)adr(TMP1, &l_CallReturn);
          }
        } else if (!Op->CallReturnBlock.IsInvalid()) {
          auto CallReturnAddressReg = GetReg(Op->CallReturnAddress).X();
          PendingCallReturnTargetLabel = &CallReturnTargets.try_emplace(Op->CallReturnBlock.ID()).first->second;
""",
        ),
        (
"""          stp<ARMEmitter::IndexType::PRE>(ARMEmitter::XReg::zr, ARMEmitter::XReg::zr, REG_CALLRET_SP, -0x10);
        }
#endif
#ifdef ARCHITECTURE_arm64ec
        /* Write back post-push x17 so dispatcher LoopTop's reload picks
         * up the new top. Without this, in-register PUSH changes get
         * lost on next dispatcher iteration and the entry leaks. */
        str(REG_CALLRET_SP, STATE, offsetof(FEXCore::Core::CpuStateFrame, State.callret_sp));
#endif
      } else if (Op->Hint == IR::BranchHint::CheckTF) {
""",
"""          stp<ARMEmitter::IndexType::PRE>(ARMEmitter::XReg::zr, ARMEmitter::XReg::zr, REG_CALLRET_SP, -0x10);
        }
#ifdef ARCHITECTURE_arm64ec
        if (CallRetStackUsed()) {
          /* Write back post-push x17 so dispatcher LoopTop's reload picks
           * up the new top. Without this, in-register PUSH changes get
           * lost on next dispatcher iteration and the entry leaks. */
          str(REG_CALLRET_SP, STATE, offsetof(FEXCore::Core::CpuStateFrame, State.callret_sp));
        }
#endif
      } else if (Op->Hint == IR::BranchHint::CheckTF) {
""",
        ),
        (
"""
#ifndef FEX_CALLRET_STACK_UNUSED
    if (Op->Hint == IR::BranchHint::Return) {
      // First try to pop from the call-ret stack, otherwise follow the normal path (but ending in a ret)
#ifdef ARCHITECTURE_arm64ec
""",
"""
#ifndef FEX_CALLRET_STACK_UNUSED
    if (Op->Hint == IR::BranchHint::Return && CallRetStackUsed()) {
      // First try to pop from the call-ret stack, otherwise follow the normal path (but ending in a ret)
#ifdef ARCHITECTURE_arm64ec
""",
        ),
        (
"""      ARMEmitter::ForwardLabel l_CallReturn;
#ifdef ARCHITECTURE_arm64ec
      // iOS-Madeira 2026-05-13: see note above — reload REG_CALLRET_SP (x17)
      // from State.callret_sp before pushing the call-return frame, since
      // native ARM64EC returns leave x17 pointing at an arbitrary RX page.
      ldr(REG_CALLRET_SP, STATE, offsetof(FEXCore::Core::CpuStateFrame, State.callret_sp));
      // iOS-Madeira 2026-05-18: inline bounds-guard (Tier-2). Same as
      // linked-path CALL push.
      {
        ARMEmitter::ForwardLabel l_callret_ok;
        /* iOS-Madeira ml263: tightened to a 4MB window -- see the first CALL push site. */
""",
"""      ARMEmitter::ForwardLabel l_CallReturn;
#ifdef ARCHITECTURE_arm64ec
      if (CallRetStackUsed()) {
        // iOS-Madeira 2026-05-13: see note above — reload REG_CALLRET_SP (x17)
        // from State.callret_sp before pushing the call-return frame, since
        // native ARM64EC returns leave x17 pointing at an arbitrary RX page.
        ldr(REG_CALLRET_SP, STATE, offsetof(FEXCore::Core::CpuStateFrame, State.callret_sp));
        // iOS-Madeira 2026-05-18: inline bounds-guard (Tier-2). Same as
        // linked-path CALL push.
        ARMEmitter::ForwardLabel l_callret_ok;
        /* iOS-Madeira ml263: tightened to a 4MB window -- see the first CALL push site. */
""",
        ),
        (
"""      }
#endif
#ifdef FEX_CALLRET_STACK_UNUSED
      // No shadow-stack push in this module; this callsite is a register blr that is never
      // backpatched, so no marker is needed either.
      if (!Op->CallReturnBlock.IsInvalid()) {
        PendingCallReturnTargetLabel = &CallReturnTargets.try_emplace(Op->CallReturnBlock.ID()).first->second;
      }
#else
      if (!Op->CallReturnBlock.IsInvalid()) {
        auto CallReturnAddressReg = GetReg(Op->CallReturnAddress).X();
        PendingCallReturnTargetLabel = &CallReturnTargets.try_emplace(Op->CallReturnBlock.ID()).first->second;
""",
"""      }
#endif
      if (!CallRetStackUsed()) {
        // No shadow-stack push in this module; this callsite is a register blr that is never
        // backpatched, so no marker is needed either.
        if (!Op->CallReturnBlock.IsInvalid()) {
          PendingCallReturnTargetLabel = &CallReturnTargets.try_emplace(Op->CallReturnBlock.ID()).first->second;
        }
      } else if (!Op->CallReturnBlock.IsInvalid()) {
        auto CallReturnAddressReg = GetReg(Op->CallReturnAddress).X();
        PendingCallReturnTargetLabel = &CallReturnTargets.try_emplace(Op->CallReturnBlock.ID()).first->second;
""",
        ),
        (
"""        stp<ARMEmitter::IndexType::PRE>(ARMEmitter::XReg::zr, ARMEmitter::XReg::zr, REG_CALLRET_SP, -0x10);
      }
#endif
#ifdef ARCHITECTURE_arm64ec
      /* Write back post-push x17 so dispatcher LoopTop reload sees it. */
      str(REG_CALLRET_SP, STATE, offsetof(FEXCore::Core::CpuStateFrame, State.callret_sp));
#endif
      blr(TMP2);
""",
"""        stp<ARMEmitter::IndexType::PRE>(ARMEmitter::XReg::zr, ARMEmitter::XReg::zr, REG_CALLRET_SP, -0x10);
      }
#ifdef ARCHITECTURE_arm64ec
      if (CallRetStackUsed()) {
        /* Write back post-push x17 so dispatcher LoopTop reload sees it. */
        str(REG_CALLRET_SP, STATE, offsetof(FEXCore::Core::CpuStateFrame, State.callret_sp));
      }
#endif
      blr(TMP2);
""",
        ),
    ),
    "FEXCore/Source/Interface/Core/Dispatcher/Dispatcher.cpp": (
        (
"""  add(ARMEmitter::Size::i64Bit, ARMEmitter::Reg::rsp, TMP1, 0);

  ldr(REG_CALLRET_SP, STATE_PTR(CpuStateFrame, State.callret_sp));

  FillSpecialRegs(TMP1, TMP2, false, true);
""",
"""  add(ARMEmitter::Size::i64Bit, ARMEmitter::Reg::rsp, TMP1, 0);

  if (CallRetStackUsed()) {
    ldr(REG_CALLRET_SP, STATE_PTR(CpuStateFrame, State.callret_sp));
  }

  FillSpecialRegs(TMP1, TMP2, false, true);
""",
        ),
        (
"""  (void)tbz(TMP1, 0, &l_NotECCode);

  str(REG_CALLRET_SP, STATE_PTR(CpuStateFrame, State.callret_sp));

  add(ARMEmitter::Size::i64Bit, ARMEmitter::Reg::rsp, StaticRegisters[X86State::REG_RSP], 0);
""",
"""  (void)tbz(TMP1, 0, &l_NotECCode);

  if (CallRetStackUsed()) {
    str(REG_CALLRET_SP, STATE_PTR(CpuStateFrame, State.callret_sp));
  }

  add(ARMEmitter::Size::i64Bit, ARMEmitter::Reg::rsp, StaticRegisters[X86State::REG_RSP], 0);
""",
        ),
        (
"""    // (e.g. underflow during prior dispatch), reset before stp to avoid
    // writing the sentinel into JIT code memory.
    {
      ARMEmitter::ForwardLabel l_callret_ok;
      ldr(TMP1, STATE, offsetof(FEXCore::Core::CpuStateFrame, State.callret_sp_base));
""",
"""    // (e.g. underflow during prior dispatch), reset before stp to avoid
    // writing the sentinel into JIT code memory.
    if (CallRetStackUsed()) {
      ARMEmitter::ForwardLabel l_callret_ok;
      ldr(TMP1, STATE, offsetof(FEXCore::Core::CpuStateFrame, State.callret_sp_base));
""",
        ),
        (
"""    }
#endif
#ifndef FEX_CALLRET_STACK_UNUSED
    // The sentinel makes a RET taken inside the callback pop {0,0} and mispredict. Without a
    // shadow stack there is neither a push nor a pop, so there is nothing to seed.
    stp<ARMEmitter::IndexType::PRE>(ARMEmitter::XReg::zr, ARMEmitter::XReg::zr, REG_CALLRET_SP, -0x10);
#endif

    // Now go back to the regular dispatcher loop
""",
"""    }
#endif
    if (CallRetStackUsed()) {
      // The sentinel makes a RET taken inside the callback pop {0,0} and mispredict. Without a
      // shadow stack there is neither a push nor a pop, so there is nothing to seed.
      stp<ARMEmitter::IndexType::PRE>(ARMEmitter::XReg::zr, ARMEmitter::XReg::zr, REG_CALLRET_SP, -0x10);
    }

    // Now go back to the regular dispatcher loop
""",
        ),
    ),
    "FEXCore/Source/Interface/Core/JIT/JIT.cpp": (
        (
"""
  // The known-call marker is the `adr TMP1, <l_CallReturn>` a linked CALL emits before its branch.
  // Without the shadow-stack push (FEX_CALLRET_STACK_UNUSED) it sits one instruction closer to the
  // callsite and its immediate is 4 bytes smaller; both values come from the same macro as the
  // emission in BranchOps.cpp.
#ifdef FEX_CALLRET_STACK_UNUSED
  constexpr uint32_t KnownCallMarkerImm = 0x8;
  constexpr uintptr_t KnownCallMarkerDisp = 0x4;
#else
  constexpr uint32_t KnownCallMarkerImm = 0xC;
  constexpr uintptr_t KnownCallMarkerDisp = 0x8;
#endif
  uint32_t ExpectedKnownCallMarkerInst = 0;
  ARMEmitter::Emitter ExpectedKnownCallMarkerEmit(reinterpret_cast<uint8_t*>(&ExpectedKnownCallMarkerInst), 4);
""",
"""
  // The known-call marker is the `adr TMP1, <l_CallReturn>` a linked CALL emits before its branch.
  // Without the shadow-stack push (CallRetStackUsed() false) it sits one instruction closer to the
  // callsite and its immediate is 4 bytes smaller; both values follow the same switch as the
  // emission in BranchOps.cpp.
  const uint32_t KnownCallMarkerImm = CallRetStackUsed() ? 0xC : 0x8;
  const uintptr_t KnownCallMarkerDisp = CallRetStackUsed() ? 0x8 : 0x4;
  uint32_t ExpectedKnownCallMarkerInst = 0;
  ARMEmitter::Emitter ExpectedKnownCallMarkerEmit(reinterpret_cast<uint8_t*>(&ExpectedKnownCallMarkerInst), 4);
""",
        ),
    ),
    "Source/Windows/ARM64EC/Module.cpp": (
        (
""" * Defined in FEXCore Arm64Emitter.cpp, where the JIT emitters also read it. */
extern "C" uint32_t IosTebTsdOffset;
/* uint32_t, not bool: a 1-byte global here misaligned the adrp/ldr pair
 * lld generates for the neighbouring word ("misaligned ldr/str offset"). */
""",
""" * Defined in FEXCore Arm64Emitter.cpp, where the JIT emitters also read it. */
extern "C" uint32_t IosTebTsdOffset;
/* Defined in FEXCore Arm64Emitter.cpp; read when the dispatcher and blocks are emitted. */
extern "C" uint32_t IosCallRetStackElided;
/* uint32_t, not bool: a 1-byte global here misaligned the adrp/ldr pair
 * lld generates for the neighbouring word ("misaligned ldr/str offset"). */
""",
        ),
        (
"""#endif

  CTX->InitCore();
  Exception::HandlerConfig.emplace(*CTX);
""",
"""#endif

#ifdef FEX_IOS_HOST
  {
    /* Nothing on iOS reads the call-ret shadow stack (CallRetStackUsed in Arm64Emitter.h), so the JIT
     * does not maintain it. Set before InitCore, which emits the dispatcher.
     * MADEIRA_FEX_CALLRET_ELIDE=0 keeps the stack. */
    const char* CallRetOpt = getenv("MADEIRA_FEX_CALLRET_ELIDE");
    IosCallRetStackElided = !(CallRetOpt && strcmp(CallRetOpt, "0") == 0);
    LogMan::Msg::IFmt("FEX: callret-elide-v1 madeira-bcd enabled={}", IosCallRetStackElided != 0);
  }
#endif
  CTX->InitCore();
  Exception::HandlerConfig.emplace(*CTX);
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
