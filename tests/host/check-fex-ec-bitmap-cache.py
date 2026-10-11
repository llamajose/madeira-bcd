#!/usr/bin/env python3
"""Check the iOS ARM64EC overlay that caches PEB->EcCodeBitMap in the thread's frame.

Needs the FEX submodule and a host C++ compiler; no Wine, FEX build or game.
1. Applied after the other ARM64EC overlays in build order, the overlay is
   idempotent and refuses a moved anchor or a partial overlay.
2. Partial evaluation of Dispatcher::EmitDispatcher: with IosEcCodeBitMapCached
   0 (the kill switch, or no pointer at ProcessInit) and on every non-iOS build
   it equals the unpatched source token for token; switched on, the loop top's
   EC code check differs only in where TMP1 gets the bitmap pointer from: one
   LDR of CpuStateFrame::IosEcCodeBitMap instead of the TPIDRRO_EL0 -> TEB ->
   PEB walk.
3. CoreState.h compiled on the host for iOS ARM64EC before and after: every
   existing field keeps its offset, IosEcCodeBitMap is appended at the old end
   of the frame, within reach of a scaled LDR; other builds do not have it.
4. The frontend: ProcessInit reads the pointer through the TEB and PEB offsets
   the dispatcher walks (a static_assert pins them) after the TSD offset is
   imported and before InitCore emits the dispatcher; ThreadInit stores it in
   the frame before the frame is published to the CPU area. The switch:
   unset or anything but "0" enables when the pointer is known, "0" or a null
   pointer keeps the walk.
"""
from pathlib import Path
import importlib.util
import os
import re
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ev = load('fexpatch_eval', root / 'tests/host/fexpatch_eval.py')
overlay = load('ec_bitmap_cache', root / 'tools/patch-fex-ios-ec-bitmap-cache.py')

CORE_STATE = 'FEXCore/include/FEXCore/Core/CoreState.h'
DISPATCH = 'FEXCore/Source/Interface/Core/Dispatcher/Dispatcher.cpp'
MODULE = 'Source/Windows/ARM64EC/Module.cpp'
assert set(overlay.EDITS) == {CORE_STATE, DISPATCH, MODULE}

# 1. The overlay on the output of the overlays before it.
with tempfile.TemporaryDirectory() as tmp:
    fex, applied = ev.chain_tree(root, tmp, stop_before='patch-fex-ios-ec-bitmap-cache.py')
    before = {rel: (fex / rel).read_text() for rel in overlay.EDITS}
assert 'patch-fex-ios-ec-ret.py' in applied and 'patch-fex-ios-branch-history.py' in applied
patched = overlay.patch(before)
assert all(patched[rel] != before[rel] for rel in overlay.EDITS)
assert overlay.patch(patched) == patched, 'not idempotent'
for rel, edits in overlay.EDITS.items():
    for old, new in edits:
        moved = dict(before)
        moved[rel] = moved[rel].replace(old, old.replace('\n', '\n ', 1) if '\n' in old else old + ' ', 1)
        try:
            overlay.patch(moved)
        except ValueError:
            pass
        else:
            raise AssertionError(f'{rel}: moved anchor accepted')
    if len(edits) > 1:
        partial = dict(patched)
        partial[rel] = partial[rel].replace(edits[0][1], edits[0][0], 1)
        try:
            overlay.patch(partial)
        except ValueError:
            pass
        else:
            raise AssertionError(f'{rel}: partial overlay accepted')
assert overlay.MARKER in patched[MODULE]

# 2. Partial evaluation of the dispatcher.
HEADER = 'void Dispatcher::EmitDispatcher() {'


def fold(text, on):
    """Evaluate `if (IosEcCodeBitMapCached) { A } else { B }` with a fixed value."""
    while True:
        m = re.search(r'\bif\s*\(\s*IosEcCodeBitMapCached\s*\)\s*\{', text)
        if not m:
            assert 'IosEcCodeBitMapCached' not in text, 'unfolded IosEcCodeBitMapCached form'
            return text
        close = ev.match_brace(text, m.end() - 1)
        rest = text[close + 1:]
        r = re.match(r'\s*else\s*\{', rest)
        assert r, 'IosEcCodeBitMapCached without an else block'
        else_close = ev.match_brace(rest, r.end() - 1)
        keep = text[m.end():close] if on else rest[r.end():else_close]
        text = text[:m.start()] + '{' + keep + '}' + rest[else_close + 1:]


def norm(text):
    return [t for t in ev.tokens(text) if t not in '{}']


def dispatcher(src, macros, on=None):
    body = ev.body(ev.preprocess(ev.strip_comments(src[DISPATCH]), macros), HEADER)
    return fold(body, on) if on is not None else body


