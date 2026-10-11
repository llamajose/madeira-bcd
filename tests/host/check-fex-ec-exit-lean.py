#!/usr/bin/env python3
"""Check the iOS ARM64EC overlay that adds ExitFunctionECJit, the JIT's lean exit into ARM64EC code.

Needs the FEX submodule and a host C++ compiler; no Wine, FEX build or game.
1. Applied after the other ARM64EC overlays in build order, the overlay is
   idempotent and refuses a moved anchor or a partial overlay.
2. Module.S: ExitFunctionECJit reads x28 only at the IOS_FRAME_* offsets,
   writes no register but x4, x16, x17 and x23, has no FPCR or TPIDRRO_EL0
   access, stores InSimulation before it loads the doorbell, and branches only
   inside itself, into ExitFunctionEC's translation tail or to the suspend
   point.
3. CoreState.h compiled on the host for iOS ARM64EC: SuspendDoorbell and the
   three new fields sit at the offsets Module.S hardcodes and Module.cpp's
   static_asserts name; every existing field keeps its offset.
4. With a clang that targets AArch64 and Python's unicorn module (not on the
   macOS CI runner, where this part is skipped with a note): the patched
   Module.S is assembled, linked in memory, and ExitFunctionEC and
   ExitFunctionECJit run from the same states (alias hint hits and misses,
   walks, no translation, a retired entry, aligned and misaligned stacks, an
   exit-thunk return, the doorbell): same stop, registers, NZCV and memory
   apart from the call counters and the last-hit pointers; the counts add up
   and the per-thread hit is used.
5. The frontend: ExitFunctionECJit only without AFP and not with
   MADEIRA_FEX_EC_EXIT_LEAN=0; ThreadInit stores the CPU area in the frame
   before it installs the exit and publishes the frame.
"""
from pathlib import Path
import importlib.util
import os
import re
import struct
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ev = load('fexpatch_eval', root / 'tests/host/fexpatch_eval.py')
overlay = load('ec_exit_lean', root / 'tools/patch-fex-ios-ec-exit-lean.py')

CORE_STATE = 'FEXCore/include/FEXCore/Core/CoreState.h'
ASM = 'Source/Windows/ARM64EC/Module.S'
MODULE = 'Source/Windows/ARM64EC/Module.cpp'
assert set(overlay.EDITS) == {CORE_STATE, ASM, MODULE}

# 1. The overlay on the output of the overlays before it.
with tempfile.TemporaryDirectory() as tmp:
    fex, applied = ev.chain_tree(root, tmp, stop_before='patch-fex-ios-ec-exit-lean.py')
    before = {rel: (fex / rel).read_text() for rel in overlay.EDITS}
assert 'patch-fex-ios-ec-bitmap-cache.py' in applied and 'patch-fex-ios-ec-ret.py' in applied
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

# 2. The stub's text.
asm = patched[ASM]
assert before[ASM] in asm.replace(asm[asm.index('#ifdef FEX_IOS_HOST\n// FEXCore::Core::CpuStateFrame offsets'):
                                     asm.index('  // Called into by FEXCore\n')], ''), 'ExitFunctionEC itself changed'
DEFINES = dict((m.group(1), int(m.group(2), 0)) for m in re.finditer(r'#define (IOS_FRAME_\w+) (\w+)', asm))
assert set(DEFINES) == {'IOS_FRAME_SUSPEND_DOORBELL', 'IOS_FRAME_CPU_AREA', 'IOS_FRAME_XP_COUNT', 'IOS_FRAME_ALIAS_HINT'}
stub = asm[asm.index('\nExitFunctionECJit:\n'):asm.index('#endif\n', asm.index('\nExitFunctionECJit:\n'))]
lines = [l.split('//')[0].strip() for l in stub.split('\n')[2:]]
lines = [l for l in lines if l]
labels = {l[:-1] for l in lines if l.endswith(':')}
code = [l for l in lines if not l.endswith(':')]
assert not any(re.search(r'\b(fpcr|tpidrro_el0|x30|lr)\b', l, re.I) for l in code), 'FPCR, TPIDRRO_EL0 or LR in the stub'
STORES = ('str', 'strb', 'stadd', 'ldadd')
for l in code:
    op, _, args = l.partition(' ')
    regs = re.findall(r'\b([xw])(\d+)\b', args)
    if 'x28' in args:
        m = re.fullmatch(r'\w+ [xw]\d+, \[x28, #(IOS_FRAME_\w+)\]', l)
        assert m and m.group(1) in DEFINES, f'x28 used other than as the frame base: {l}'
    if op in ('ldr', 'add', 'sub', 'and', 'mov', 'ubfx', 'adrp') and regs:
        assert regs[0][1] in ('4', '16', '17', '23'), f'writes a register outside the clobber set: {l}'
    if op == 'ldadd':
        assert regs[1][1] in ('4', '16', '17', '23'), l
    if op.startswith('b') or op in ('cbz', 'cbnz'):
        target = args.split(',')[-1].strip()
        assert target in labels or target in ('2f', 'ios_ec_xlate_hit', 'ios_ec_xlate_done', 'ExitFunctionSuspendPoint'), l
