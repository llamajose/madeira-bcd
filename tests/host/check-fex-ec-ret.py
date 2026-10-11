#!/usr/bin/env python3
"""Check the iOS ARM64EC overlay that returns from ARM64EC calls with RET.

Needs the FEX submodule and a host C++ compiler; no Wine, FEX build or game.
1. Applied after the other ARM64EC overlays in build order, the overlay is
   idempotent and refuses a moved anchor or a partial overlay.
2. Partial evaluation of BranchOps.cpp ExitFunction and
   Dispatcher::EmitDispatcher: with IosEcReturnWithRet 0 (the kill switch) and
   on every non-iOS build they equal the unpatched sources token for token.
   Switched on, only a guest CALL into ARM64EC code leaves with `blr`, and the
   EnterEC fast path is the loop top's trap-flag check and L1 probe with
   EC_CALL_CHECKER_PC_REG for RipReg, misses sent to the loop top and `ret`
   for `br`.
3. FEX's own emitter, built on the host, emits that fast path and the loop
   top's probe; a small AArch64 interpreter runs both over a set of L1 and
   trap-flag states: the fast path returns to exactly the block the loop top
   would branch to, takes the loop top otherwise, writes no memory, and
   leaves every register but TMP1/TMP2/TMP4 alone. Switched off it is a
   single `b` to the loop top.
4. ExitFunctionEC never reads the LR the `blr` sets: it loads LR itself on
   the entry-thunk paths.
5. MADEIRA_FEX_EC_RET: unset or anything but "0" enables, "0" disables; the
   switch is set before InitCore emits the dispatcher.
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
overlay = load('ec_ret', root / 'tools/patch-fex-ios-ec-ret.py')

BRANCH = 'FEXCore/Source/Interface/Core/JIT/BranchOps.cpp'
DISPATCH = 'FEXCore/Source/Interface/Core/Dispatcher/Dispatcher.cpp'
EMITTER = 'FEXCore/Source/Interface/Core/ArchHelpers/Arm64Emitter.cpp'
EMITTER_H = 'FEXCore/Source/Interface/Core/ArchHelpers/Arm64Emitter.h'
MODULE = 'Source/Windows/ARM64EC/Module.cpp'
assert set(overlay.EDITS) == {BRANCH, DISPATCH, EMITTER, EMITTER_H, MODULE}

# 1. The overlay on the output of the overlays before it.
with tempfile.TemporaryDirectory() as tmp:
    fex, applied = ev.chain_tree(root, tmp, stop_before='patch-fex-ios-ec-ret.py')
    before = {rel: (fex / rel).read_text() for rel in overlay.EDITS}
    module_s = (fex / 'Source/Windows/ARM64EC/Module.S').read_text()
assert 'patch-fex-ios-callret-elide.py' in applied and 'patch-fex-ios-smc-store-fastpath.py' in applied
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

# 2. Partial evaluation.
FUNCS = {BRANCH: 'DEF_OP(ExitFunction) {', DISPATCH: 'void Dispatcher::EmitDispatcher() {'}


def fold(text, on):
    """Evaluate the overlay's IosEcReturnWithRet conditions with a fixed value."""
    while True:
        m = re.search(r'\bif\s*\(\s*(?:([^{};]*?)\s*&&\s*)?IosEcReturnWithRet\s*\)\s*\{', text)
        if not m:
            assert 'IosEcReturnWithRet' not in text, 'unfolded IosEcReturnWithRet form'
            return text
        close = ev.match_brace(text, m.end() - 1)
        rest = text[close + 1:]
        r = re.match(r'\s*else\s*\{', rest)
        assert r, 'IosEcReturnWithRet without an else block'
        else_close = ev.match_brace(rest, r.end() - 1)
        then_part, else_part = text[m.end():close], rest[r.end():else_close]
        if on and m.group(1):
            new = 'if (' + m.group(1) + ') {' + then_part + '} else {' + else_part + '}'
        else:
            new = '{' + (then_part if on else else_part) + '}'
        text = text[:m.start()] + new + rest[else_close + 1:]


def evaluate(text, macros, on=None):
    text = ev.preprocess(ev.strip_comments(text), macros)
    return fold(text, on) if on is not None else text


def norm(text):
    return [t for t in ev.tokens(text) if t not in '{}']


