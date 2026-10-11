#!/usr/bin/env python3
"""Check the overlay that lets IosJitReverseTranslate skip the alias-table walk.

Needs the FEX submodule and a host C++ compiler; no Wine, FEX build or game.
1. Applied after the other ARM64EC overlays in build order, the overlay is
   idempotent and refuses a moved anchor or a partial overlay.
2. The real BTCpu64IosAddAliasMapping and IosJitReverseTranslate, before and
   after, compiled on the host and fed the same 4000 random registrations
   (new images, re-registrations, PE and JIT overlaps that retire entries,
   reused slots, sub-floor windows, a full table): every reverse translation
   of addresses inside, at the edges of, between and far outside the copies
   gives the same answer, and the new one walks the table only for addresses
   inside the JIT bounds.
"""
from pathlib import Path
import importlib.util
import os
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ev = load('fexpatch_eval', root / 'tests/host/fexpatch_eval.py')
overlay = load('alias_xlate_bounds', root / 'tools/patch-fex-ios-alias-xlate-bounds.py')

ALIAS = 'Source/Windows/ARM64EC/IosJitAlias.cpp'
assert set(overlay.EDITS) == {ALIAS}

# 1. The overlay on the output of the overlays before it.
with tempfile.TemporaryDirectory() as tmp:
    fex, applied = ev.chain_tree(root, tmp, stop_before='patch-fex-ios-alias-xlate-bounds.py')
    before = {rel: (fex / rel).read_text() for rel in overlay.EDITS}
assert 'patch-fex-ios-alias-retire-jit.py' in applied and 'patch-fex-ios-ec-exit-lean.py' in applied
patched = overlay.patch(before)
assert patched[ALIAS] != before[ALIAS] and overlay.MARKER in patched[ALIAS]
assert overlay.patch(patched) == patched, 'not idempotent'
for old, new in overlay.EDITS[ALIAS]:
    moved = {ALIAS: before[ALIAS].replace(old, old.replace('\n', '\n ', 1), 1)}
    try:
        overlay.patch(moved)
    except ValueError:
        pass
    else:
        raise AssertionError('moved anchor accepted')
partial = {ALIAS: patched[ALIAS].replace(overlay.EDITS[ALIAS][0][1], overlay.EDITS[ALIAS][0][0], 1)}
try:
    overlay.patch(partial)
except ValueError:
    pass
else:
    raise AssertionError('partial overlay accepted')


