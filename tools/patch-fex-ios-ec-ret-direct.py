#!/usr/bin/env python3
"""Return from ARM64EC code into the JIT without testing the EC code bitmap twice.

An entry thunk finishes through __os_arm64x_dispatch_ret (RetToEntryThunk in
Module.S) with the x64 return address in LR. RetToEntryThunk tested that
address against the EC code bitmap (TPIDRRO_EL0, the TEB's TSD slot,
TEB->Peb, PEB->EcCodeBitMap, the bitmap word: an mrs and four dependent loads)
and entered the JIT for x64 code, or went to ExitFunctionEC for ARM64EC code.

The dispatcher already decides the same thing for every address it cannot
find in L1: its loop top tests the bitmap and leaves for ExitFunctionEC, and
an ARM64EC address is never in L1 (blocks are only compiled for addresses
the loop top found outside the bitmap). With IosRetToJit set, RetToEntryThunk
enters the JIT at once; an x64 return address is entered from L1 or looked
up, an ARM64EC one reaches ExitFunctionEC through the loop top with the same
x9, sp and LR. The 11-instruction test becomes a 3-instruction flag test, 8
instructions less on every return from a call into ARM64EC code; the rare
ARM64EC target takes the longer way.

IosRetToJit lives in Module.S's own data: one more word in Module.cpp's data
moved a neighbour into a misaligned adrp/ldr pair at link time.
MADEIRA_FEX_EC_RET_DIRECT=0 at launch keeps the test in RetToEntryThunk.
Applied by tools/build-xtajit64.sh after patch-fex-ios-alias-xlate-bounds.py;
the pinned submodule is never updated.
"""
from pathlib import Path
import sys

MARKER = "ec-ret-direct-v1 madeira-bcd"
SUMMARY = 'FEX ARM64EC: returns from ARM64EC code enter the JIT without their own EC bitmap test (MADEIRA_FEX_EC_RET_DIRECT=0 keeps it)'

EDITS = {
    "Source/Windows/ARM64EC/Module.S": (
        (
'  ldr \\reg, [\\reg, #0x898]\n.endm\n#endif\n\n',
'  ldr \\reg, [\\reg, #0x898]\n.endm\n\n// Read by RetToEntryThunk below; ARM64EC/Module.cpp sets it at process init.\n.data\n.balign 8\n.globl IosRetToJit\nIosRetToJit:\n  .word 0\n.text\n#endif\n\n',
        ),
        (
"""RetToEntryThunk:
  mov x9, lr

check_target_ec:
""",
"""RetToEntryThunk:
  mov x9, lr
#ifdef FEX_IOS_HOST
  // An entry thunk returns to the x64 caller of its function. Enter the JIT at once: the dispatcher
  // tests the EC code bitmap at the loop top on an L1 miss, and an ARM64EC address is never in L1.
  // IosRetToJit is set at process init; 0 keeps the test here.
  adrp x16, IosRetToJit
  ldr w16, [x16, #:lo12:IosRetToJit]
  cbnz w16, enter_jit
#endif

check_target_ec:
""",
        ),
    ),
    "Source/Windows/ARM64EC/Module.cpp": (
        (
"""/* Non-zero when JIT exits use ExitFunctionECJit (Module.S) instead of ExitFunctionEC; set in ProcessInit. */
static uint32_t IosEcExitLean = 0;
#endif // FEX_IOS_HOST

""",
"""/* Non-zero when JIT exits use ExitFunctionECJit (Module.S) instead of ExitFunctionEC; set in ProcessInit. */
static uint32_t IosEcExitLean = 0;
/* Defined in Module.S and read by RetToEntryThunk: non-zero enters the JIT without testing the EC code
 * bitmap there. (Kept out of this file's data: another word here shifted a neighbour into a misaligned
 * adrp/ldr pair at link time.) */
extern "C" uint32_t IosRetToJit;
#endif // FEX_IOS_HOST

""",
        ),
        (
"""  CTX->InitCore();
  Exception::HandlerConfig.emplace(*CTX);
#ifdef FEX_IOS_HOST
  {
""",
"""  CTX->InitCore();
  Exception::HandlerConfig.emplace(*CTX);
#ifdef FEX_IOS_HOST
  {
    /* RetToEntryThunk (Module.S) enters the JIT without testing the EC code bitmap itself: the loop top
     * tests it on an L1 miss. MADEIRA_FEX_EC_RET_DIRECT=0 keeps the test in RetToEntryThunk. */
    const char* RetDirectOpt = getenv("MADEIRA_FEX_EC_RET_DIRECT");
    IosRetToJit = !(RetDirectOpt && strcmp(RetDirectOpt, "0") == 0);
    LogMan::Msg::IFmt("FEX: ec-ret-direct-v1 madeira-bcd enabled={}", IosRetToJit != 0);
  }
#endif
#ifdef FEX_IOS_HOST
  {
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