assert code.index('strb wzr, [x17, #0x0]') < code.index('ldr w17, [x28, #IOS_FRAME_SUSPEND_DOORBELL]') < \
    code.index('cbnz w17, ExitFunctionSuspendPoint'), 'InSimulation is cleared before the doorbell is read'
assert '\nios_ec_xlate_hit:\n' in before[ASM] and '\nios_ec_xlate_done:\n' in before[ASM]

# 3. The frame layout, compiled on the host.
FIELDS = ('State', 'State.rip', 'State.L1Pointer', 'SuspendDoorbell', 'Pointers', 'Pointers.ExitFunctionEC',
          'IosLastEnterECRip', 'IosGuestBranchHistory', 'IosEcCodeBitMap')
NEW = ('IosCpuArea', 'IosXpCallCount', 'IosAliasHint')
layout = r'''
#include <FEXCore/Core/CoreState.h>
#include <cstdio>
using Frame = FEXCore::Core::CpuStateFrame;
int main() {
''' + ''.join(f'  std::printf("%zu\\n", offsetof(Frame, {f}));\n' for f in FIELDS) + r'''
#ifdef NEW_FIELDS
''' + ''.join(f'  std::printf("%zu\\n", offsetof(Frame, {f}));\n' for f in NEW) + r'''
#endif
}
'''
results = {}
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    for label, text, extra in (('old', before[CORE_STATE], []), ('new', patched[CORE_STATE], ['-DNEW_FIELDS'])):
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
assert results['old'] == results['new'][:len(FIELDS)], 'an existing CpuStateFrame field moved'
offsets = dict(zip(FIELDS + NEW, results['new']))
assert offsets['IosCpuArea'] == offsets['IosEcCodeBitMap'] + 8, 'the new fields are appended'
for define, field in (('IOS_FRAME_SUSPEND_DOORBELL', 'SuspendDoorbell'), ('IOS_FRAME_CPU_AREA', 'IosCpuArea'),
                      ('IOS_FRAME_XP_COUNT', 'IosXpCallCount'), ('IOS_FRAME_ALIAS_HINT', 'IosAliasHint')):
    assert DEFINES[define] == offsets[field], (define, DEFINES[define], offsets[field])
    assert f'static_assert(offsetof(FEXCore::Core::CpuStateFrame, {field}) == {offsets[field]}, "{define} in Module.S");' \
        in patched[MODULE], define
assert DEFINES['IOS_FRAME_SUSPEND_DOORBELL'] % 4 == 0 and DEFINES['IOS_FRAME_SUSPEND_DOORBELL'] <= 16380
assert all(DEFINES[d] % 8 == 0 and DEFINES[d] <= 32760 for d in DEFINES if d != 'IOS_FRAME_SUSPEND_DOORBELL')
for macros in ({'FEX_IOS_HOST': 1}, {'ARCHITECTURE_arm64ec': 1}, {}):
    text = ev.preprocess(ev.strip_comments(patched[CORE_STATE]), macros)
    assert not any(f in text for f in NEW)

# 4. Both stubs under emulation.
emu = load('fexstub_emu', root / 'tests/host/fexstub_emu.py')
TEXT, STUBS, DATA, FRAME, CPUAREA, TEB, TSD, STACK, JIT = (emu.TEXT, emu.STUBS, emu.DATA, emu.FRAME, emu.CPUAREA,
                                                           emu.TEB, emu.TSD, emu.STACK, emu.JIT)
emulated = 0


def run(code, symbols, data_at, entry, state, stops):
    return emu.run(code, b'', entry, state, stops)


if emu.missing():
    print('note: ' + emu.missing() + ' unavailable; the emulation of both stubs is skipped (the text and layout checks '
          'above ran)')
