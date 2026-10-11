#!/usr/bin/env python3
"""Render passes fill only the colour attachments they use (winemetal,
madeira.cfg rp-lean, default 1); no Wine, no Metal.

tools/patch-winemetal-rp-lean.py changes _MTLCommandBuffer_renderCommandEncoder
in dxmt/src/winemetal/unix/winemetal_unix.c: a colour slot with neither a
texture nor a resolve texture is left as the fresh descriptor made it, and a
used slot is looked up once and filled with the same ten values as the full
loop. This test copies dxmt/src (or $DXMT_SRC_DIR/src) with the files the
patches need into a temporary tree, runs every winemetal/dxmt/airconv patch the
workflow runs before it, then this one twice, and checks:
  - it applies once and then only says "already patched";
  - the switch is read once with madeira_cfg_int("rp-lean", 1) and cached;
  - the lean branch skips exactly the slots without texture and resolve
    texture, and sets exactly the ten properties the full loop sets, from the
    same fields, through one lookup;
  - the full loop is still there for rp-lean = 0, and depth, stencil and the
    pass-wide fields are set as before, after the loop;
  - the remote path (wmtr) is untouched;
  - an anchor that moved fails loudly, the tree unchanged;
  - the workflow runs it after the GPU trace patch and before winemetal is
    built, and the catalog lists rp-lean, on by default.
Objective-C cannot be compiled here; the lean slot selection is also checked
as C against the full loop's result for every combination of used slots.
SKIP (exit 0) when no dxmt sources are available.
"""
import importlib.util, os, pathlib, re, shutil, subprocess, sys, tempfile

R = pathlib.Path(__file__).resolve().parents[2]
src = pathlib.Path(os.environ.get("DXMT_SRC_DIR", R / "dxmt")) / "src"
if not (src / "winemetal" / "unix" / "winemetal_unix.c").is_file():
    print(f"SKIP: no dxmt sources at {src} (check out the submodule or set DXMT_SRC_DIR)")
    sys.exit(0)
ok = True


def check(what, cond):
    global ok
    print(("ok   " if cond else "FAIL ") + what)
    ok &= bool(cond)


wf = (R / ".github/workflows/build-ipa.yml").read_text()
scripts = re.findall(r"python3 (tools/patch-(?:dxmt|airconv|winemetal)-[\w.-]+\.py)", wf)
me = "tools/patch-winemetal-rp-lean.py"
check("the workflow runs the patch", me in scripts)
check("after the GPU trace patch and before winemetal is built",
      me in scripts and "tools/patch-winemetal-gpu-trace.py" in scripts
      and scripts.index(me) > scripts.index("tools/patch-winemetal-gpu-trace.py")
      and wf.index(me) < wf.index("name: Build dxmt-ios"))
cat = (R / "app/Madeira/ConfigCatalog.generated.swift").read_text()
check("catalog lists rp-lean, on by default, from the patch",
      re.search(r'key: "rp-lean".*kind: \.bool, defaultValue: "1".*sources: \["tools/patch-winemetal-rp-lean.py"\]', cat) is not None)

FIELDS = ["clearColor", "level", "slice", "depthPlane", "texture", "loadAction", "storeAction", "resolveTexture",
          "resolveLevel", "resolveSlice", "resolveDepthPlane"]