IOS_EC = {'FEX_IOS_HOST': 1, 'ARCHITECTURE_arm64ec': 1, '_WIN32': 1}
CONFIGS = {
    'iOS ARM64EC, switched off': (IOS_EC, False),
    'iOS WOW64': ({'FEX_IOS_HOST': 1, '_WIN32': 1}, None),
    'ARM64EC': ({'ARCHITECTURE_arm64ec': 1, '_WIN32': 1}, None),
    'Linux arm64': ({}, None),
}
compared = 0
for rel, header in FUNCS.items():
    for name, (macros, on) in CONFIGS.items():
        a = norm(ev.body(evaluate(before[rel], macros), header))
        b = norm(ev.body(evaluate(patched[rel], macros, on), header))
        assert a == b, f'{rel} {header} differs under {name}'
        compared += 1
assert compared == 2 * len(CONFIGS)

# Switched on: only a guest CALL into ARM64EC code leaves the JIT with blr.
exit_on = re.sub(r'\s+', ' ', ev.body(evaluate(patched[BRANCH], IOS_EC, True), FUNCS[BRANCH]))
exit_off = re.sub(r'\s+', ' ', ev.body(evaluate(patched[BRANCH], IOS_EC, False), FUNCS[BRANCH]))
ec_exit = 'ldr(TMP2, STATE_PTR(CpuStateFrame, Pointers.ExitFunctionEC)); '
assert exit_on.count(ec_exit) == 1 and exit_off.count(ec_exit) == 1
assert exit_on.replace(ec_exit + 'if (Op->Hint == IR::BranchHint::Call) { blr(TMP2); } else { br(TMP2); }',
                       ec_exit + '{ br(TMP2); }') == exit_off, 'switched on, more than the CALL exit changed'
assert exit_on.count('blr(TMP2);') == exit_off.count('blr(TMP2);') + 1

# Switched on: the EnterEC fast path is the loop top's TF check and L1 probe.
disp = ev.body(evaluate(patched[DISPATCH], IOS_EC), FUNCS[DISPATCH])
enter = disp[disp.index('AbsoluteLoopTopAddressEnterEC = GetCursorAddress'):disp.index('Align16B();')]
m = re.search(r'\bif\s*\(\s*IosEcReturnWithRet\s*\)\s*\{', enter)
fast = enter[m.end():ev.match_brace(enter, m.end() - 1)]
assert enter.index('FillSpecialRegs(TMP1, TMP2, false, true);') < m.start(), 'fast path must follow the EnterEC setup'
loop = disp[disp.index('(void)Bind(&LoopTop);'):]
tf = re.search(r'ldrb\(TMP1, STATE_PTR\(CpuStateFrame, State\.flags\[X86State::RFLAG_TF_RAW_LOC\]\)\);\s*'
               r'\(void\)cbnz\(ARMEmitter::Size::i32Bit, TMP1, &CompileSingleStep\);', loop).group(0)
probe = loop[loop.index('ARMEmitter::ForwardLabel L1Miss;'):loop.index('(void)Bind(&L1Miss);')]
probe = probe[probe.index(';') + 1:]
expect = (tf.replace('&CompileSingleStep', '&LoopTop') +
          re.sub(r'\bRipReg\b', 'EC_CALL_CHECKER_PC_REG', probe).replace('&L1Miss', '&LoopTop').replace('br(TMP4)', 'ret(TMP4)'))
assert norm(fast) == norm(expect), 'EnterEC fast path is not the loop-top probe'
assert 'b(&LoopTop)' in re.sub(r'\s+', '', enter[ev.match_brace(enter, m.end() - 1):]).replace('(void)', '')