else:
    code, _, symbols, data_at = emu.link(emu.assemble(asm))
    for name in ('IosXpFex', 'IosAliasEntries', 'IosAliasCount', 'IosAliasLast', 'X64ReturnInstr'):
        assert name in data_at, name
    D = DEFINES
    ENTRIES = [(0x7ff000000000, JIT, 0x10000), (0x7ff100000000, JIT + 0x100000, 0x10000), (0x7ff200000000, JIT + 0x200000, 0x10000)]

    def make_state(target, sp, entries=ENTRIES, frame_hint=None, global_hint=None, doorbell=0, kind='entry'):
        mem = {DATA: bytearray(0x100000), FRAME: bytearray(0x2000), CPUAREA: bytearray(0x1000), TEB: bytearray(0x2000),
               TSD: bytearray(0x1000), STACK: bytearray(0x10000), JIT: bytearray(0x400000), 0x60000000: bytearray(0x10000)}
        put = lambda base, off, fmt, value: struct.pack_into(fmt, mem[base], off, value)
        put(DATA, data_at['X64ReturnInstr'] - DATA, '<Q', 0x14000beef)
        for i, (pe, jit, size) in enumerate(entries):
            struct.pack_into('<QQQQ', mem[DATA], data_at['IosAliasEntries'] - DATA + 32 * i, pe, jit, size, 0)
        put(DATA, data_at['IosAliasCount'] - DATA, '<I', len(entries))
        entry_addr = lambda i: data_at['IosAliasEntries'] + 32 * i
        if global_hint is not None:
            put(DATA, data_at['IosAliasLast'] - DATA, '<Q', entry_addr(global_hint))
        if frame_hint is not None:
            put(FRAME, D['IOS_FRAME_ALIAS_HINT'], '<Q', entry_addr(frame_hint))
        put(FRAME, D['IOS_FRAME_CPU_AREA'], '<Q', CPUAREA)
        put(FRAME, D['IOS_FRAME_SUSPEND_DOORBELL'], '<I', doorbell)
        put(CPUAREA, 0, '<B', 1)                          # InSimulation
        put(CPUAREA, 0x20, '<Q', FRAME + D['IOS_FRAME_SUSPEND_DOORBELL'])
        put(TEB, 0x1788, '<Q', CPUAREA)
        put(TSD, 0x898, '<Q', TEB)
        put(STACK, sp - STACK, '<Q', 0x140001234)        # the x64 return address
        native = next((jit + target - pe for pe, jit, size in entries if size and pe <= target < pe + size), target)
        base = 0x60000000 if native >= 0x60000000 else JIT
        word = 0xd63f0200 if kind == 'exit-thunk' else 0x800  # blr x16, or an entry thunk 0x800 past the function
        struct.pack_into('<I', mem[base], native - 4 - base, word)
        regs = [0x5000 + r for r in range(31)]
        regs[28], regs[9] = FRAME, target
        stop = native if kind == 'exit-thunk' else native + 0x800
        return {'mem': mem, 'regs': regs, 'sp': sp}, {stop, symbols['ExitFunctionSuspendPoint']}

    COUNTERS = (data_at['IosXpFex'] - DATA, data_at['IosXpFex'] - DATA + 4496 * 8)
    LAST = data_at['IosAliasLast'] - DATA

    def same(a, b, kind):
        pc_a, regs_a, nzcv_a, mem_a, _ = a
        pc_b, regs_b, nzcv_b, mem_b, _ = b
        assert pc_a == pc_b, (hex(pc_a), hex(pc_b))
        skip = {4} if kind == 'exit-thunk' else set()  # x64 R10, volatile; ExitFunctionEC leaves its last scratch value
        if pc_a == symbols['ExitFunctionSuspendPoint']:
            skip = {4, 16, 17, 23}
        else:
            assert nzcv_a == nzcv_b
        assert [r for i, r in enumerate(regs_a) if i not in skip] == [r for i, r in enumerate(regs_b) if i not in skip], \
            [(i, hex(x), hex(y)) for i, (x, y) in enumerate(zip(regs_a, regs_b)) if x != y and i not in skip]
        for base in mem_a:
            x, y = bytearray(mem_a[base]), bytearray(mem_b[base])
            if base == DATA:
                x[COUNTERS[0]:COUNTERS[1]] = y[COUNTERS[0]:COUNTERS[1]] = bytes(COUNTERS[1] - COUNTERS[0])
                x[LAST:LAST + 8] = y[LAST:LAST + 8] = bytes(8)
            if base == FRAME:
                for f in ('IOS_FRAME_XP_COUNT', 'IOS_FRAME_ALIAS_HINT'):
                    x[D[f]:D[f] + 8] = y[D[f]:D[f] + 8] = bytes(8)
            assert x == y, f'memory at {base:#x} differs'
        assert mem_a[CPUAREA][0] == 0 and mem_b[CPUAREA][0] == 0, 'InSimulation must be cleared'

    def compare(target, sp, **kw):
        global emulated
        state, stops = make_state(target, sp, **kw)
        old = run(code, symbols, data_at, symbols['ExitFunctionEC'], state, stops)
        new = run(code, symbols, data_at, symbols['ExitFunctionECJit'], state, stops)
        same(old, new, kw.get('kind', 'entry'))
        emulated += 1
        return old, new

    ALIGNED, MISALIGNED = STACK + 0x8008, STACK + 0x8000  # RSP % 16 == 8 after a CALL; 0 when the caller left it so
    for target in (0x7ff000001000, 0x7ff100004560, 0x7ff20000fff0):
        for sp in (ALIGNED, MISALIGNED):
            for hints in ({}, {'frame_hint': 0, 'global_hint': 0}, {'frame_hint': 1, 'global_hint': 1},
                          {'frame_hint': 2, 'global_hint': 2}, {'frame_hint': 2, 'global_hint': 0}):
                old, new = compare(target, sp, **hints)
                hit = next(i for i, (pe, jit, size) in enumerate(ENTRIES) if pe <= target < pe + size)
                assert new[1][9] == ENTRIES[hit][1] + target - ENTRIES[hit][0], 'translated target'
                hint = struct.unpack_from('<Q', new[3][FRAME], D['IOS_FRAME_ALIAS_HINT'])[0]
                assert hint == data_at['IosAliasEntries'] + 32 * hit, 'the per-thread hit is recorded'
                assert struct.unpack_from('<Q', new[3][DATA], LAST)[0] == \
                    struct.unpack_from('<Q', make_state(target, sp, **hints)[0]['mem'][DATA], LAST)[0], \
                    'ExitFunctionECJit leaves the process-wide last hit alone'
            compare(target, sp, kind='exit-thunk', frame_hint=1, global_hint=1)
    # No entry covers the target: the PE address is used as is.
    compare(0x60001000, ALIGNED)
    compare(0x60001000, ALIGNED, frame_hint=0, global_hint=1)
    compare(0x60001000, ALIGNED, entries=[], kind='exit-thunk')
    # A retired entry (Size 0) under both hints.
    retired = [(0x7ff000000000, JIT, 0), ENTRIES[1], ENTRIES[2]]
    compare(0x7ff100000040, ALIGNED, entries=retired, frame_hint=0, global_hint=0)
    # The first byte past an entry belongs to the image mapped right after it, not to the hinted one.
    adjacent = ENTRIES + [(0x7ff100010000, JIT + 0x300000, 0x10000)]
    for hints in ({'frame_hint': 1, 'global_hint': 1}, {}):
        old, new = compare(0x7ff100010000, ALIGNED, entries=adjacent, **hints)
        assert new[1][9] == JIT + 0x300000, hex(new[1][9])
    # The doorbell: both stop at the suspend point with InSimulation cleared and the target untouched.
    old, new = compare(0x7ff100000040, ALIGNED, doorbell=1)
    assert old[0] == new[0] == symbols['ExitFunctionSuspendPoint'] and new[1][9] == 0x7ff100000040
    # A hit through the per-thread hint skips the walk.
    old, new = compare(0x7ff000000040, ALIGNED, frame_hint=0, global_hint=0)
    _, walked = compare(0x7ff000000040, ALIGNED)
    assert new[4] < walked[4], (new[4], walked[4])

    # Counting: 130 calls of one thread.
    def calls(entry, n):
        state, stops = make_state(0x7ff100000040, ALIGNED)
        mem = state['mem']
        for i in range(n):
            target = 0x7ff100000040 + 16 * i
            struct.pack_into('<I', mem[JIT], 0x100000 + 0x40 + 16 * i - 4, 0x800)
            regs = list(state['regs'])
            regs[9] = target
            st = {'mem': mem, 'regs': regs, 'sp': ALIGNED}
            pc, out, _, mem, _ = run(code, symbols, data_at, entry, st, {JIT + 0x100000 + 0x40 + 16 * i + 0x800})
            struct.pack_into('<Q', mem[STACK], ALIGNED - STACK, 0x140001234)
            mem[CPUAREA][0] = 1
        fex = mem[DATA][COUNTERS[0]:COUNTERS[1]]
        shards = sum(struct.unpack_from('<Q', fex, 64 + 64 * s)[0] for s in range(16))
        ring_index = struct.unpack_from('<Q', fex, 3136)[0]
        ring = [struct.unpack_from('<Q', fex, 3200 + 8 * k)[0] for k in range(ring_index)]
        return shards, ring, struct.unpack_from('<Q', mem[FRAME], D['IOS_FRAME_XP_COUNT'])[0]
    shards, ring, count = calls(symbols['ExitFunctionEC'], 130)
    assert shards == 130 and ring == [0x7ff100000040, 0x7ff100000040 + 16 * 64, 0x7ff100000040 + 16 * 128] and count == 0
    shards, ring, count = calls(symbols['ExitFunctionECJit'], 130)
    assert count == 130 and shards == 128 and ring == [0x7ff100000040 + 16 * 63, 0x7ff100000040 + 16 * 127], (shards, ring)

