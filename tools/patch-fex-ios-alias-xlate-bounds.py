#!/usr/bin/env python3
"""Answer most reverse alias translations without walking the alias table.

IosJitReverseTranslate (IosJitAlias.cpp) maps an address inside a module's
JIT-pool copy back to the module's PE address. CompileBlock asks it for every
guest RIP the dispatcher's L1 lookup misses, before it looks for a compiled
block (tens of thousands of times a second in a game), and the executable
range queries ask it too. Each call walked every entry of the table, one per
loaded image, to find that a RIP in a PE image is no alias.

The table now keeps the lowest JitBase and the highest JitBase + Size of any
entry it has held, widened before an entry is published and never narrowed.
An address outside them lies in no entry and is returned at once; inside
them the walk is unchanged. A lookup racing an add may miss the new entry,
as the walk could before.

No switch: the answer is the same for every address. Applied by
tools/build-xtajit64.sh after patch-fex-ios-ec-exit-lean.py; the pinned
submodule is never updated.
"""
from pathlib import Path
import sys

MARKER = "WidenJitBounds(JitBase, Size);"
SUMMARY = 'FEX ARM64EC: reverse alias translation skips the table walk outside the JIT copies'

EDITS = {
    "Source/Windows/ARM64EC/IosJitAlias.cpp": (
        (
"""IosAliasEntry* const g_Entries = IosAliasEntries;
volatile int& g_EntryCount = IosAliasCount;
}  // namespace

""",
"""IosAliasEntry* const g_Entries = IosAliasEntries;
volatile int& g_EntryCount = IosAliasCount;

/* The lowest JitBase and the highest JitBase + Size of any entry ever added, widened before the entry
 * is published and never narrowed: an address outside them is in no entry, so IosJitReverseTranslate
 * answers it without walking the table (every block lookup the dispatcher misses asks it). */
uint64_t g_JitLow = ~0ULL;
uint64_t g_JitHigh = 0;

void WidenJitBounds(uint64_t JitBase, uint64_t Size) {
  uint64_t Low = __atomic_load_n(&g_JitLow, __ATOMIC_RELAXED);
  while (JitBase < Low && !__atomic_compare_exchange_n(&g_JitLow, &Low, JitBase, true, __ATOMIC_RELEASE, __ATOMIC_RELAXED)) {
  }
  uint64_t High = __atomic_load_n(&g_JitHigh, __ATOMIC_RELAXED);
  while (JitBase + Size > High &&
         !__atomic_compare_exchange_n(&g_JitHigh, &High, JitBase + Size, true, __ATOMIC_RELEASE, __ATOMIC_RELAXED)) {
  }
}
}  // namespace

""",
        ),
        (
"""  }

  // Retire every entry whose PE range overlaps the incoming image: a live
  // image proves any overlapping entry is dead (two images cannot share a VA).
""",
"""  }

  WidenJitBounds(JitBase, Size);

  // Retire every entry whose PE range overlaps the incoming image: a live
  // image proves any overlapping entry is dead (two images cannot share a VA).
""",
        ),
        (
"""
uint64_t IosJitReverseTranslate(uint64_t Addr) {
  int count = g_EntryCount;
  for (int i = 0; i < count; i++) {
""",
"""
uint64_t IosJitReverseTranslate(uint64_t Addr) {
  if (Addr < __atomic_load_n(&g_JitLow, __ATOMIC_ACQUIRE) || Addr >= __atomic_load_n(&g_JitHigh, __ATOMIC_ACQUIRE)) {
    return Addr;
  }
  int count = g_EntryCount;
  for (int i = 0; i < count; i++) {
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