# 3. Emit both with FEX's emitter and run them.
harness = r'''
#include <CodeEmitter/Emitter.h>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
namespace FEXCore::Allocator {
void* aligned_alloc(size_t a, size_t s) { return ::aligned_alloc(a, (s + a - 1) / a * a); }
void aligned_free(void* p) { ::free(p); }
}
namespace fextl { void ReportAllocationFailure(size_t, size_t) { abort(); } }
namespace FEXCore {
template<typename T>
constexpr T ilog2(T Value) { return std::countr_zero(Value); }
}
namespace FEXCore::Core {
struct FakeState { uint64_t rip; uint64_t Pad[5]; uint64_t L1Pointer; uint64_t L1Mask; uint64_t Pad2[3]; uint8_t flags[48]; };
struct CpuStateFrame { uint64_t Pad[2]; FakeState State; };
}
namespace FEXCore::LookupCache { struct LookupCacheEntry { uintptr_t HostCode; uintptr_t GuestCode; }; }
namespace FEXCore::X86State { enum { RFLAG_TF_RAW_LOC = 8 }; }
namespace FEXCore::CPU {
constexpr auto STATE = ARMEmitter::XReg::x28;
constexpr auto TMP1 = ARMEmitter::XReg::x10;
constexpr auto TMP2 = ARMEmitter::XReg::x11;
constexpr auto TMP3 = ARMEmitter::XReg::x12;
constexpr auto TMP4 = ARMEmitter::XReg::x13;
constexpr auto EC_CALL_CHECKER_PC_REG = ARMEmitter::XReg::x9;
#define STATE_PTR(STATE_TYPE, FIELD) STATE.R(), offsetof(FEXCore::Core::STATE_TYPE, FIELD)
static uint32_t IosEcReturnWithRet;
struct Harness : ARMEmitter::Emitter {
  using ARMEmitter::Emitter::Emitter;
  size_t Off() { return GetCursorAddress<uint8_t*>() - GetBufferBase(); }
  void EnterEC(size_t& LoopTopAt) {
    ARMEmitter::BiDirectionalLabel LoopTop {};
    if (IosEcReturnWithRet) {
''' + fast + r'''
    } else {
      (void)b(&LoopTop);
    }
    (void)Bind(&LoopTop);
    LoopTopAt = Off();
    brk(1);
  }
  void Probe(size_t& SingleStepAt, size_t& MissAt) {
    auto RipReg = TMP3;
    ARMEmitter::ForwardLabel CompileSingleStep;
    ARMEmitter::ForwardLabel Miss;
''' + tf + r'''
    {
      ARMEmitter::ForwardLabel L1Miss;
''' + probe + r'''
      (void)Bind(&L1Miss);
    }
    (void)b(&Miss);
    (void)Bind(&CompileSingleStep);
    SingleStepAt = Off();
    brk(2);
    (void)Bind(&Miss);
    MissAt = Off();
    brk(3);
  }
};
}
using namespace FEXCore::CPU;
static void Dump(const char* Name, const uint8_t* Base, size_t Size, size_t A, size_t B) {
  printf("%s %zu %zu ", Name, A, B);
  for (size_t i = 0; i < Size; ++i) printf("%02x", Base[i]);
  printf("\n");
}
int main() {
  printf("layout %zu %zu %zu %zu\n", offsetof(FEXCore::Core::CpuStateFrame, State.L1Pointer),
         offsetof(FEXCore::Core::CpuStateFrame, State.L1Mask), offsetof(FEXCore::Core::CpuStateFrame, State.flags[8]),
         sizeof(FEXCore::Core::CpuStateFrame));
  for (int On = 0; On < 2; ++On) {
    alignas(16) static uint8_t Buf[512];
    Harness E(Buf, sizeof(Buf));
    IosEcReturnWithRet = On;
    size_t LoopTopAt = 0;
    E.EnterEC(LoopTopAt);
    Dump(On ? "enter-on" : "enter-off", Buf, E.Off(), LoopTopAt, 0);
  }
  alignas(16) static uint8_t Buf[512];
  Harness E(Buf, sizeof(Buf));
  size_t SingleStepAt = 0, MissAt = 0;
  E.Probe(SingleStepAt, MissAt);
  Dump("probe", Buf, E.Off(), SingleStepAt, MissAt);
}
'''
with tempfile.TemporaryDirectory() as tmp:
    src = Path(tmp) / 'ecret.cpp'
    exe = Path(tmp) / 'ecret'
    src.write_text(harness)
    inc = [f'-I{root / "FEX" / d}' for d in ('CodeEmitter', 'FEXCore/include', 'FEXHeaderUtils', 'External/fmt/include')]
    subprocess.run([os.environ.get('CXX', 'c++'), '-std=c++20', '-O1', *inc, str(src), '-o', str(exe)], check=True)
    out = subprocess.run([str(exe)], capture_output=True, text=True, check=True).stdout.split('\n')
layout = list(map(int, out[0].split()[1:]))
OFF_L1P, OFF_L1M, OFF_TF, FRAME = layout
code = {}
for line in out[1:]:
    if line:
        name, a, b, hexcode = line.split()
        code[name] = (bytes.fromhex(hexcode), int(a), int(b))

M64 = (1 << 64) - 1


def sext(value, bits):
    return value - (1 << bits) if value >> (bits - 1) else value


class Memory:
    def __init__(self, regions):
        self.regions = regions  # base -> bytearray

    def read(self, addr, size):
        for base, data in self.regions.items():
            if base <= addr and addr + size <= base + len(data):
                return int.from_bytes(data[addr - base:addr - base + size], 'little')
        raise AssertionError(f'wild read {addr:#x}')


