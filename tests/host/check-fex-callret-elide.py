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
overlay = load('callret_elide', root / 'tools/patch-fex-ios-callret-elide.py')

BRANCH = 'FEXCore/Source/Interface/Core/JIT/BranchOps.cpp'
DISPATCH = 'FEXCore/Source/Interface/Core/Dispatcher/Dispatcher.cpp'
EMITTER = 'FEXCore/Source/Interface/Core/ArchHelpers/Arm64Emitter.cpp'
EMITTER_H = 'FEXCore/Source/Interface/Core/ArchHelpers/Arm64Emitter.h'
JIT = 'FEXCore/Source/Interface/Core/JIT/JIT.cpp'
MODULE = 'Source/Windows/ARM64EC/Module.cpp'
assert set(overlay.EDITS) == {BRANCH, DISPATCH, EMITTER, EMITTER_H, JIT, MODULE}

# 1. The overlay on the output of the overlays before it.
with tempfile.TemporaryDirectory() as tmp:
    fex, applied = ev.chain_tree(root, tmp, stop_before='patch-fex-ios-callret-elide.py')
    before = {rel: (fex / rel).read_text() for rel in overlay.EDITS}
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

# Build wiring: after the other ARM64EC overlays, before the patched build; the tree is restored.
build = (root / 'tools/build-xtajit64.sh').read_text()
apply = 'python3 "$R/tools/patch-fex-ios-callret-elide.py" "$R/FEX"\n'
assert build.count(apply) == 1
assert build.index('patch-fex-ios-smc-store-fastpath.py') < build.index(apply) < build.index('\nbuild\n', build.index(apply))
restore = [line for line in build.splitlines() if line.startswith('git -C FEX checkout --')]
assert len(restore) == 1 and all(f' {rel}' in restore[0] for rel in overlay.EDITS), 'every patched file must be restored'
workflow = (root / '.github/workflows/build-ipa.yml').read_text()
assert "b'callret-elide-v1 madeira-bcd' not in module" in workflow

print(f'call-ret elision: overlay idempotent after {len(applied)} build overlays, drift refused; {compared} emitter '
      'comparisons equal with the stack kept and on every other build; no shadow-stack access when elided; '
      'linked CALL relinked as bl only when elided; MADEIRA_FEX_CALLRET_ELIDE=0 keeps the stack')