with tempfile.TemporaryDirectory() as tmp:
    t = pathlib.Path(tmp)
    # CI runs this check after every dxmt/winemetal patch step: start from dxmt/src as committed
    arc = subprocess.run(["git", "-C", str(src.parent), "archive", "--format=tar", "HEAD", "src"], capture_output=True)
    if arc.returncode == 0 and arc.stdout:
        import io, tarfile
        (t / "dxmt").mkdir()
        with tarfile.open(fileobj=io.BytesIO(arc.stdout)) as tf:
            try:
                tf.extractall(t / "dxmt", filter="data")
            except TypeError:
                tf.extractall(t / "dxmt")
    else:
        shutil.copytree(src, t / "dxmt/src")
    (t / "build").mkdir()
    shutil.copy(R / "build/madeira_cfg.h", t / "build/madeira_cfg.h")
    shutil.copytree(R / "research/remote-metal", t / "research/remote-metal")
    wm = t / "dxmt/src/winemetal/unix/winemetal_unix.c"
    pristine = wm.read_text()
    for s in scripts[:scripts.index(me)] if me in scripts else scripts:
        p = subprocess.run([sys.executable, str(R / s)], cwd=t, capture_output=True, text=True)
        if p.returncode:
            print(f"FAIL earlier patch {s}:\n{(p.stdout + p.stderr).strip()}")
            ok = False
    before = wm.read_text()
    p1 = subprocess.run([sys.executable, str(R / me)], cwd=t, capture_output=True, text=True)
    p2 = subprocess.run([sys.executable, str(R / me)], cwd=t, capture_output=True, text=True)
    check("applies once, then says already patched",
          p1.returncode == 0 and "rp-lean patched" in p1.stdout and p2.returncode == 0 and "already patched" in p2.stdout)
    after = wm.read_text()
    fn = after[after.index("static NTSTATUS\n_MTLCommandBuffer_renderCommandEncoder(void *obj) {"):]
    fn = fn[:fn.index("\n}\n")]
    check("switch read once with madeira_cfg_int(\"rp-lean\", 1), cached",
          'if (on < 0) on = madeira_cfg_int("rp-lean", 1) ? 1 : 0;' in after and "static int on = -1;" in after
          and after.index("static int madeira_rp_lean_on(void)") < after.index("_MTLCommandBuffer_renderCommandEncoder(void *obj) {"))
    lean = fn[fn.index("if (madeira_lean) {"):fn.index("      continue;\n    }\n") + len("      continue;\n    }\n")]
    check("a slot with neither texture nor resolve texture is skipped",
          "if (!info->colors[i].texture && !info->colors[i].resolve_texture)\n        continue;" in lean)
    check("one lookup per used slot", lean.count("descriptor.colorAttachments[i]") == 1)
    full = fn[fn.index("    descriptor.colorAttachments[i].clearColor = MTLClearColorMake("):fn.index("  }\n\n  if (info->depth.texture)")]
    lean_sets = re.findall(r"madeira_att\.(\w+) = (.*?);\n", lean, re.S)
    full_sets = re.findall(r"descriptor\.colorAttachments\[i\]\.(\w+) = (.*?);\n", full, re.S)
    norm = lambda v: re.sub(r"\s+", "", v)
    check("the lean slot gets exactly the full loop's ten sets, from the same fields",
          [f for f, _ in lean_sets] == FIELDS and [f for f, _ in full_sets] == FIELDS
          and [norm(v) for _, v in lean_sets] == [norm(v) for _, v in full_sets])
    check("the full loop stays for rp-lean = 0", fn.index("if (madeira_lean) {") < fn.index(full) and "continue;" not in full)
    tail = fn[fn.index("  if (info->depth.texture) {"):]
    btail = before[before.index("static NTSTATUS\n_MTLCommandBuffer_renderCommandEncoder(void *obj) {"):]
    btail = btail[:btail.index("\n}\n")]
    check("depth, stencil, pass-wide fields and the remote path unchanged",
          tail == btail[btail.index("  if (info->depth.texture) {"):]
          and fn[:fn.index("  MTLRenderPassDescriptor *descriptor")] == btail[:btail.index("  MTLRenderPassDescriptor *descriptor")])
    spec = importlib.util.spec_from_file_location("rp_lean_patch", R / me)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    check("nothing else in the file changed (undoing the two insertions gives the file back)",
          after.count(mod.HELPER) == 1 and after.count(mod.LOOP_NEW) == 1
          and after.replace(mod.LOOP_NEW, mod.LOOP_OLD).replace(mod.HELPER, "", 1) == before)
    # a moved anchor fails loudly and leaves the file alone
    wm.write_text(before.replace("    descriptor.colorAttachments[i].level = info->colors[i].level;\n",
                                 "    descriptor.colorAttachments[i].level = (uint16_t)info->colors[i].level;\n"))
    moved = wm.read_text()
    p3 = subprocess.run([sys.executable, str(R / me)], cwd=t, capture_output=True, text=True)
    check("a moved anchor fails by name, file unchanged",
          p3.returncode != 0 and "rp-lean" in p3.stdout + p3.stderr and wm.read_text() == moved)
    _ = pristine

# The slot choice as C: for every combination of slots with a texture and/or a
# resolve texture, the lean descriptor's used slots equal the full loop's.
harness = r'''
#include <stdio.h>
#include <string.h>
#include <stdint.h>
struct slot { uint64_t texture, resolve; int set; };
int main(void) {
    unsigned m, i, bad = 0;
    for (m = 0; m < (1u << 16); m++) {
        struct slot full[8], lean[8], info[8];
        memset(full, 0, sizeof full); memset(lean, 0, sizeof lean);
        for (i = 0; i < 8; i++) { info[i].texture = (m >> i) & 1 ? 0x100 + i : 0; info[i].resolve = (m >> (8 + i)) & 1 ? 0x200 + i : 0; }
        for (i = 0; i < 8; i++) { full[i].texture = info[i].texture; full[i].resolve = info[i].resolve; full[i].set = 1; }
        for (i = 0; i < 8; i++) {
            if (!info[i].texture && !info[i].resolve) continue;
            lean[i].texture = info[i].texture; lean[i].resolve = info[i].resolve; lean[i].set = 1;
        }
        /* Metal uses a slot when it has a texture (a resolve texture alone is kept as the full loop keeps it) */
        for (i = 0; i < 8; i++)
            if (full[i].texture != lean[i].texture || full[i].resolve != lean[i].resolve) bad++;
    }
    printf(bad ? "FAIL\n" : "ok\n");
    return bad != 0;
}
'''
cc = shutil.which("clang") or shutil.which("gcc") or shutil.which("cc")
if cc:
    with tempfile.TemporaryDirectory() as t:
        t = pathlib.Path(t)
        (t / "h.c").write_text(harness)
        r = subprocess.run([cc, "-O1", "-o", str(t / "h"), str(t / "h.c")], capture_output=True, text=True)
        out = subprocess.run([str(t / "h")], capture_output=True, text=True).stdout if r.returncode == 0 else r.stderr
        check("every combination of used slots gives the full loop's attachments", out.strip() == "ok")
print("PASS" if ok else "FAILED")
sys.exit(0 if ok else 1)