def run(blob, stops, regs, mem):
    """Interpret the emitted forms; returns ('stop', offset) or ('ret'|'br', target, regs)."""
    pc = 0
    for _ in range(64):
        if pc in stops:
            return ('stop', pc, regs)
        insn = struct.unpack_from('<I', blob, pc)[0]
        rd, rn, rm = insn & 31, (insn >> 5) & 31, (insn >> 16) & 31
        get = lambda r: 0 if r == 31 else regs[r]
        if insn & 0xFFC00000 == 0x39400000:  # ldrb wt, [xn, #imm]
            assert rn != 31
            regs[rd] = mem.read(regs[rn] + ((insn >> 10) & 0xFFF), 1)
        elif insn & 0xFFC00000 == 0xA9400000:  # ldp xt, xt2, [xn, #imm]
            assert rn != 31
            addr = regs[rn] + sext((insn >> 15) & 0x7F, 7) * 8
            regs[rd], regs[(insn >> 10) & 31] = mem.read(addr, 8), mem.read(addr + 8, 8)
        elif insn & 0xFF200000 in (0x8A000000, 0x8B000000, 0xCB000000):  # and/add/sub xd, xn, xm, lsl #n
            assert (insn >> 22) & 3 == 0, 'only LSL'
            op2 = (get(rm) << ((insn >> 10) & 63)) & M64
            kind = insn & 0xFF000000
            value = get(rn) & op2 if kind == 0x8A000000 else get(rn) + op2 if kind == 0x8B000000 else get(rn) - op2
            if rd != 31:
                regs[rd] = value & M64
        elif insn & 0x7E000000 == 0x34000000:  # cbz/cbnz
            value = get(rd) if insn >> 31 else get(rd) & 0xFFFFFFFF
            if (value != 0) == bool((insn >> 24) & 1):
                pc += sext((insn >> 5) & 0x7FFFF, 19) * 4
                continue
        elif insn & 0xFC000000 == 0x14000000:  # b
            pc += sext(insn & 0x3FFFFFF, 26) * 4
            continue
        elif insn & 0xFFFFFC1F == 0xD65F0000:
            return ('ret', regs[rn], regs)
        elif insn & 0xFFFFFC1F == 0xD61F0000:
            return ('br', regs[rn], regs)
        else:
            raise AssertionError(f'unexpected instruction {insn:#010x} at {pc:#x}')
        pc += 4
    raise AssertionError('no exit')


STATE_BASE, L1_BASE, ENTRIES = 0x10000, 0x200000, 16
TABLE = {1: (0x140001001, 0x70001000), 3: (0x140001003, 0), 5: (0x7ffe00000005, 0x70005000)}
cases = 0
for rip in (0x140001001, 0x140001003, 0x140001005, 0x7ffe00000005, 0x140001002, 0x0, 0x11, 0x7ffe00000015):
    for tf in (0, 1, 2):
        frame = bytearray(FRAME)
        frame[OFF_L1P:OFF_L1P + 8] = L1_BASE.to_bytes(8, 'little')
        frame[OFF_L1M:OFF_L1M + 8] = ((ENTRIES - 1) << 4).to_bytes(8, 'little')
        frame[OFF_TF] = tf
        table = bytearray(16 * ENTRIES)
        for index, (guest, host) in TABLE.items():
            table[16 * index:16 * index + 16] = host.to_bytes(8, 'little') + guest.to_bytes(8, 'little')
        mem = Memory({STATE_BASE: frame, L1_BASE: table})
        start = {r: 0x1111111100000000 + r for r in range(31)}
        start[28], start[9] = STATE_BASE, rip  # STATE, EC_CALL_CHECKER_PC_REG; RipReg (x12) is not loaded at EnterEC
        blob, single, miss = code['probe']
        p = run(blob, {single, miss}, {**start, 12: rip}, mem)  # the loop top loads RipReg from State.rip
        loop_top = ('br', p[1]) if p[0] == 'br' else ('loop top',)
        entry = TABLE.get(rip & (ENTRIES - 1), (0, 0))
        model = ('br', entry[1]) if not tf and entry[0] == rip and entry[1] else ('loop top',)
        assert loop_top == model, (hex(rip), tf, p[:2])
        blob, stop, _ = code['enter-on']
        e = run(blob, {stop}, dict(start), mem)
        got = ('br', e[1]) if e[0] == 'ret' else ('loop top',)
        assert e[0] in ('ret', 'stop') and got == loop_top, (hex(rip), tf, e[:2], loop_top)
        changed = {r for r in range(31) if e[2][r] != start[r]}
        assert changed <= {10, 11, 13}, f'fast path clobbers x{sorted(changed)}'
        blob, stop, _ = code['enter-off']
        assert run(blob, {stop}, dict(start), mem)[:2] == ('stop', stop)
        cases += 1