IOS_EC = {'FEX_IOS_HOST': 1, 'ARCHITECTURE_arm64ec': 1, '_WIN32': 1}
CONFIGS = {
    'iOS ARM64EC, switched off': (IOS_EC, False),
    'iOS WOW64': ({'FEX_IOS_HOST': 1, '_WIN32': 1}, None),
    'ARM64EC': ({'ARCHITECTURE_arm64ec': 1, '_WIN32': 1}, None),
    'Linux arm64': ({}, None),
}
for name, (macros, on) in CONFIGS.items():
    assert norm(dispatcher(before, macros)) == norm(dispatcher(patched, macros, on)), f'EmitDispatcher differs under {name}'

off = re.sub(r'\s+', ' ', dispatcher(patched, IOS_EC, False))
on = re.sub(r'\s+', ' ', dispatcher(patched, IOS_EC, True))
walk = ('{ mrs(TMP1, ARMEmitter::SystemRegister::TPIDRRO_EL0); and_(ARMEmitter::Size::i64Bit, TMP1, TMP1, ~7ULL); '
        'ldr(TMP1, TMP1, IosTebTsdOffset); ldr(TMP1, TMP1, TEB_PEB_OFFSET); ldr(TMP1, TMP1, PEB_EC_CODE_BITMAP_OFFSET); }')
cached = '{ ldr(TMP1, STATE_PTR(CpuStateFrame, IosEcCodeBitMap)); }'
assert off.count(walk) == 1 and on.count(cached) == 1, 'EC code check shape'
assert on.replace(cached, walk) == off, 'switched on, more than the bitmap pointer load changed'
check = on[on.index(cached):]
assert check.index('lsr(ARMEmitter::Size::i64Bit, TMP2, RipReg, 18);') < check.index('(void)tbz(TMP1, 0, &l_NotECCode);'), \
    'the bitmap test follows the load'
assert on.count('IosEcCodeBitMap') == 1, 'the cached pointer is read only by the loop top'

# 3. The frame layout, compiled on the host.
FIELDS = ('State', 'State.rip', 'State.L1Pointer', 'State.callret_sp', 'ReturningStackLocation', 'InSyscallInfo',
          'SignalHandlerRefCounter', 'SynchronousFaultData', 'Thread', 'SuspendDoorbell', 'Pointers',
          'Pointers.ExitFunctionEC', 'Pointers.DispatcherLoopTop', 'IosLastEnterECRip', 'IosLastCallbackRip',
          'IosLastCallbackLR', 'IosGuestBranchHistory')
layout = r'''
#include <FEXCore/Core/CoreState.h>
#include <cstdio>
using Frame = FEXCore::Core::CpuStateFrame;
int main() {
''' + ''.join(f'  std::printf("%zu\\n", offsetof(Frame, {f}));\n' for f in FIELDS) + r'''
  std::printf("%zu\n", sizeof(Frame));
#ifdef NEW_FIELD
  std::printf("%zu\n", offsetof(Frame, IosEcCodeBitMap));
#endif
}
'''
results = {}
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    for label, text, extra in (('old', before[CORE_STATE], []), ('new', patched[CORE_STATE], ['-DNEW_FIELD'])):
        inc = tmp / label / 'FEXCore/Core'
        inc.mkdir(parents=True)
        (inc / 'CoreState.h').write_text(text)
        (tmp / f'{label}.cpp').write_text(layout)
        subprocess.run([os.environ.get('CXX', 'c++'), '-std=c++20', '-w', '-DARCHITECTURE_arm64ec', '-DFEX_IOS_HOST=1', *extra,
                        '-I', str(tmp / label), '-I', str(root / 'FEX/FEXCore/include'), '-I', str(root / 'FEX/FEXHeaderUtils'),
                        '-I', str(root / 'FEX/External/fmt/include'), str(tmp / f'{label}.cpp'), '-o', str(tmp / label / 'run')],
                       check=True)
        results[label] = [int(x) for x in subprocess.run([str(tmp / label / 'run')], capture_output=True, text=True,
                                                         check=True).stdout.split()]
old_offsets, new_offsets = results['old'][:len(FIELDS)], results['new'][:len(FIELDS)]
assert old_offsets == new_offsets, 'an existing CpuStateFrame field moved'
old_size, new_size, field = results['old'][-1], results['new'][-2], results['new'][-1]
last_end = old_offsets[FIELDS.index('IosGuestBranchHistory')] + 272  # the old last field
assert field == last_end and new_size >= field + 8 and new_size >= old_size, (old_size, field, new_size)
assert field % 8 == 0 and field <= 32760
for macros in ({'FEX_IOS_HOST': 1}, {'ARCHITECTURE_arm64ec': 1}, {}):
    assert 'IosEcCodeBitMap' not in ev.preprocess(ev.strip_comments(patched[CORE_STATE]), macros)