# 2. Differential run of the real functions.
def unit(src):
    text = ev.strip_comments(src)
    entry = text[text.index('struct IosAliasEntry {'):text.index('constexpr int kMaxEntries = 256;') + len('constexpr int kMaxEntries = 256;')]
    store = text[text.index('IosAliasEntry IosAliasEntries[kMaxEntries];'):]
    store = store[:store.index('}', store.index('IosAliasEntry* volatile IosAliasLast'))]
    anon = text.index('namespace {', text.index('IosAliasEntry* volatile IosAliasLast'))
    anon = text[anon:ev.match_brace(text, text.index('{', anon)) + 1]
    add = ev.body(text, 'void BTCpu64IosAddAliasMapping(uint64_t PeBase, uint64_t JitBase, uint64_t Size) {')
    rev = ev.body(text, 'uint64_t IosJitReverseTranslate(uint64_t Addr) {')
    return r'''
#include <cstdint>
#include <cstdio>
#include <cstdlib>
static constexpr uint64_t kIosSubfloorFloor = 0x100000000ull;
static unsigned long long Subfloor;
static bool IosSubfloorAdd(uint64_t, uint64_t, uint64_t) { ++Subfloor; return true; }
static unsigned long long Steps;
''' + entry + '\nextern "C" {\n' + store + '}\n' + anon + '\nextern "C" {\n' + add + '\n' + \
        rev.replace('for (int i = 0; i < count; i++) {', 'for (int i = 0; i < count; i++) {\n    ++Steps;', 1) + r'''
}
static uint64_t Seed = 0x9e3779b97f4a7c15ull;
static uint64_t Next() { Seed ^= Seed << 13; Seed ^= Seed >> 7; Seed ^= Seed << 17; return Seed; }
int main() {
  const uint64_t Pool = 0x11c000000ull, PoolSize = 0x12000000ull;
  unsigned long long Hash = 1469598103934665603ull, Queries = 0, Outside = 0, OutsideSteps = 0;
  for (int Op = 0; Op < 4000; ++Op) {
    const uint64_t Kind = Next() % 16;
    uint64_t Pe = 0x140000000ull + (Next() % 64) * 0x200000ull + (Next() % 4) * 0x1000ull;
    uint64_t Jit = Pool + (Next() % 128) * 0x200000ull + (Next() % 4) * 0x1000ull;
    uint64_t Size = 0x1000ull * (1 + Next() % 0x300);
    if (Kind == 0) Pe = 0x10000000ull + (Next() % 16) * 0x100000ull;       // sub-floor window
    if (Kind == 1) Pe = 0x7ff000000000ull + (Next() % 8) * 0x1000000ull;  // a high image
    if (Kind == 2 && IosAliasCount) {                                       // re-register a live entry
      const IosAliasEntry& E = IosAliasEntries[Next() % IosAliasCount];
      Pe = E.PeBase; Jit = E.JitBase; Size = E.Size ? E.Size : Size;
    }
    BTCpu64IosAddAliasMapping(Pe, Jit, Size);
    for (int Q = 0; Q < 24; ++Q) {
      uint64_t Addr;
      const uint64_t Pick = Next() % 8;
      const IosAliasEntry& E = IosAliasEntries[IosAliasCount ? Next() % IosAliasCount : 0];
      if (Pick == 0) Addr = E.JitBase;
      else if (Pick == 1) Addr = E.JitBase + E.Size - 1;
      else if (Pick == 2) Addr = E.JitBase + E.Size;
      else if (Pick == 3) Addr = E.JitBase - 1;
      else if (Pick == 4) Addr = Pool + Next() % PoolSize;
      else if (Pick == 5) Addr = 0x140000000ull + Next() % 0x10000000ull;   // guest PE images
      else if (Pick == 6) Addr = 0x7ff000000000ull + Next() % 0x10000000ull;
      else Addr = Next();
      const unsigned long long Before = Steps;
      const uint64_t R = IosJitReverseTranslate(Addr);
      Hash = (Hash ^ R) * 1099511628211ull;
      ++Queries;
      if (Addr < Pool || Addr >= Pool + PoolSize + 0x400000ull) {
        ++Outside;
        OutsideSteps += Steps - Before;
      }
    }
  }
  std::printf("%llx %llu %llu %llu %d %llu\n", Hash, Queries, Outside, OutsideSteps, (int)IosAliasCount, Subfloor);
}
'''


results = {}
with tempfile.TemporaryDirectory() as tmp:
    for label, src in (('old', before[ALIAS]), ('new', patched[ALIAS])):
        cpp, exe = Path(tmp) / f'{label}.cpp', Path(tmp) / label
        cpp.write_text(unit(src))
        subprocess.run([os.environ.get('CXX', 'c++'), '-std=c++20', '-O1', '-w', str(cpp), '-o', str(exe)], check=True)
        results[label] = subprocess.run([str(exe)], capture_output=True, text=True, check=True).stdout.split()
old, new = results['old'], results['new']
assert old[0] == new[0] and old[1:3] == new[1:3] and old[4:] == new[4:], (old, new)
queries, outside = int(new[1]), int(new[2])
assert int(new[4]) >= 32 and int(new[5]) > 0, 'the run grew the table and registered sub-floor windows'
assert outside > queries // 4 and int(old[3]) > 0 and int(new[3]) == 0, 'addresses outside the JIT copies skip the walk'

# Build wiring: after the lean exit, before the patched build; the tree is restored.
build = (root / 'tools/build-xtajit64.sh').read_text()
apply = 'python3 "$R/tools/patch-fex-ios-alias-xlate-bounds.py" "$R/FEX"\n'
assert build.count(apply) == 1
assert build.index('patch-fex-ios-ec-exit-lean.py" "$R/FEX"') < build.index(apply) < build.index('\nbuild\n', build.index(apply))
restore = [line for line in build.splitlines() if line.startswith('git -C FEX checkout --')]
assert len(restore) == 1 and f' {ALIAS}' in restore[0], 'the patched file must be restored'

print(f'alias xlate bounds: overlay idempotent after {len(applied)} build overlays, drift refused; {queries} reverse '
      f'translations over 4000 registrations identical before and after; {outside} outside the copies answered without '
      f'a walk (was {old[3]} entry visits)')