assert len(code['enter-off'][0]) == 8 and code['enter-off'][1] == 4  # b LoopTop; brk
assert struct.unpack_from('<I', code['enter-on'][0], code['enter-on'][1] - 4)[0] == 0xD65F01A0  # ret x13

# 4. ExitFunctionEC loads LR itself before anything uses it.
body = module_s[module_s.index('\nExitFunctionEC:'):module_s.index('\nExitFunctionSuspendPoint:')]
lines = [l.split('//')[0].strip() for l in body.split('\n')]
uses = [l for l in lines if re.search(r'\b(lr|x30|w30)\b', l)]
assert uses == ['ldr lr, [x4], #0x8', 'adrp lr, X64ReturnInstr', 'ldr lr, [lr, #:lo12:X64ReturnInstr]'], uses
for macro in ('IOS_XP_COUNT', 'IOS_LOAD_TEB'):
    text = module_s[module_s.index(f'.macro {macro}'):]
    assert not re.search(r'\b(lr|x30|w30)\b', text[:text.index('.endm')]), macro

# 5. The frontend switch.
module = patched[MODULE]
block = module[module.index('    const char* EcRetOpt = getenv("MADEIRA_FEX_EC_RET");'):]
block = block[:block.index('    LogMan::Msg::IFmt("FEX: ec-ret-v1 madeira-bcd')]
init = module.index('NTSTATUS ProcessInit() {')
assert init < module.index('FEX::Windows::Logging::Init();', init) < module.index(block) < module.index('  CTX->InitCore();', init)
assert module.count('CTX->InitCore();') == 1
switch = r'''
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
static uint32_t IosEcReturnWithRet = 7;
int main() {
''' + block + r'''
  printf("%u\n", IosEcReturnWithRet);
}
'''
with tempfile.TemporaryDirectory() as tmp:
    src = Path(tmp) / 'switch.cpp'
    exe = Path(tmp) / 'switch'
    src.write_text(switch)
    subprocess.run([os.environ.get('CXX', 'c++'), '-std=c++20', '-O1', '-Wall', '-Werror', str(src), '-o', str(exe)], check=True)
    for value, expect in ((None, '1'), ('0', '0'), ('1', '1'), ('', '1'), ('00', '1')):
        env = {k: v for k, v in os.environ.items() if k != 'MADEIRA_FEX_EC_RET'}
        if value is not None:
            env['MADEIRA_FEX_EC_RET'] = value
        got = subprocess.run([str(exe)], env=env, capture_output=True, text=True, check=True).stdout.strip()
        assert got == expect, (value, got)
assert 'extern "C" uint32_t IosEcReturnWithRet = 0;' in patched[EMITTER]
hdr = ev.preprocess(patched[EMITTER_H], IOS_EC)
assert 'extern "C" uint32_t IosEcReturnWithRet;' in hdr
assert 'IosEcReturnWithRet' not in ev.preprocess(patched[EMITTER_H], {'FEX_IOS_HOST': 1})

# Build wiring: after the call-ret elision, before the patched build; the tree is restored.
build = (root / 'tools/build-xtajit64.sh').read_text()
apply = 'python3 "$R/tools/patch-fex-ios-ec-ret.py" "$R/FEX"\n'
assert build.count(apply) == 1
assert build.index('patch-fex-ios-callret-elide.py" "$R/FEX"') < build.index(apply) < build.index('\nbuild\n', build.index(apply))
restore = [line for line in build.splitlines() if line.startswith('git -C FEX checkout --')]
assert len(restore) == 1 and all(f' {rel}' in restore[0] for rel in overlay.EDITS), 'every patched file must be restored'
workflow = (root / '.github/workflows/build-ipa.yml').read_text()
assert "b'ec-ret-v1 madeira-bcd' not in module" in workflow
catalog = (root / 'build/tools/gen-config-catalog.py').read_text()
assert '"env.MADEIRA_FEX_EC_RET"' in catalog and '"tools/patch-fex-ios-ec-ret.py"' in catalog
assert 'key: "env.MADEIRA_FEX_EC_RET"' in (root / 'app/Madeira/ConfigCatalog.generated.swift').read_text()

print(f'EC return: overlay idempotent after {len(applied)} build overlays, drift refused; {compared} emitter comparisons '
      f'equal switched off and on every other build; EnterEC fast path = loop-top probe, {cases} L1/trap-flag states '
      'run through FEX-emitted code agree; ExitFunctionEC sets LR itself; MADEIRA_FEX_EC_RET=0 switches it off')