# 5. The frontend.
module = patched[MODULE]
init = module.index('NTSTATUS ProcessInit() {')
block = module[module.index('    const char* LeanOpt = getenv("MADEIRA_FEX_EC_EXIT_LEAN");'):]
block = block[:block.index('    LogMan::Msg::IFmt("FEX: ec-exit-lean-v1 madeira-bcd')]
assert init < module.index(block) < module.index('  CTX->InitCore();', init)
thread = module[module.index('NTSTATUS ThreadInit() {'):]
area = '  Thread->CurrentFrame->IosCpuArea = reinterpret_cast<uint64_t>(CPUArea.Area);\n'
pick = '  Thread->CurrentFrame->Pointers.ExitFunctionEC = reinterpret_cast<uintptr_t>(IosEcExitLean ? &ExitFunctionECJit : &ExitFunctionEC);\n'
assert thread.index(area) < thread.index(pick) < thread.index('  CPUArea.StateFrame() = Thread->CurrentFrame;') < \
    thread.index('  CPUArea.Area->SuspendDoorbell = reinterpret_cast<ULONG*>(&Thread->CurrentFrame->SuspendDoorbell);')
switch = r'''
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
struct { bool SupportsAFP; } HostFeatures;
static uint32_t IosEcExitLean = 7;
int main(int argc, char** argv) {
  HostFeatures.SupportsAFP = argv[1][0] == '1';
''' + block + r'''
  std::printf("%u\n", IosEcExitLean);
}
'''
with tempfile.TemporaryDirectory() as tmp:
    src = Path(tmp) / 'switch.cpp'
    exe = Path(tmp) / 'switch'
    src.write_text(switch)
    subprocess.run([os.environ.get('CXX', 'c++'), '-std=c++20', '-O1', '-Wall', '-Werror', str(src), '-o', str(exe)], check=True)
    for afp in ('0', '1'):
        for value, expect in ((None, '1'), ('0', '0'), ('1', '1'), ('', '1'), ('00', '1')):
            env = {k: v for k, v in os.environ.items() if k != 'MADEIRA_FEX_EC_EXIT_LEAN'}
            if value is not None:
                env['MADEIRA_FEX_EC_EXIT_LEAN'] = value
            got = subprocess.run([str(exe), afp], env=env, capture_output=True, text=True, check=True).stdout.strip()
            assert got == (expect if afp == '0' else '0'), (afp, value, got)

