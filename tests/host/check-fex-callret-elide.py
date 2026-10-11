#!/usr/bin/env python3
"""Check the iOS ARM64EC call-ret shadow stack elision overlay.

Needs the FEX submodule and a host C++ compiler; no Wine, FEX build or game.
1. Applied after the other ARM64EC overlays in build order, the overlay is
   idempotent and refuses a moved anchor or a partial overlay.
2. Partial evaluation of the patched emitters (BranchOps.cpp ExitFunction,
   Dispatcher::EmitDispatcher, Spill/FillStaticRegs, ExitFunctionLink) for each
   build configuration: with CallRetStackUsed() true (the kill switch, every
   non-iOS build) they equal the unpatched sources token for token; the iOS
   WOW64 build (FEX_CALLRET_STACK_UNUSED) is unchanged too; with the stack
   elided no call-ret stack access is left on the iOS ARM64EC paths.
3. FEX's own emitter, built on the host, encodes both linked-CALL layouts: the
   patched ExitFunctionLink marker test matches the elided layout (the call is
   relinked as `bl`) and not the shadow-stack layout (relinked as `b`, as
   before).
4. MADEIRA_FEX_CALLRET_ELIDE: unset or anything but "0" elides, "0" keeps the
   stack; the switch is set before InitCore emits the dispatcher.
5. The real ExitFunction body, Spill/FillStaticRegs' x17 access, the
   dispatcher's EnterEC load and EC-exit store, the JIT callback's bound check
   and sentinel and ExitFunctionLink's marker constants, before and after the
   overlay, compiled into a host fixture with FEX's emitter and emitted for 14
   shapes: the kill switch emits the unpatched bytes; elided, a linked CALL is
   the marker and its `bl` where ExitFunctionLink looks, a RET is the L1 lookup
   alone, an indirect CALL and an EC exit lose only their stack instructions,
   and no shape touches x17. With Python's unicorn module (not on the macOS
   CI runner, where this part is skipped with a note) both RETs run from one
   state, L1 hit and miss, and end at the same target with the same state.
"""
from pathlib import Path
import importlib.util
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile

try:
    from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM
    from unicorn.arm64_const import UC_ARM64_REG_X0, UC_ARM64_REG_PC, UC_ARM64_REG_SP, UC_ARM64_REG_NZCV
except ImportError:
    Uc = None

root = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ev = load('fexpatch_eval', root / 'tests/host/fexpatch_eval.py')
overlay = load('callret_elide', root / 'tools/patch-fex-ios-callret-elide.py')

BRANCH = 'FEXCore/Source/Interface/Core/JIT/BranchOps.cpp'
DISPATCH = 'FEXCore/Source/Interface/Core/Dispatcher/Dispatcher.cpp'
EMITTER = 'FEXCore/Source/Interface/Core/ArchHelpers/Arm64Emitter.cpp'
EMITTER_H = 'FEXCore/Source/Interface/Core/ArchHelpers/Arm64Emitter.h'
JIT = 'FEXCore/Source/Interface/Core/JIT/JIT.cpp'
MODULE = 'Source/Windows/ARM64EC/Module.cpp'
assert set(overlay.EDITS) == {BRANCH, DISPATCH, EMITTER, EMITTER_H, JIT, MODULE}

# 1. The overlay on the output of the overlays before it.
CORE_STATE = 'FEXCore/include/FEXCore/Core/CoreState.h'
with tempfile.TemporaryDirectory() as tmp:
    fex, applied = ev.chain_tree(root, tmp, stop_before='patch-fex-ios-callret-elide.py')
    before = {rel: (fex / rel).read_text() for rel in overlay.EDITS}
    core_state = (fex / CORE_STATE).read_text()
assert 'patch-fex-ios-smc-store-fastpath.py' in applied and 'patch-fex-ios-branch-history.py' in applied
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
FUNCS = {
    BRANCH: ['DEF_OP(ExitFunction) {'],
    DISPATCH: ['void Dispatcher::EmitDispatcher() {'],
    EMITTER: ['void Arm64Emitter::SpillStaticRegs(', 'void Arm64Emitter::FillStaticRegs('],
    JIT: ['uint64_t Arm64JITCore::ExitFunctionLink('],
}


