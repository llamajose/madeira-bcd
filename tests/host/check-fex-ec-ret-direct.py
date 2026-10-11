#!/usr/bin/env python3
"""Check the overlay that lets RetToEntryThunk enter the JIT without its own EC code bitmap test.

Needs the FEX submodule and a host C++ compiler; no Wine, FEX build or game.
1. Applied after the other ARM64EC overlays in build order, the overlay is
   idempotent and refuses a moved anchor or a partial overlay.
2. Module.S: RetToEntryThunk tests IosRetToJit right after `mov x9, lr` and
   otherwise falls into the unchanged check_target_ec; IosRetToJit is a word
   of Module.S's own 8-byte aligned .data, which Module.cpp only declares.
3. With a clang that targets AArch64 and Python's unicorn module (not on the
   macOS CI runner, where this part is skipped with a note): the patched and
   the unpatched Module.S run RetToEntryThunk for x64 and ARM64EC return
   addresses. Switched off, the patched stub reaches the same stop (EnterEC
   or ExitFunctionEC) with the same registers and memory as the unpatched one.
   Switched on, an x64 address reaches EnterEC in the same state with fewer
   instructions, and an ARM64EC address reaches EnterEC too (the dispatcher's
   loop top then sends it to ExitFunctionEC) with the same x9, sp and LR.
4. The frontend reads MADEIRA_FEX_EC_RET_DIRECT before any thread runs: unset
   or anything but "0" enables, "0" keeps the test.
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
emu = load('fexstub_emu', root / 'tests/host/fexstub_emu.py')
overlay = load('ec_ret_direct', root / 'tools/patch-fex-ios-ec-ret-direct.py')

ASM = 'Source/Windows/ARM64EC/Module.S'
MODULE = 'Source/Windows/ARM64EC/Module.cpp'
assert set(overlay.EDITS) == {ASM, MODULE}

# 1. The overlay on the output of the overlays before it.
with tempfile.TemporaryDirectory() as tmp:
    fex, applied = ev.chain_tree(root, tmp, stop_before='patch-fex-ios-ec-ret-direct.py')
    before = {rel: (fex / rel).read_text() for rel in overlay.EDITS}
assert 'patch-fex-ios-ec-exit-lean.py' in applied and 'patch-fex-ios-alias-xlate-bounds.py' in applied
patched = overlay.patch(before)
assert all(patched[rel] != before[rel] for rel in overlay.EDITS)
assert overlay.patch(patched) == patched, 'not idempotent'
for rel, edits in overlay.EDITS.items():
    for old, new in edits:
        moved = dict(before)
        moved[rel] = moved[rel].replace(old, old.replace('\n', '\n ', 1), 1)
        try:
            overlay.patch(moved)
        except ValueError:
            pass
        else:
            raise AssertionError(f'{rel}: moved anchor accepted')
    partial = dict(patched)
    partial[rel] = partial[rel].replace(edits[0][1], edits[0][0], 1)
    try:
        overlay.patch(partial)
    except ValueError:
        pass
    else:
        raise AssertionError(f'{rel}: partial overlay accepted')
assert overlay.MARKER in patched[MODULE]

# 2. The text.
asm = patched[ASM]
ret = asm[asm.index('\nRetToEntryThunk:\n'):asm.index('\ncheck_target_ec:\n')]
code = [l.split('//')[0].strip() for l in ret.split('\n')]
code = [l for l in code if l and not l.endswith(':')]
assert code == ['mov x9, lr', '#ifdef FEX_IOS_HOST', 'adrp x16, IosRetToJit', 'ldr w16, [x16, #:lo12:IosRetToJit]',
                'cbnz w16, enter_jit', '#endif'], code
check = lambda text: text[text.index('\ncheck_target_ec:\n'):text.index('\n  // __os_arm64x_dispatch_call_no_redirect')]
assert check(asm) == check(before[ASM]), 'check_target_ec changed'
data = re.search(r'\n\.data\n\.balign 8\n\.globl IosRetToJit\nIosRetToJit:\n  \.word 0\n\.text\n', asm)
assert data and asm.index('#ifdef FEX_IOS_HOST') < data.start() < asm.index('\nDispatchJump:\n'), 'IosRetToJit in Module.S data'
module = patched[MODULE]
assert 'extern "C" uint32_t IosRetToJit;' in module and not re.search(r'\buint32_t IosRetToJit\s*[={]', module)

# 3. Both versions under emulation.
emulated = 0
if emu.missing():
    print('note: ' + emu.missing() + ' unavailable; the emulation of RetToEntryThunk is skipped (the text checks above ran)')
else:
    builds = {}
    for label, text in (('old', before[ASM]), ('new', asm)):
        code, data_bytes, symbols, data_at = emu.link(emu.assemble(text))
        builds[label] = (code, data_bytes, symbols, data_at)
    ENTER_EC = emu.STUBS + 0x800
    PEB = emu.TEB + 0x1000

    def state(build, target, flag, ec):
        code, data_bytes, symbols, data_at = build
        mem = emu.blank_memory()
        mem[emu.DATA][:len(data_bytes)] = data_bytes
        put = lambda base, off, fmt, value: struct.pack_into(fmt, mem[base], off, value)
        if 'IosRetToJit' in data_at:
            put(emu.DATA, data_at['IosRetToJit'] - emu.DATA, '<I', flag)
        put(emu.TSD, 0x898, '<Q', emu.TEB)
        put(emu.TEB, 0x1788, '<Q', emu.CPUAREA)
        put(emu.TEB, 0x60, '<Q', PEB)
        bitmap = (emu.NATIVE - ((target >> 18) << 3)) % (1 << 64)  # the bitmap word for the target sits at NATIVE
        put(emu.TEB, 0x1000 + 0x368, '<Q', bitmap)
        put(emu.NATIVE, 0, '<Q', (1 << ((target >> 12) & 63)) if ec else 0)
        put(emu.CPUAREA, 0x40, '<Q', ENTER_EC)
        regs = [0x5000 + r for r in range(31)]
        regs[30] = target
        return {'mem': mem, 'regs': regs, 'sp': emu.STACK + 0x8000}, {ENTER_EC, symbols['ExitFunctionEC']}

    def go(label, target, flag, ec):
        build = builds[label]
        st, stops = state(build, target, flag, ec)
        out = emu.run(build[0], build[1], build[2]['RetToEntryThunk'], st, stops)
        return out, build[2]

    def same(a, b):
        assert a[0] == b[0] and a[1] == b[1] and a[2] == b[2], ([hex(x) for x in a[1]], [hex(x) for x in b[1]])
        for base in a[3]:
            x, y = a[3][base], b[3][base]
            if base == emu.DATA:
                continue  # the patched build has IosRetToJit in its data
            assert x == y, f'memory at {base:#x} differs'

    for target in (0x140001234, 0x7ff123456789, 0x1c0400010):
        for ec in (False, True):
            (old, old_syms) = go('old', target, 0, ec)
            (off, new_syms) = go('new', target, 0, ec)
            # The stop addresses differ between the builds only by the inserted instructions.
            expect = ENTER_EC if not ec else old_syms['ExitFunctionEC']
            assert old[0] == expect and off[0] == (ENTER_EC if not ec else new_syms['ExitFunctionEC'])
            old_n = (ENTER_EC if not ec else 0, old[1], old[2], old[3])
            off_n = (ENTER_EC if not ec else 0, off[1], off[2], off[3])
            same(old_n, off_n)
            assert off[4] == old[4] + 3, (off[4], old[4])  # adrp, ldr, cbnz
            (on, _) = go('new', target, 1, ec)
            assert on[0] == ENTER_EC and on[1][9] == target and on[1][30] == target and on[1][31] == emu.STACK + 0x8000
            assert on[3][emu.CPUAREA][0] == 1, 'InSimulation is set on the way into the JIT'
            if not ec:
                same((on[0], on[1], on[2], on[3]), (off[0], off[1], off[2], off[3]))
                assert off[4] - on[4] == 11, (off[4], on[4])  # the bitmap test it skips
                assert old[4] - on[4] == 8, (old[4], on[4])  # net of the flag test
            emulated += 1

# 4. The frontend switch.
init = module.index('NTSTATUS ProcessInit() {')
block = module[module.index('    const char* RetDirectOpt = getenv("MADEIRA_FEX_EC_RET_DIRECT");'):]
block = block[:block.index('    LogMan::Msg::IFmt("FEX: ec-ret-direct-v1 madeira-bcd')]
assert init < module.index(block) < module.index('NTSTATUS ThreadInit() {')
switch = r'''
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
static uint32_t IosRetToJit = 7;
int main() {
''' + block + r'''
  std::printf("%u\n", IosRetToJit);
}
'''
with tempfile.TemporaryDirectory() as tmp:
    src = Path(tmp) / 'switch.cpp'
    exe = Path(tmp) / 'switch'
    src.write_text(switch)
    subprocess.run([os.environ.get('CXX', 'c++'), '-std=c++20', '-O1', '-Wall', '-Werror', str(src), '-o', str(exe)], check=True)
    for value, expect in ((None, '1'), ('0', '0'), ('1', '1'), ('', '1'), ('00', '1')):
        env = {k: v for k, v in os.environ.items() if k != 'MADEIRA_FEX_EC_RET_DIRECT'}
        if value is not None:
            env['MADEIRA_FEX_EC_RET_DIRECT'] = value
        got = subprocess.run([str(exe)], env=env, capture_output=True, text=True, check=True).stdout.strip()
        assert got == expect, (value, got)

# Build wiring: after the alias bounds, before the patched build; the tree is restored.
build = (root / 'tools/build-xtajit64.sh').read_text()
apply = 'python3 "$R/tools/patch-fex-ios-ec-ret-direct.py" "$R/FEX"\n'
assert build.count(apply) == 1
assert build.index('patch-fex-ios-alias-xlate-bounds.py" "$R/FEX"') < build.index(apply) < build.index('\nbuild\n', build.index(apply))
restore = [line for line in build.splitlines() if line.startswith('git -C FEX checkout --')]
assert len(restore) == 1 and all(f' {rel}' in restore[0] for rel in overlay.EDITS), 'every patched file must be restored'
workflow = (root / '.github/workflows/build-ipa.yml').read_text()
assert "b'ec-ret-direct-v1 madeira-bcd' not in module" in workflow
catalog = (root / 'build/tools/gen-config-catalog.py').read_text()
assert '"env.MADEIRA_FEX_EC_RET_DIRECT"' in catalog and '"tools/patch-fex-ios-ec-ret-direct.py"' in catalog
assert 'key: "env.MADEIRA_FEX_EC_RET_DIRECT"' in (root / 'app/Madeira/ConfigCatalog.generated.swift').read_text()

print(f'EC return direct: overlay idempotent after {len(applied)} build overlays, drift refused; RetToEntryThunk text '
      'checked' + (f'; {emulated} return addresses run through the old and new stub: switched off identical, switched on '
                   'the same EnterEC state 8 instructions sooner than unpatched' if emulated else '') +
      '; MADEIRA_FEX_EC_RET_DIRECT=0 keeps the test')