# Build wiring: after the EC bitmap cache, before the patched build; the tree is restored.
build = (root / 'tools/build-xtajit64.sh').read_text()
apply = 'python3 "$R/tools/patch-fex-ios-ec-exit-lean.py" "$R/FEX"\n'
assert build.count(apply) == 1
assert build.index('patch-fex-ios-ec-bitmap-cache.py" "$R/FEX"') < build.index(apply) < build.index('\nbuild\n', build.index(apply))
restore = [line for line in build.splitlines() if line.startswith('git -C FEX checkout --')]
assert len(restore) == 1 and all(f' {rel}' in restore[0] for rel in overlay.EDITS), 'every patched file must be restored'
workflow = (root / '.github/workflows/build-ipa.yml').read_text()
assert "b'ec-exit-lean-v1 madeira-bcd' not in module" in workflow
catalog = (root / 'build/tools/gen-config-catalog.py').read_text()
assert '"env.MADEIRA_FEX_EC_EXIT_LEAN"' in catalog and '"tools/patch-fex-ios-ec-exit-lean.py"' in catalog
assert 'key: "env.MADEIRA_FEX_EC_EXIT_LEAN"' in (root / 'app/Madeira/ConfigCatalog.generated.swift').read_text()

print(f'lean JIT exit: overlay idempotent after {len(applied)} build overlays, drift refused; stub text and frame offsets '
      f'checked' + (f'; {emulated} states run through both stubs agree, 130 calls counted' if emulated else '') +
      '; only without AFP, MADEIRA_FEX_EC_EXIT_LEAN=0 keeps ExitFunctionEC')