def fold(text, used):
    """Evaluate the overlay's CallRetStackUsed() forms with a fixed value."""
    while True:
        m = re.search(r'\bif\s*\(\s*(!?)\s*CallRetStackUsed\s*\(\s*\)\s*\)\s*\{', text)
        if m:
            close = ev.match_brace(text, m.end() - 1)
            inner = text[m.end():close]
            rest = text[close + 1:]
            keep = (m.group(1) == '') == used
            r = re.match(r'\s*else\b', rest)
            if r:
                rest = rest[r.end():]
                if keep:
                    # drop the else branch: an `if (...) {..}` chain or a block
                    e = re.match(r'\s*if\s*\(', rest)
                    if e:
                        depth, i = 0, e.end() - 1
                        while True:
                            if rest[i] == '(':
                                depth += 1
                            elif rest[i] == ')':
                                depth -= 1
                                if depth == 0:
                                    break
                            i += 1
                        b = rest.index('{', i)
                        end = ev.match_brace(rest, b)
                        tail = rest[end + 1:]
                        while re.match(r'\s*else\b', tail):
                            t = re.match(r'\s*else\b', tail)
                            tail2 = tail[t.end():]
                            b = tail2.index('{')
                            tail = tail2[ev.match_brace(tail2, b) + 1:]
                        rest = tail
                    else:
                        b = rest.index('{')
                        rest = rest[ev.match_brace(rest, b) + 1:]
                    text = text[:m.start()] + '{' + inner + '}' + rest
                else:
                    text = text[:m.start()] + rest
            else:
                text = text[:m.start()] + ('{' + inner + '}' if keep else '') + rest
            continue
        m = re.search(r'\bif\s*\(([^{};]*?)\s*&&\s*CallRetStackUsed\s*\(\s*\)\s*\)\s*\{', text)
        if m:
            close = ev.match_brace(text, m.end() - 1)
            if used:
                text = text[:m.start()] + 'if (' + m.group(1) + ') {' + text[m.end():]
            else:
                text = text[:m.start()] + text[close + 1:]
            continue
        m = re.search(r'CallRetStackUsed\s*\(\s*\)\s*\?\s*([^:;]+?)\s*:\s*([^;]+?)\s*;', text)
        if m:
            text = text[:m.start()] + (m.group(1) if used else m.group(2)) + ';' + text[m.end():]
            continue
        assert 'CallRetStackUsed' not in text, 'unfolded CallRetStackUsed form'
        return text


def evaluate(text, macros, used=None):
    text = ev.preprocess(ev.strip_comments(text), macros)
    return fold(text, used) if used is not None else text


def norm(text):
    return [('const' if t == 'constexpr' else t) for t in ev.tokens(text) if t not in '{}']


IOS_EC = {'FEX_IOS_HOST': 1, 'ARCHITECTURE_arm64ec': 1, '_WIN32': 1}
CONFIGS = {
    'iOS ARM64EC, stack kept': (IOS_EC, True),
    'iOS WOW64 (FEX_CALLRET_STACK_UNUSED)': ({'FEX_IOS_HOST': 1, 'FEX_CALLRET_STACK_UNUSED': 1, '_WIN32': 1}, False),
    'ARM64EC': ({'ARCHITECTURE_arm64ec': 1, '_WIN32': 1}, True),
    'Linux arm64': ({}, True),
}
compared = 0
for rel, headers in FUNCS.items():
    for header in headers:
        for name, (macros, used) in CONFIGS.items():
            a = norm(ev.body(evaluate(before[rel], macros), header))
            b = norm(ev.body(evaluate(patched[rel], macros, used), header))
            assert a == b, f'{rel} {header} differs under {name}'
            compared += 1
assert compared == 5 * len(CONFIGS)

# Elided on iOS ARM64EC: no call-ret stack access is emitted.
elided = {rel: evaluate(patched[rel], IOS_EC, False) for rel in FUNCS}
for rel, headers in FUNCS.items():
    for header in headers:
        code = ev.body(elided[rel], header)
        assert 'REG_CALLRET_SP' not in code and 'callret_sp' not in code, f'{rel} {header}: call-ret access left'
kept = ev.body(evaluate(patched[BRANCH], IOS_EC, True), FUNCS[BRANCH][0])
assert kept.count('REG_CALLRET_SP') >= 10, 'the kill switch must keep every shadow-stack access'
# The linked CALL keeps its marker and nothing is emitted between it and the branch.
call = ev.body(elided[BRANCH], FUNCS[BRANCH][0])
pro = call[call.index('if (Op->Hint == IR::BranchHint::Call) {'):call.index('} else if (Op->Hint == IR::BranchHint::CheckTF)')]
emits = re.findall(r'\b(adr|stp|str|ldr|add|sub|lsr|cbz|bl|b|blr)\s*[<(]', pro)
assert emits == ['adr'], f'linked CALL prologue emits {emits}'
assert 'EmitLinkedBranch(NewRIP, Op->Hint == IR::BranchHint::Call);' in call