# 4. The frontend.
module = patched[MODULE]
init = module.index('NTSTATUS ProcessInit() {')
block = module[module.index('    static_assert(offsetof(__TEB, Peb) == 0x60'):]
block = block[:block.index('    LogMan::Msg::IFmt("FEX: ec-bitmap-cache-v1 madeira-bcd')]
assert init < module.index('IosTebTsdOffset = *Published;', init) < module.index(block) < module.index('  CTX->InitCore();', init)
assert '0x60' in block and '0x368' in block
emitter_h = (root / 'FEX/FEXCore/Source/Interface/Core/ArchHelpers/Arm64Emitter.h').read_text()
assert 'constexpr size_t TEB_PEB_OFFSET = 0x60;' in emitter_h and 'constexpr size_t PEB_EC_CODE_BITMAP_OFFSET = 0x368;' in emitter_h
thread = module[module.index('NTSTATUS ThreadInit() {'):]
store = '  Frame->IosEcCodeBitMap = IosEcCodeBitMapValue;\n'
assert thread.count(store) == 1 and thread.index('CTX->CreateThread(0, 0);') < thread.index(store) < \
    thread.index('  CPUArea.StateFrame() = Thread->CurrentFrame;'), 'the frame gets the pointer before it is published'
switch = r'''
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
struct __PEB { char Pad[0x368]; void* EcCodeBitMap; };
struct __TEB { char Pad[0x60]; __PEB* Peb; };
static __PEB Peb;
static __TEB Teb;
static __TEB* IOSLoadTEB() { return &Teb; }
static uint32_t IosEcCodeBitMapCached = 7;
static uint64_t IosEcCodeBitMapValue = 7;
int main(int argc, char** argv) {
  Teb.Peb = &Peb;
  Peb.EcCodeBitMap = reinterpret_cast<void*>(std::strtoull(argv[1], nullptr, 0));
''' + block + r'''
  std::printf("%u %llu\n", IosEcCodeBitMapCached, (unsigned long long)IosEcCodeBitMapValue);
}
'''
with tempfile.TemporaryDirectory() as tmp:
    src = Path(tmp) / 'switch.cpp'
    exe = Path(tmp) / 'switch'
    src.write_text(switch)
    subprocess.run([os.environ.get('CXX', 'c++'), '-std=c++20', '-O1', '-Wall', '-Werror', str(src), '-o', str(exe)], check=True)
    for bitmap in (0x7fe00000, 0):
        for value, expect in ((None, '1'), ('0', '0'), ('1', '1'), ('', '1'), ('00', '1')):
            env = {k: v for k, v in os.environ.items() if k != 'MADEIRA_FEX_EC_BITMAP_CACHE'}
            if value is not None:
                env['MADEIRA_FEX_EC_BITMAP_CACHE'] = value
            got = subprocess.run([str(exe), str(bitmap)], env=env, capture_output=True, text=True, check=True).stdout.split()
            assert got == [expect if bitmap else '0', str(bitmap)], (bitmap, value, got)
assert 'extern "C" uint32_t IosEcCodeBitMapCached = 0;' in patched[DISPATCH]

# Build wiring: after the EC-return overlay, before the patched build; the tree is restored.
build = (root / 'tools/build-xtajit64.sh').read_text()
apply = 'python3 "$R/tools/patch-fex-ios-ec-bitmap-cache.py" "$R/FEX"\n'
assert build.count(apply) == 1
assert build.index('patch-fex-ios-ec-ret.py" "$R/FEX"') < build.index(apply) < build.index('\nbuild\n', build.index(apply))
restore = [line for line in build.splitlines() if line.startswith('git -C FEX checkout --')]
assert len(restore) == 1 and all(f' {rel}' in restore[0] for rel in overlay.EDITS), 'every patched file must be restored'
workflow = (root / '.github/workflows/build-ipa.yml').read_text()
assert "b'ec-bitmap-cache-v1 madeira-bcd' not in module" in workflow
catalog = (root / 'build/tools/gen-config-catalog.py').read_text()
assert '"env.MADEIRA_FEX_EC_BITMAP_CACHE"' in catalog and '"tools/patch-fex-ios-ec-bitmap-cache.py"' in catalog
assert 'key: "env.MADEIRA_FEX_EC_BITMAP_CACHE"' in (root / 'app/Madeira/ConfigCatalog.generated.swift').read_text()

print(f'EC bitmap cache: overlay idempotent after {len(applied)} build overlays, drift refused; EmitDispatcher equal '
      f'switched off and on every other build; switched on only the bitmap pointer load differs; {len(FIELDS)} frame '
      f'offsets unchanged, IosEcCodeBitMap appended at {field}; ProcessInit reads it before InitCore, ThreadInit stores '
      'it before publishing the frame; MADEIRA_FEX_EC_BITMAP_CACHE=0 or a null pointer keeps the walk')