# The header: constexpr for every build but iOS ARM64EC.
hdr = patched[EMITTER_H]
assert hdr.index('#define FEX_CALLRET_STACK_UNUSED 1') < hdr.index('inline bool CallRetStackUsed()')
for macros, expect in ((IOS_EC, 'IosCallRetStackElided == 0'), ({'FEX_IOS_HOST': 1, 'FEX_CALLRET_STACK_UNUSED': 1}, 'return false;'),
                       ({}, 'return true;')):
    text = ev.preprocess(hdr, macros)
    tail = text[text.index('bool CallRetStackUsed()'):]
    assert expect in tail[:tail.index('}')], macros
assert 'extern "C" uint32_t IosCallRetStackElided = 0;' in patched[EMITTER]

# 3. ExitFunctionLink's marker test against FEX's own encodings.
jit = patched[JIT]
start = jit.index('  const uint32_t KnownCallMarkerImm')
marker_setup = jit[start:jit.index('\n', jit.index('ExpectedKnownCallMarkerEmit.adr(TMP1, KnownCallMarkerImm);')) + 1]
marker_read = re.search(r'\n(  uintptr_t KnownCallMarkerAddr = CallerAddress - KnownCallMarkerDisp;\n'
                        r'  uint32_t KnownCallMarkerInst = \*reinterpret_cast<uint32_t\*>\(KnownCallMarkerAddr\);\n)', jit).group(1)
assert 'if (KnownCallMarkerInst == ExpectedKnownCallMarkerInst) {\n      BranchEmit.bl(BranchOffset);' in jit
harness = r'''
#include <CodeEmitter/Emitter.h>
#include <cstdio>
#include <cstdlib>
namespace FEXCore::Allocator {
void* aligned_alloc(size_t a, size_t s) { return ::aligned_alloc(a, (s + a - 1) / a * a); }
void aligned_free(void* p) { ::free(p); }
}
namespace fextl { void ReportAllocationFailure(size_t, size_t) { abort(); } }
static uint32_t IosCallRetStackElided;
static bool CallRetStackUsed() { return IosCallRetStackElided == 0; }
constexpr auto TMP1 = ARMEmitter::XReg::x10;
static bool Linked(uintptr_t CallerAddress) {
''' + marker_setup + marker_read + r'''
  return KnownCallMarkerInst == ExpectedKnownCallMarkerInst;
}
int main() {
  for (int Elided = 0; Elided < 2; ++Elided) {
    IosCallRetStackElided = Elided;
    for (int Layout = 0; Layout < 2; ++Layout) {
      alignas(16) uint32_t Buf[16] {};
      ARMEmitter::Emitter E(reinterpret_cast<uint8_t*>(Buf), sizeof(Buf));
      ARMEmitter::ForwardLabel Ret;
      (void)E.adr(TMP1, &Ret);
      if (Layout == 0) {  // shadow stack: stp RetReg, TMP1, [x17, #-0x10]!; str x17, [x28, #0xb0]
        E.stp<ARMEmitter::IndexType::PRE>(ARMEmitter::XReg::x6, TMP1, ARMEmitter::Reg::r17, -0x10);
        E.str(ARMEmitter::XReg::x17, ARMEmitter::Reg::r28, 0xb0);
      }
      const auto Caller = E.GetCursorAddress<uintptr_t>();
      (void)E.bl(&Ret);
      (void)E.Bind(&Ret);
      printf("%d %d %d\n", Elided, Layout, Linked(Caller) ? 1 : 0);
    }
  }
}
'''
with tempfile.TemporaryDirectory() as tmp:
    src = Path(tmp) / 'marker.cpp'
    exe = Path(tmp) / 'marker'
    src.write_text(harness)
    inc = [f'-I{root / "FEX" / d}' for d in ('CodeEmitter', 'FEXCore/include', 'FEXHeaderUtils', 'External/fmt/include')]
    subprocess.run([os.environ.get('CXX', 'c++'), '-std=c++20', '-O1', *inc, str(src), '-o', str(exe)], check=True)
    rows = {tuple(map(int, l.split()[:2])): int(l.split()[2]) for l in subprocess.run([str(exe)], capture_output=True, text=True, check=True).stdout.split('\n') if l}
assert rows == {(0, 0): 0, (0, 1): 0, (1, 0): 0, (1, 1): 1}, rows

# 4. The frontend switch.
module = patched[MODULE]
block = module[module.index('    const char* CallRetOpt = getenv("MADEIRA_FEX_CALLRET_ELIDE");'):]
block = block[:block.index('    LogMan::Msg::IFmt("FEX: callret-elide-v1 madeira-bcd')]
init = module.index('NTSTATUS ProcessInit() {')
assert init < module.index('FEX::Windows::Logging::Init();', init) < module.index(block) < module.index('  CTX->InitCore();', init)
assert module.count('CTX->InitCore();') == 1
switch = r'''
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
static uint32_t IosCallRetStackElided = 7;
int main() {
''' + block + r'''
  printf("%u\n", IosCallRetStackElided);
}
'''
with tempfile.TemporaryDirectory() as tmp:
    src = Path(tmp) / 'switch.cpp'
    exe = Path(tmp) / 'switch'
    src.write_text(switch)
    subprocess.run([os.environ.get('CXX', 'c++'), '-std=c++20', '-O1', '-Wall', '-Werror', str(src), '-o', str(exe)], check=True)
    for value, expect in ((None, '1'), ('0', '0'), ('1', '1'), ('', '1'), ('00', '1')):
        env = {k: v for k, v in os.environ.items() if k != 'MADEIRA_FEX_CALLRET_ELIDE'}
        if value is not None:
            env['MADEIRA_FEX_CALLRET_ELIDE'] = value
        got = subprocess.run([str(exe)], env=env, capture_output=True, text=True, check=True).stdout.strip()
        assert got == expect, (value, got)

# 5. Byte level: the real emitter code, before and after, for every exit shape.
def between(text, start, end):
    assert text.count(start) == 1, start
    i = text.index(start) + len(start)
    return text[i:text.index(end, i)]


def regions(src):
    branch, emitter, disp, jit = src[BRANCH], src[EMITTER], src[DISPATCH], src[JIT]
    exit_start = branch.index('DEF_OP(ExitFunction) {\n') + len('DEF_OP(ExitFunction) {\n')
    spill = emitter[emitter.index('void Arm64Emitter::SpillStaticRegs('):]
    fill = emitter[emitter.index('void Arm64Emitter::FillStaticRegs('):]
    return {
        '@BODY@': branch[exit_start:branch.index('\n}\n\nDEF_OP(Jump)', exit_start)],
        '@SPILL@': between(spill, '  Options.GPRSpillMask &= ~PFAFSpillMask;\n', '\n  for (size_t i = 0; i < StaticRegisters.size()'),
        '@FILL@': between(fill[:fill.index('#ifdef FEX_GUEST_WINDOW') + 30], '  ldr(STATE, TmpReg, CPU_AREA_EMULATOR_DATA_OFFSET);\n#endif\n',
                          '#ifdef FEX_GUEST_WINDOW'),
        '@ENTER@': between(disp, '  add(ARMEmitter::Size::i64Bit, ARMEmitter::Reg::rsp, TMP1, 0);\n', '\n  FillSpecialRegs(TMP1, TMP2, false, true);\n'),
        '@LOOPEC@': between(disp, '  (void)tbz(TMP1, 0, &l_NotECCode);\n',
                            '\n  add(ARMEmitter::Size::i64Bit, ARMEmitter::Reg::rsp, StaticRegisters[X86State::REG_RSP], 0);\n'
                            '  mov(EC_CALL_CHECKER_PC_REG, RipReg);\n'),
        '@CALLBACK@': between(disp, '    // load static regs\n    FillStaticRegs();\n', '\n    // Now go back to the regular dispatcher loop\n'),
        '@MARKER@': between(jit, '  auto BranchOffset = HostCode / 4 - CallerAddress / 4;\n', '  uint32_t ExpectedKnownCallMarkerInst = 0;'),
    }


fixture = r'''
#include <FEXCore/Core/CoreState.h>
#include <FEXCore/Core/X86Enums.h>
#include <FEXCore/Utils/MathUtils.h>
#include <CodeEmitter/Emitter.h>
#include <array>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <map>
#include <vector>

namespace FEXCore::Allocator {
void* aligned_alloc(size_t a, size_t s) { return std::aligned_alloc(a, (s + a - 1) / a * a); }
void aligned_free(void* ptr) { std::free(ptr); }
}
namespace fextl { void ReportAllocationFailure(std::size_t, std::size_t) { std::abort(); } }
namespace FEXCore::Core { uint32_t IosGuestBranchTraceEnabled = 0; }
namespace LogMan::Msg { void MFmtImpl(DebugLevels, const char*, const fmt::format_args&) {} }
namespace FEXCore::Context { constexpr bool BLOCK_DEBUGGING = false; }
namespace FEXCore { struct LookupCache { struct LookupCacheEntry { uintptr_t HostCode; uintptr_t GuestCode; }; }; }
namespace FEXCore::IR {
enum class BranchHint : uint8_t { None = 0, Call, Return, CheckTF };
struct Operand { unsigned Kind; uint64_t Value; unsigned Reg; };  // 0 constant, 1 entry offset, 2 register
struct BlockRef { uint32_t Id; bool IsInvalid() const { return Id == 0; } uint32_t ID() const { return Id; } };
struct IROp_ExitFunction { Operand NewRIP; BranchHint Hint; Operand CallReturnAddress; BlockRef CallReturnBlock; };
struct Header { IROp_ExitFunction Op; template<typename T> const T* C() const { return &Op; } };
}
static bool FixtureEcTarget = false;
static bool RtlIsEcCode(uint64_t) { return FixtureEcTarget; }
extern "C" uint32_t IosCallRetStackElided = 0;

namespace FEXCore::CPU {
constexpr auto STATE = ARMEmitter::XReg::x28;
constexpr auto TMP1 = ARMEmitter::XReg::x10;
constexpr auto TMP2 = ARMEmitter::XReg::x11;
constexpr auto TMP3 = ARMEmitter::XReg::x12;
constexpr auto REG_CALLRET_SP = ARMEmitter::XReg::x17;
constexpr auto EC_CALL_CHECKER_PC_REG = ARMEmitter::XReg::x9;
constexpr uint64_t EC_CODE_BITMAP_MAX_ADDRESS = 1ULL << 47;
inline bool CallRetStackUsed() { return IosCallRetStackElided == 0; }
#define STATE_PTR(STATE_TYPE, FIELD) STATE.R(), offsetof(FEXCore::Core::STATE_TYPE, FIELD)

struct Fixture : ARMEmitter::Emitter {
  struct { struct { bool IsInstCountCI = false; } HostFeatures; } CtxData;
  decltype(&CtxData) CTX = &CtxData;
  ARMEmitter::BiDirectionalLabel* PendingCallReturnTargetLabel {};
  std::map<uint32_t, ARMEmitter::BiDirectionalLabel> CallReturnTargets;
  ARMEmitter::ForwardLabel Thunk;
  struct Debug { struct Mapping { uint64_t GuestEntryOffset; }; std::vector<Mapping> GuestOpcodes; } DebugStore;
  Debug* DebugData = &DebugStore;
  uint64_t Entry = 0x140000000ULL;
  std::array<ARMEmitter::Register, 16> StaticRegisters {
    ARMEmitter::Reg::r8, ARMEmitter::Reg::r0, ARMEmitter::Reg::r1, ARMEmitter::Reg::r27, ARMEmitter::Reg::r23,
    ARMEmitter::Reg::r29, ARMEmitter::Reg::r25, ARMEmitter::Reg::r26, ARMEmitter::Reg::r2, ARMEmitter::Reg::r3,
    ARMEmitter::Reg::r4, ARMEmitter::Reg::r5, ARMEmitter::Reg::r19, ARMEmitter::Reg::r20, ARMEmitter::Reg::r21,
    ARMEmitter::Reg::r22};
  Fixture(uint8_t* Buffer, size_t Size) : Emitter(Buffer, Size) {}
  void ResetStack() {}
  bool IsInlineConstant(const IR::Operand& Op, uint64_t* Value) const {
    if (Op.Kind != 0) return false;
    if (Value) *Value = Op.Value;
    return true;
  }
  bool IsInlineEntrypointOffset(const IR::Operand& Op, uint64_t* Value) const {
    if (Op.Kind != 1) return false;
    *Value = Entry + Op.Value;
    return true;
  }
  ARMEmitter::Register GetReg(const IR::Operand& Op) const { return ARMEmitter::Register(Op.Reg); }
  void InsertGuestRIPMove(ARMEmitter::Register Reg, uint64_t Value) {
    movz(ARMEmitter::Size::i64Bit, Reg, Value & 0xffff);
    for (unsigned Shift = 16; Shift < 64; Shift += 16) {
      if ((Value >> Shift) & 0xffff) movk(ARMEmitter::Size::i64Bit, Reg, (Value >> Shift) & 0xffff, Shift);
    }
  }
  void EmitLinkedBranch(uint64_t, bool Call) {
    if (Call) (void)bl(&Thunk); else (void)b(&Thunk);
  }
  template<typename T> void BindOrRestart(T* Label) { (void)Bind(Label); }
  void Exit(const IR::Header* IROp) {
@BODY@
  }
  void Spill() {
@SPILL@
  }
  void Fill() {
@FILL@
  }
  void EnterEC() {
@ENTER@
  }
  void LoopEC() {
    ARMEmitter::ForwardLabel l_NotECCode;
    (void)tbz(TMP1, 0, &l_NotECCode);
@LOOPEC@
    (void)Bind(&l_NotECCode);
  }
  void Callback() {
@CALLBACK@
  }
};

static void Marker(uint32_t& OutImm, uintptr_t& OutDisp) {
@MARKER@
  OutImm = KnownCallMarkerImm;
  OutDisp = KnownCallMarkerDisp;
}
} // namespace FEXCore::CPU

int main(int argc, char** argv) {
  using namespace FEXCore;
  if (argc < 4) return 1;
  const unsigned Shape = std::strtoul(argv[1], nullptr, 0);
  IosCallRetStackElided = std::strtoul(argv[2], nullptr, 0);
  static uint8_t Code[8192];
  CPU::Fixture F(Code, sizeof(Code));
  IR::Header Hdr {};
  auto& Op = Hdr.Op;
  Op.CallReturnAddress = {2, 0, 7};
  Op.CallReturnBlock = {5};
  switch (Shape) {
  case 0: Op.NewRIP = {0, 0x140001230, 0}; Op.Hint = IR::BranchHint::Call; break;              // linked CALL
  case 1: Op.NewRIP = {0, 0x140001230, 0}; Op.Hint = IR::BranchHint::Call; Op.CallReturnBlock = {0}; break;
  case 2: Op.NewRIP = {1, 0x40, 0}; Op.Hint = IR::BranchHint::None; break;                      // linked jump
  case 3: Op.NewRIP = {0, 0x7ffe12345670, 0}; Op.Hint = IR::BranchHint::Call; FixtureEcTarget = true; break;
  case 4: Op.NewRIP = {2, 0, 6}; Op.Hint = IR::BranchHint::Call; break;                         // indirect CALL
  case 5: Op.NewRIP = {2, 0, 6}; Op.Hint = IR::BranchHint::Call; Op.CallReturnBlock = {0}; break;
  case 6: Op.NewRIP = {2, 0, 6}; Op.Hint = IR::BranchHint::Return; break;                       // RET
  case 7: Op.NewRIP = {2, 0, 6}; Op.Hint = IR::BranchHint::None; break;                         // indirect jump
  case 8: Op.NewRIP = {0, 0x140001230, 0}; Op.Hint = IR::BranchHint::CheckTF; break;
  case 9: F.Spill(); break;
  case 10: F.Fill(); break;
  case 11: F.EnterEC(); break;
  case 12: F.LoopEC(); break;
  case 13: F.Callback(); break;
  default: return 2;
  }
  if (Shape < 9) {
    F.Exit(&Hdr);
    (void)F.Bind(&F.Thunk);
    F.brk(0x7);
  }
  FILE* Out = std::fopen(argv[3], "wb");
  std::fwrite(Code, 1, F.GetCursorOffset(), Out);
  std::fclose(Out);
  uint32_t Imm; uintptr_t Disp;
  CPU::Marker(Imm, Disp);
  uint32_t MarkerWord = 0;
  ARMEmitter::Emitter MarkerEmit(reinterpret_cast<uint8_t*>(&MarkerWord), 4);
  MarkerEmit.adr(CPU::TMP1, Imm);
  using Frame = FEXCore::Core::CpuStateFrame;
  std::printf("{\"marker\":%u,\"disp\":%u,\"l1\":%zu,\"rip\":%zu,\"looptop\":%zu,\"crsp\":%zu,\"crbase\":%zu}\n",
              MarkerWord, (unsigned)Disp, offsetof(Frame, State.L1Pointer), offsetof(Frame, State.rip),
              offsetof(Frame, Pointers.DispatcherLoopTop), offsetof(Frame, State.callret_sp),
              offsetof(Frame, State.callret_sp_base));
}
'''

with tempfile.TemporaryDirectory(prefix='fex-callret-elide-') as tmp:
    tmp = Path(tmp)
    include = tmp / 'include/FEXCore/Core'
    include.mkdir(parents=True)
    (include / 'CoreState.h').write_text(core_state)

    def compile_fixture(src, out):
        unit = fixture
        for key, text in regions(src).items():
            unit = unit.replace(key, text)
        (tmp / (out + '.cpp')).write_text(unit)
        subprocess.run([os.environ.get('CXX', 'c++'), '-std=c++20', '-O1', '-w', '-DARCHITECTURE_arm64ec', '-DFEX_IOS_HOST=1',
                        '-I', str(tmp / 'include'), '-I', str(root / 'FEX/FEXCore/include'), '-I', str(root / 'FEX/CodeEmitter'),
                        '-I', str(root / 'FEX/FEXHeaderUtils'), '-I', str(root / 'FEX/External/fmt/include'),
                        str(tmp / (out + '.cpp')), '-o', str(tmp / out)], check=True)
        return tmp / out

    old_bin, new_bin = compile_fixture(before, 'old'), compile_fixture(patched, 'new')

    def emit(binary, shape, elided):
        out = tmp / f'{binary.name}-{shape}-{elided}.bin'
        layout = json.loads(subprocess.check_output([str(binary), str(shape), str(elided), str(out)], text=True))
        data = out.read_bytes()
        return [struct.unpack_from('<I', data, i)[0] for i in range(0, len(data), 4)], layout

    LDR_X17_SP, LDR_X10_BASE, STR_X17_SP = 0xf9405b91, 0xf9405f8a, 0xf9005b91
    BL_MASK, BL, BRK7 = 0xfc000000, 0x94000000, 0xd4200000 | (0x7 << 5)

    def touches_x17(words):
        for w in words:
            if (w & 0x3b000000) == 0x39000000 or (w & 0x3a000000) == 0x28000000:   # ldr/str imm, ldp/stp
                if (w >> 5) & 31 == 17 or w & 31 == 17 or (w & 0x3a000000) == 0x28000000 and (w >> 10) & 31 == 17:
                    return True
            if (w & 0x1f000000) in (0x11000000, 0x0b000000) and (w & 31 == 17 or (w >> 5) & 31 == 17):
                return True
        return False

    SHAPES = range(14)
    old = {s: emit(old_bin, s, 0)[0] for s in SHAPES}
    for s in SHAPES:
        kept, layout = emit(new_bin, s, 0)
        assert kept == old[s], f'MADEIRA_FEX_CALLRET_ELIDE=0 changed the code of shape {s}'
        assert layout['marker'] == 0x1000006a and layout['disp'] == 8, 'the kill switch changed the call marker'
    elided = {s: emit(new_bin, s, 1)[0] for s in SHAPES}
    layout = emit(new_bin, 0, 1)[1]
    assert layout['marker'] == 0x1000004a and layout['disp'] == 4, layout   # adr x10, #8, one instruction back
    # Linked CALL: the marker, then the branch ExitFunctionLink patches, the marker where it looks.
    call = elided[0]
    assert len(call) == 3 and call[0] == layout['marker'] and (call[1] & BL_MASK) == BL and call[2] == BRK7, call
    assert call[1 - layout['disp'] // 4] == layout['marker']
    # The shadow-stack layout (adr; stp; str x17; bl) never matched its own marker: linked as `b`.
    assert (old[0][-2] & BL_MASK) == BL and old[0][-2 - 8 // 4] != 0x1000006a
    assert len(elided[1]) == 2 and (elided[1][0] & BL_MASK) == BL and elided[1][1] == BRK7, elided[1]
    for s in (2, 7, 8):  # jumps and the TF check never touched the stack
        assert elided[s] == old[s], f'shape {s} changed'
    assert old[3][0] == STR_X17_SP and elided[3] == old[3][1:], 'the EC exit should lose only its x17 store'
    for s in (4, 5):  # indirect CALL: without the reload, guard, push and writeback
        i, j = old[s].index(LDR_X17_SP), old[s].index(0xd63f0160)  # blr x11
        assert STR_X17_SP in old[s][i:j] and elided[s] == old[s][:i] + old[s][j:], f'indirect call shape {s}'
    i = old[6].index(LDR_X17_SP)  # RET: the L1 lookup alone, `ret x11` included
    j = old[6].index(0xa8c12e2a) + 3  # ldp x10, x11, [x17], #16; str x17; sub x10, x10, x6
    assert i == 0 and old[6][j - 2] == STR_X17_SP and old[6][j - 1] == 0xcb06014a
    assert elided[6] == old[6][j:] and elided[6][-2] == 0xd65f0160, 'RET should be the L1 lookup alone'
    for s in SHAPES:
        assert not touches_x17(elided[s]), f'shape {s} still touches x17'
    assert old[9] == [STR_X17_SP] and elided[9] == [], (old[9], elided[9])
    assert old[10] == [LDR_X17_SP] and elided[10] == [], (old[10], elided[10])
    assert old[11] == [LDR_X17_SP] and elided[11] == [], (old[11], elided[11])
    TBZ_MASK, TBZ = 0x7f000000, 0x36000000
    assert len(old[12]) == 2 and (old[12][0] & TBZ_MASK) == TBZ and old[12][1] == STR_X17_SP, old[12]
    assert len(elided[12]) == 1 and (elided[12][0] & ~0x7ffe0) == (old[12][0] & ~0x7ffe0), elided[12]
    STP_ZR_X17 = 0xa9bf7e3f  # stp xzr, xzr, [x17, #-16]!
    assert len(old[13]) == 7 and old[13][0] == LDR_X10_BASE and old[13][-1] == STP_ZR_X17, old[13]
    assert elided[13] == [], elided[13]

    def run(words, layout, hit):
        uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
        code, state, l1, crstack, target = 0x100000, 0x200000, 0x300000, 0x400000, 0x1000000
        for addr, size in ((code, 0x10000), (state, 0x10000), (l1, 0x10000), (crstack, 0x800000), (target, 0x1000)):
            uc.mem_map(addr, size)
        uc.mem_write(code, b''.join(struct.pack('<I', w) for w in words))
        uc.mem_write(target, struct.pack('<I', 0xd4200000) * 16)
        rip, mask = 0x140004560, 0xff0
        uc.mem_write(state + layout['l1'], struct.pack('<QQ', l1, mask))
        uc.mem_write(state + layout['looptop'], struct.pack('<Q', target + 0x40))
        uc.mem_write(state + layout['crsp'], struct.pack('<Q', crstack + 0x400000))
        uc.mem_write(state + layout['crbase'], struct.pack('<Q', crstack))
        uc.mem_write(crstack + 0x400000, struct.pack('<QQ', rip, 0xdead0000))
        uc.mem_write(l1 + ((rip << 4) & mask), struct.pack('<QQ', target, rip if hit else rip + 1))
        for reg in range(31):
            uc.reg_write(UC_ARM64_REG_X0 + reg, 0x5000 + reg)
        uc.reg_write(UC_ARM64_REG_X0 + 28, state)
        uc.reg_write(UC_ARM64_REG_X0 + 6, rip)
        uc.reg_write(UC_ARM64_REG_SP, 0x7000)
        uc.reg_write(UC_ARM64_REG_NZCV, 0x60000000)
        uc.emu_start(code, target + (0 if hit else 0x40))
        regs = [uc.reg_read(UC_ARM64_REG_X0 + r) for r in range(31)]
        st = bytearray(uc.mem_read(state, 0x10000))
        st[layout['crsp']:layout['crsp'] + 8] = bytes(8)
        return uc.reg_read(UC_ARM64_REG_PC), regs, bytes(st), uc.reg_read(UC_ARM64_REG_NZCV)

    if Uc is None:
        print('note: Python unicorn unavailable; the RET emulation is skipped (the byte checks above ran)')
    for hit in ((True, False) if Uc is not None else ()):
        pc_old, regs_old, st_old, nzcv_old = run(old[6], layout, hit)
        pc_new, regs_new, st_new, nzcv_new = run(elided[6], layout, hit)
        assert pc_old == pc_new, (hex(pc_old), hex(pc_new))
        assert st_old == st_new and nzcv_old == nzcv_new == 0x60000000
        assert all(regs_old[r] == regs_new[r] for r in range(31) if r not in (10, 11, 17)), 'guest registers differ'
        assert regs_new[17] == 0x5000 + 17, 'the elided RET must leave x17 alone'

# Build wiring: after the other ARM64EC overlays, before the patched build; the tree is restored.
build = (root / 'tools/build-xtajit64.sh').read_text()
apply = 'python3 "$R/tools/patch-fex-ios-callret-elide.py" "$R/FEX"\n'
assert build.count(apply) == 1
assert build.index('patch-fex-ios-smc-store-fastpath.py') < build.index(apply) < build.index('\nbuild\n', build.index(apply))
restore = [line for line in build.splitlines() if line.startswith('git -C FEX checkout --')]
assert len(restore) == 1 and all(f' {rel}' in restore[0] for rel in overlay.EDITS), 'every patched file must be restored'
workflow = (root / '.github/workflows/build-ipa.yml').read_text()
assert "b'callret-elide-v1 madeira-bcd' not in module" in workflow
catalog = (root / 'build/tools/gen-config-catalog.py').read_text()
assert '"env.MADEIRA_FEX_CALLRET_ELIDE"' in catalog and '"tools/patch-fex-ios-callret-elide.py"' in catalog
assert 'key: "env.MADEIRA_FEX_CALLRET_ELIDE"' in (root / 'app/Madeira/ConfigCatalog.generated.swift').read_text()

print(f'call-ret elision: overlay idempotent after {len(applied)} build overlays, drift refused; {compared} emitter '
      'comparisons equal with the stack kept and on every other build; no shadow-stack access when elided; '
      'linked CALL relinked as bl only when elided; 14 emitted shapes: kill switch = old bytes, elided = marker + bl, '
      'RET = L1 lookup alone, no x17 traffic' + ('; old and elided RET reach the same target with the same state'
                                                  if Uc is not None else '') + '; MADEIRA_FEX_CALLRET_ELIDE=0 keeps the stack')
