#!/usr/bin/env python3
"""Render-pass memory traffic in madeira-d3d12; no Wine, no Metal.

On Apple's tile-based GPU every Load and every Store of a render-pass
attachment is a whole pass over that surface in memory (Horizon Zero Dawn,
build 493: ~0.9 GB loaded and ~0.95 GB stored a frame at 1408x648). This test
cuts the replay's pass setup out of madeira_d3d12.c -- the real struct
mad_cmd and struct mad_exec, exec_end, exec_flush_clear, exec_add_clear,
exec_same_targets, exec_begin_render and the madeira-bcd render-pass block
(gpu-traffic, clear-defer, rp-discard, rp-carry) -- compiles it on the host
against the real winemetal.h with a recording Metal, replays synthetic
command lists through a loop that carries the real loop's hooks, and checks:
  - gpu-traffic: bytes by load and store action from pass setup (texel sizes
    of the common formats, depth and stencil apart);
  - clear-defer (default on; 0 = the old order exactly): a clear of a texture
    the open pass does not have leaves the pass open and becomes the Clear
    load action of the pass that binds it, with no clear-only pass; a clear of
    one of the pass's own textures still ends it; a barrier naming the
    texture (or one whose resources were not all recorded, but not one that
    only adds resource-less UAV barriers), a copy naming it, a pass binding
    or a clear of the texture through another view, and the list end apply
    the clear first; with clear-defer = 0 the old order comes back,
    including the old gap (a dispatch after a barrier ran before the clear);
  - rp-discard (default off): a whole DiscardResource makes the next pass's
    Load of that texture DontCare only with the switch on, counted either way;
    a barrier or an open pass in between, or a partial discard, keeps the Load;
    a pass with a texture the game discards says Unknown store actions only
    when winemetal answers op 24, and exec_end sets DontCare exactly when the
    next use (this list, then the call's later lists) is a whole discard or a
    whole clear of the aspect, Store otherwise, after the thread's queued
    encode-batch commands went out and before endEncoding; the traffic moves
    to "not stored"; the decision follows a carried pass into the next list;
  - tools/patch-winemetal-rp-store.py: applies once to winemetal_unix.c,
    op 24 sits in _madeira_ctl (not forwarded to i386), and its code, compiled
    with a recording encoder, answers a probe and sets only the actions given;
  - rp-carry (default off): a pass open at a list end goes on into the next
    list of the same call when that list's first draw has the same targets
    and views and only state (viewport and scissor set again) comes between:
    one encoder, one fence wait and one update instead of one per list;
    barriers before or after, other targets, a missing scissor, a blend
    factor left set, diagnostics, an unsynced mode-6 pass, the 48-list batch
    bound and the call's end keep the boundary; pending clears go along; the
    census counts what each boundary costs and the next call's continuation
    (before and after a commit);
and in the source: where the hooks sit in mad_exec_list and mad_ecl_run,
DiscardResource is recorded, the perf-log lines, the catalog entries and
the CI step. Needs clang (or gcc with C23 enums).
"""
from pathlib import Path
import os, re, shutil, subprocess, sys, tempfile

root = Path(__file__).resolve().parents[2]
src = (root / "madeira-d3d12/src/pe/madeira_d3d12.c").read_text()
ok = True


def check(what, cond):
    global ok
    print(("ok   " if cond else "FAIL ") + what)
    ok &= bool(cond)


def between(a, b, start=0):
    i = src.index(a, start)
    return src[i:src.index(b, i)]


def func(sig):
    i = src.index(sig)
    return src[i:src.index("\n}\n", i) + 3]


# ---- the source -------------------------------------------------------------
block = between("/* ---- madeira-bcd: render-pass traffic", "/* A clear whose target is not part of the next pass")
check("render-pass block found", "static void mad_tr_pass(" in block and "static void exec_rp_list_end(" in block
      and "static void mad_rp_close(" in block)
check("clear-defer on by default (a kill switch), rp-carry and rp-discard off by default, each read once",
      'g_clear_defer = mad_cfg_int_pe("clear-defer", 1) ? 1 : 0;' in block
      and 'g_rp_carry = mad_cfg_int_pe("rp-carry", 0) ? 1 : 0;' in block
      and 'g_rp_discard = mad_cfg_int_pe("rp-discard", 0) ? 1 : 0;' in block
      and block.count("if (g_clear_defer < 0)") == 1 and block.count("if (g_rp_carry < 0)") == 1
      and block.count("if (g_rp_discard < 0)") == 1)
enum = between("enum mad_ck {", "};") + "};"
check("MC_DISCARD is the last command kind (the others keep their values)", enum.rstrip("};\n ").endswith(
      "MC_DISCARD,     /* madeira-bcd rp-discard: a DiscardResource (u.discard); the replay only notes it */"))
disc = func("static void STDMETHODCALLTYPE list_DiscardResource(")
check("DiscardResource still counted, recorded for textures only, whole or not",
      "InterlockedIncrement(&g_discards);" in disc and "if (!r || !r->texture) return;" in disc
      and "mad_list_push((struct mad_list *)This, MC_DISCARD);" in disc
      and "c->u.discard.whole = mad_discard_whole(r, region);" in disc)
loop = between("static void mad_exec_list(struct mad_queue *q, struct mad_list *l, obj_handle_t cb) {", "\n}\n")
check("replay loop: the pass handed over is taken before the first command",
      loop.index("exec_rp_list_begin(&e);") < loop.index("for (i = 0; i < l->ncmds; i++) {"))
check("replay loop: pending clears / discards noted before each command is replayed",
      "        if ((e.npend | e.rp_ndisc) || c->kind == MC_DISCARD) exec_rp_note(&e, c);   /* madeira-bcd clear-defer, rp-discard */\n"
      "        switch (c->kind) {" in loop and "case MC_DISCARD: break;" in loop)
check("replay loop: the list end may hand the open pass over before ending it",
      "    exec_rp_list_end(&e);   /* madeira-bcd rp-carry: may hand the open pass to the next list */\n"
      "    if (e.renc) InterlockedIncrement(&g_pass_end_list);\n    exec_end(&e);\n    while (e.npend) exec_flush_clear(&e, 0);" in loop)
run = func("static void mad_ecl_run(")
check("mad_ecl_run: one carry slot per call, the next list per list, closed before a 48-list commit and at the end, "
      "under the submit lock",
      "memset(&rp_k, 0, sizeof rp_k); q->rp_carry = &rp_k; q->rp_next = NULL;" in run
      and "if (rp_k.renc && l != rp_want) mad_rp_close(q);" in run
      and "q->rp_next = mad_rp_next(q, count, lists, i); rp_want = q->rp_next;" in run
      and run.index("q->rp_next = mad_rp_next(q, count, lists, i);") < run.index("if (l) mad_exec_list(q, l, q->open_cb);")
      and run.index("mad_rp_close(q);      /* madeira-bcd rp-carry: nothing is carried into a full batch") < run.index("mad_queue_flush(q);   /* bound the batch */")
      and run.index("mad_rp_close(q); q->rp_carry = NULL; q->rp_next = NULL;") < run.index("LeaveCriticalSection(&ml1021_q->submit_lock);"))
begin = func("static int exec_begin_render(struct mad_exec *e) {")
check("exec_begin_render: pending clears outside the pass flushed only with clear-defer = 0 (one of a texture the pass binds "
      "through another view always); traffic counted per pass",
      "if (bound || (!here && mad_clear_defer_on())) { k++; continue; }" in begin
      and "if (e->renc) mad_tr_pass(&rpi, e->rt, e->rtp, e->nrt, e->depth, &e->dp);" in begin)
addc = func("static void exec_add_clear(")
check("exec_add_clear: ends the open pass only for its own textures (clear-defer) or always (clear-defer = 0)",
      "if (e->renc && (!mad_clear_defer_on() || exec_enc_has(e, r))) { InterlockedIncrement(&g_pass_end_clear); exec_end(e); }" in addc)
perf = func("static void mad_perf_present(void) {")
check("perf-log prints the gpu-traffic lines; the ml1137 line keeps the prefix abline.py reads",
      "if (g_perf_presents) mad_tr_report((double)g_perf_presents);" in perf
      and '"[perf] ml1137 attachments per census frame: load %.1f MB, clear %.1f MB, store %.1f MB, of which next in the list:' in perf
      and "discarded %.1f MB; DiscardResource calls" in perf)
check("gpu-traffic lines", '"[perf] gpu-traffic per frame: load %.1f MB, clear %.1f MB, not loaded %.1f MB, store %.1f MB, not stored %.1f MB; "' in block
      and '"[perf] gpu-traffic list ends per frame with a pass the next list goes on with (store + reload): carried %.1f (%.0f MB), "' in block)
for name, sig in (("exec_flush_clear", "static void exec_flush_clear(struct mad_exec *e, int i) {"),
                  ("exec_clear_rects", "static void exec_clear_rects("), ("exec_resolve", "static void exec_resolve(")):
    check(f"{name} counts its pass", "mad_tr_pass(&rpi," in func(sig))
census = func("static void f7_store_census(")
check("the ml1137 census sees a whole discard after a store", "case MC_DISCARD:" in census and "&g_ac_st_discarded" in census)
check("late stores: only with rp-discard, no capture, a discarded texture attached and op 24 answering",
      "if (mad_rp_discard_on() && !g_capture_on && exec_rp_late_want(e) && mad_rp_late_ok()) {" in begin
      and "if (e->rp_late) exec_rp_late_store(e);   /* madeira-bcd rp-discard: the store actions the pass left open */\n"
          "        MTLCommandEncoder_endEncoding(e->renc);" in func("static void exec_end(struct mad_exec *e) {")
      and "if (c->u.discard.whole && mad_rp_discard_on()) mad_rp_dset_add(r);" in disc
      and "q->rp_lists = lists; q->rp_nlists = count;" in run and "rp_want = q->rp_next; q->rp_idx = i;" in run)
rar = func("static int f6_rar_only(const struct mad_cmd *c) {")
check("f6-rar: off by default, read once; mode 6 skips the sync only for read-to-read batches, after the census",
      'g_f6_rar = mad_cfg_int_pe("f6-rar", 0) ? 1 : 0;' in rar
      and "                f7_census_barrier(&e, c);   /* ml1137: counts only */\n"
          "                if (f6_rar_only(c)) { InterlockedIncrement(&g_f6_rar_passed); break; }   /* madeira-bcd f6-rar */" in loop)
need = func("static void f7_need(struct mad_exec *e, UINT8 cls, const struct mad_resource *r) {")
check("ml1137 state-aware census: a UAV barrier needs pending blits too (ClearUnorderedAccessView is a blit fill)",
      "cls == BC_RAW_UAV) e->f7_mask |= 1ull << i;" in need and "(cls == BC_RAW_UAV && (e->cenc || e->renc || e->benc))" in need)
patch = root / "tools/patch-winemetal-rp-store.py"
wm_src = root / "dxmt/src/winemetal/unix/winemetal_unix.c"
check("the winemetal patch is applied by CI", "- name: Patch winemetal late render-pass store actions\n        run: python3 tools/patch-winemetal-rp-store.py" in
      (root / ".github/workflows/build-ipa.yml").read_text())
patched = None
if wm_src.exists():
    with tempfile.TemporaryDirectory(prefix="madeira-rp-store-") as d:
        t = Path(d) / "dxmt/src/winemetal/unix/winemetal_unix.c"
        t.parent.mkdir(parents=True)
        shutil.copy(wm_src, t)
        r1 = subprocess.run([sys.executable, str(patch)], cwd=d, capture_output=True, text=True)
        r2 = subprocess.run([sys.executable, str(patch)], cwd=d, capture_output=True, text=True)
        check("the patch applies once and is idempotent", r1.returncode == 0 and r2.returncode == 0 and "already applied" in r2.stdout)
        patched = t.read_text()
    ctl = patched[patched.index("static NTSTATUS _madeira_ctl(void *args) {"):]
    ctl = ctl[:ctl.index("\n}\n") + 3]
    cases = re.findall(r"^\s*case (\d+):", ctl, re.M)
    wow = patched[patched.index("static NTSTATUS _madeira_ctl_wow64(void *args) {"):]
    wow = wow[:wow.index("\n}\n") + 3]
    check("op 24 in _madeira_ctl, once, local mode only; the i386 entry does not forward it",
          cases.count("24") == 1 and len(cases) == len(set(cases)) and "wmtr_enabled()" in ctl[ctl.index("case 24:"):]
          and "if (!a->ptr || a->len < sizeof(struct madeira_rp_store_req) || wmtr_enabled()) break;" in ctl
          and "op == 24" not in wow)
    helper = patched[patched.index("/* madeira-bcd: late render-pass store actions"):patched.index("static NTSTATUS _madeira_ctl(void *args) {")]
    c_code = helper.replace("id<MTLRenderCommandEncoder> enc;", "uintptr_t enc;")
    c_code = c_code.replace("enc = (id<MTLRenderCommandEncoder>)(uintptr_t)r->enc;", "enc = (uintptr_t)r->enc;")
    c_code = c_code.replace("[enc setColorStoreAction:(MTLStoreAction)r->color[i] atIndex:i]", "fake_color(enc, r->color[i], i)")
    c_code = c_code.replace("[enc setDepthStoreAction:(MTLStoreAction)r->depth]", "fake_depth(enc, r->depth)")
    c_code = c_code.replace("[enc setStencilStoreAction:(MTLStoreAction)r->stencil]", "fake_stencil(enc, r->stencil)")
    check("the patch's code has no Objective-C left after translation", "[enc" not in c_code and "id<" not in c_code)
    wm_harness = r'''
#include <stdio.h>
#include <stdint.h>
#include <string.h>
static int calls[16][3], ncalls;
static void fake_color(uintptr_t e, int a, unsigned i) { calls[ncalls][0] = 'c' + (int)e; calls[ncalls][1] = a; calls[ncalls++][2] = (int)i; }
static void fake_depth(uintptr_t e, int a) { calls[ncalls][0] = 'd' + (int)e; calls[ncalls][1] = a; calls[ncalls++][2] = -1; }
static void fake_stencil(uintptr_t e, int a) { calls[ncalls][0] = 's' + (int)e; calls[ncalls][1] = a; calls[ncalls++][2] = -1; }
''' + c_code + r'''
int main(void) {
    struct madeira_rp_store_req r;
    int bad = 0;
    memset(&r, 0, sizeof r);
    if (madeira_rp_store(&r) != 1 || ncalls) { printf("probe\n"); bad = 1; }
    memset(&r, 0xff, sizeof r); r.enc = 0x50; r.color[0] = 1; r.color[2] = 0; r.depth = 0;
    if (madeira_rp_store(&r) != 1 || ncalls != 3 || calls[0][0] != 'c' + 0x50 || calls[0][1] != 1 || calls[0][2] != 0 ||
        calls[1][0] != 'c' + 0x50 || calls[1][1] != 0 || calls[1][2] != 2 || calls[2][0] != 'd' + 0x50 || calls[2][1] != 0) { printf("set\n"); bad = 1; }
    if (sizeof r != 24) { printf("size\n"); bad = 1; }
    if (!bad) printf("ok\n");
    return bad;
}
'''
    cc0 = shutil.which("clang") or shutil.which("gcc")
    if cc0:
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "w.c").write_text(wm_harness)
            r = subprocess.run([cc0, "-Wall", "-o", str(Path(d) / "w"), str(Path(d) / "w.c")], capture_output=True, text=True)
            out = subprocess.run([str(Path(d) / "w")], capture_output=True, text=True).stdout if r.returncode == 0 else r.stderr
            check("op 24 run: a probe sets nothing and answers 1; only the actions given are set; the request is 24 bytes",
                  out.strip() == "ok")
            if out.strip() != "ok":
                print(out[:2000])
else:
    print("note: no dxmt sources; the patch's own checks are skipped")
cat = (root / "app/Madeira/ConfigCatalog.generated.swift").read_text()
for key, dflt in (("clear-defer", "1"), ("rp-carry", "0"), ("rp-discard", "0"), ("f6-rar", "0")):
    check(f"catalog lists {key}, default {dflt}",
          re.search(r'key: "%s".*kind: \.bool, defaultValue: "%s"' % (re.escape(key), dflt), cat) is not None)
wf = (root / ".github/workflows/build-ipa.yml").read_text()
check("CI runs this check", "- name: Check render-pass traffic\n        run: python3 tests/host/check-render-pass-traffic.py" in wf)

# ---- the code, run ------------------------------------------------------------
cmd_struct = between("struct mad_cmd {", "\n};\n") + "\n};\n"
exec_struct = between("struct mad_exec {", "\n};\n") + "\n};\n"
rp_last = re.search(r"    struct \{ struct mad_resource \*rt\[8\], \*depth;[^\n]*\} rp_last;\n", src).group(0)
rp_lists = re.search(r"    ID3D12CommandList \*const \*rp_lists; UINT rp_nlists, rp_idx;[^\n]*\n", src).group(0)
bc_enum = re.search(r"enum \{ BC_RAR, [^\n]*\};\n", src).group(0)
eb_enum = re.search(r"enum \{ EBF_END, [^\n]*\};", src).group(0)
rtvp = re.search(r"struct mad_rtvp \{[^\n]*\};\n", src).group(0)
tr_globals = between("/* madeira-bcd gpu-traffic: what every render pass moves", "static int g_census_on;")
helpers = between("static int mad_rtvp_eq(const struct mad_rtvp *a, const struct mad_rtvp *b) {",
                  "\n/* ---- madeira-bcd: render-pass traffic")
flush_add = between("static void exec_flush_clear(struct mad_exec *e, int i) {", "\n/* The rasterisation area of a pass")

harness = r'''
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stdarg.h>
#include "winemetal.h"
typedef uint64_t UINT64; typedef unsigned UINT; typedef uint32_t UINT32; typedef uint16_t UINT16; typedef uint8_t UINT8;
typedef int INT; typedef long LONG; typedef long long LONG64; typedef int BOOL; typedef uintptr_t ULONG_PTR;
typedef struct { float TopLeftX, TopLeftY, Width, Height, MinDepth, MaxDepth; } D3D12_VIEWPORT;
typedef struct { LONG left, top, right, bottom; } D3D12_RECT;
typedef int D3D12_PRIMITIVE_TOPOLOGY;
typedef struct { UINT NumRects; const D3D12_RECT *pRects; UINT FirstSubresource, NumSubresources; } D3D12_DISCARD_REGION;
enum { D3D12_RESOURCE_DIMENSION_TEXTURE2D = 3, D3D12_RESOURCE_DIMENSION_TEXTURE3D = 4 };
typedef struct { int Dimension; UINT64 Width; UINT Height; UINT16 DepthOrArraySize, MipLevels; } D3D12_RESOURCE_DESC;
#define D3D12_CLEAR_FLAG_DEPTH 1
#define D3D12_CLEAR_FLAG_STENCIL 2
#define MAD_ROOT_PARAM_MAX 32
typedef struct ID3D12CommandList ID3D12CommandList;
struct mad_pso { const char *vs_name, *ps_name; };
struct mad_rootsig; struct mad_heap; struct mad_queryheap; struct mad_skinx;
''' + rtvp + r'''
struct mad_resource { obj_handle_t texture, buffer; UINT32 width, height; int is_depth, has_stencil; UINT samples;
                      enum WMTTextureType tex_type; enum WMTPixelFormat tex_pf; UINT tex_mips; D3D12_RESOURCE_DESC desc; };
struct mad_vis_batch { UINT64 next; int dirty; unsigned active; };
struct mad_device { LONG has_query_heaps; };
struct mad_alloc { LONG generation; };
''' + enum + "\n" + bc_enum + cmd_struct + r'''
struct mad_list { struct mad_cmd *cmds; unsigned ncmds; int closed; struct mad_alloc *alloc; LONG recorded_generation; };
struct mad_rp_carry;
struct mad_queue { struct mad_device *device; struct mad_vis_batch *vis; unsigned open_lists; UINT64 batches; obj_handle_t open_cb;
                   struct mad_rp_carry *rp_carry; const struct mad_list *rp_next;
''' + rp_lists + rp_last + r'''};
''' + exec_struct + r'''
/* ---- stand-ins ---- */
static LONG InterlockedIncrement(volatile LONG *p) { return __atomic_add_fetch(p, 1, __ATOMIC_SEQ_CST); }
typedef struct { void *p; } SRWLOCK;
#define SRWLOCK_INIT { 0 }
static int srw_held, srw_taken;
static void AcquireSRWLockExclusive(SRWLOCK *l) { (void)l; srw_held++; srw_taken++; }
static void ReleaseSRWLockExclusive(SRWLOCK *l) { (void)l; srw_held--; }
static void *InterlockedCompareExchangePointer(void *volatile *p, void *x, void *cmp) {
    void *e = cmp; __atomic_compare_exchange_n(p, &e, x, 0, __ATOMIC_SEQ_CST, __ATOMIC_SEQ_CST); return e; }
/* encode-batch: what this thread queued goes out (recorded: op 24 must come after it) */
''' + eb_enum + r'''
static int eb_flushed;
static void mad_eb_flush_cur(int why) { if (why == EBF_OTHER) eb_flushed = 1; }
/* MadeiraCtl op 24 as tools/patch-winemetal-rp-store.py answers it (op24_ok = 0: a winemetal without it) */
static int op24_ok, op24_probes, n24, f24[64]; static struct { UINT64 enc; UINT8 color[8], depth, stencil, pad[6]; } r24[64];
void MadeiraCtl(struct madeira_ctl_args *a) {
    const UINT8 *p = (const UINT8 *)(uintptr_t)a->ptr;
    a->ret = 0;
    if (a->op != 24 || !op24_ok || !p || a->len < 24) return;
    if (!*(const UINT64 *)p) op24_probes++; else if (n24 < 64) { f24[n24] = eb_flushed; memcpy(&r24[n24++], p, 24); }
    eb_flushed = 0;
    a->ret = 1;
}
static LONG InterlockedExchange(volatile LONG *p, LONG v) { return __atomic_exchange_n(p, v, __ATOMIC_SEQ_CST); }
static LONG64 InterlockedExchange64(volatile LONG64 *p, LONG64 v) { return __atomic_exchange_n(p, v, __ATOMIC_SEQ_CST); }
static LONG64 InterlockedExchangeAdd64(volatile LONG64 *p, LONG64 v) { return __atomic_fetch_add(p, v, __ATOMIC_SEQ_CST); }
static int cfg_defer = -1, cfg_carry = -1, cfg_discard = -1, cfg_rar = -1, nlogs;
static long long mad_cfg_int_pe(const char *k, long long d) {
    if (!strcmp(k, "f6-rar") && cfg_rar >= 0) return cfg_rar;
    if (!strcmp(k, "clear-defer") && cfg_defer >= 0) return cfg_defer;
    if (!strcmp(k, "rp-carry") && cfg_carry >= 0) return cfg_carry;
    if (!strcmp(k, "rp-discard") && cfg_discard >= 0) return cfg_discard;
    return d;
}
static void d3d12_log(const char *fmt, ...) { (void)fmt; nlogs++; }
static int g_capture_on, g_census_on, g_enc_labels;
static volatile LONG g_fault_diag, g_att_census, g_vis_restarts;
static unsigned g_enc_seq, g_said_attachless;
static volatile LONG g_pass_begun, g_pass_end_rts, g_pass_end_clear, g_pass_end_dispatch, g_pass_end_blit, g_pass_end_list,
                     g_pass_end_attachless, g_pass_clear_only, g_pass_reused;
static volatile LONG64 g_pass_attach_bytes;
static LONG64 g_ac_load, g_ac_clear;
''' + tr_globals + r'''
/* the recording Metal: every encoder as an event */
struct ev { char kind; int ended, waits, updates, late_before_end; obj_handle_t h; struct WMTRenderPassInfo rpi; };
static struct ev evs[256]; static int nev;
static struct ev *ev_of(obj_handle_t h) { int i; for (i = nev - 1; i >= 0; i--) if (evs[i].h == h) return &evs[i]; return NULL; }
obj_handle_t MTLCommandBuffer_renderCommandEncoder(obj_handle_t cb, struct WMTRenderPassInfo *rpi) {
    struct ev *v = &evs[nev++]; (void)cb; memset(v, 0, sizeof *v); v->kind = 'R'; v->rpi = *rpi; v->h = 0x1000 + nev; return v->h;
}
static obj_handle_t new_enc(char kind) { struct ev *v = &evs[nev++]; memset(v, 0, sizeof *v); v->kind = kind; v->h = 0x1000 + nev; return v->h; }
void MTLCommandEncoder_endEncoding(obj_handle_t h) {
    struct ev *v = ev_of(h); int i;
    if (!v) return;
    v->ended++;
    for (i = 0; i < n24; i++) if (r24[i].enc == h) v->late_before_end = 1;
}
static int g_mode = 1;
static void exec_fence_render(struct mad_exec *e, obj_handle_t enc, int update) {
    struct ev *v = ev_of(enc);
    if (!e->mode || !v) return;
    if (update) v->updates++; else v->waits++;
    if (e->mode == 6 && !update) { e->f6_enc_synced = e->f6_sync_needed; e->f6_sync_needed = 0; }
}
static void exec_fence_blit(struct mad_exec *e, obj_handle_t enc, int update) { (void)e; (void)enc; (void)update; }
static void exec_fence_compute(struct mad_exec *e, obj_handle_t enc, int update) { (void)e; (void)enc; (void)update; }
static void f7_store_census(struct mad_exec *e, const struct mad_resource *r, UINT level) { (void)e; (void)r; (void)level; }
static void mad_fault_label_render(struct mad_exec *e) { (void)e; }
static void mad_capture_pass(struct mad_exec *e, struct mad_resource **rt, unsigned nrt, const struct mad_rtvp *rtp,
                             struct mad_resource *depth, const struct mad_rtvp *dp, unsigned seq, unsigned draws) {
    (void)e; (void)rt; (void)nrt; (void)rtp; (void)depth; (void)dp; (void)seq; (void)draws; }
static void mad_ic_encode(struct mad_exec *e) { (void)e; }
static void exec_note_write(struct mad_exec *e, struct mad_resource *r) { (void)e; (void)r; }
static struct mad_vis_batch *mad_vis_get(struct mad_exec *e) { (void)e; return NULL; }
static obj_handle_t mad_vis_chunk(struct mad_exec *e, struct mad_vis_batch *v, UINT64 slot, void **cpu_out) { (void)e; (void)v; (void)slot; (void)cpu_out; return 0; }
static LONG64 f7_att_bytes(const struct mad_resource *r, UINT level) { (void)r; (void)level; return 0; }
static void mad_label(obj_handle_t enc, const char *fmt, ...) { (void)enc; (void)fmt; }
static void exec_rp_late_store(struct mad_exec *e);
''' + func("static void exec_end(struct mad_exec *e) {") + helpers + block + flush_add + \
    func("static void exec_attachless_area(") + func("static int exec_same_targets(struct mad_exec *e) {") + begin + \
    "static int g_f6_rar = -1;\n" + rar + r'''
/* ---- a replay with the real loop's hooks (mode 1: barriers end compute and blit encoders) ---- */
static void replay(struct mad_queue *q, struct mad_list *l) {
    struct mad_exec *ep = calloc(1, sizeof *ep), e; unsigned i;
    e = *ep; free(ep);
    e.fence_needed = 1; e.f6_sync_needed = 1; e.f6_list_start = 1; e.mode = g_mode; e.f7_reason = 2;
    e.q = q; e.l = l; e.cb = 0xcb; e.vis_prev = ~(UINT64)0; e.topo = 4;
    exec_rp_list_begin(&e);
    for (i = 0; i < l->ncmds; i++) {
        const struct mad_cmd *c = &l->cmds[i];
        e.cur = i;
        if ((e.npend | e.rp_ndisc) || c->kind == MC_DISCARD) exec_rp_note(&e, c);
        switch (c->kind) {
        case MC_VP: e.vp = c->u.vp; e.has_vp = 1; break;
        case MC_SCISSOR: e.sc = c->u.scissor; e.has_sc = 1; break;
        case MC_BLEND_FACTOR: memcpy(e.blend, c->u.blend.rgba, sizeof e.blend); e.has_blend = 1; break;
        case MC_RTS:
            if (e.renc && ((!c->u.rts.n && !c->u.rts.depth) || (!e.enc_nrt && !e.enc_depth))) exec_end(&e);
            memcpy(e.rt, c->u.rts.rt, sizeof e.rt); e.nrt = c->u.rts.n; e.depth = c->u.rts.depth;
            memcpy(e.rtp, c->u.rts.v, sizeof e.rtp); e.dp = c->u.rts.dv;
            break;
        case MC_CLEAR_RT: exec_add_clear(&e, c->u.clear.res, &c->u.clear.v, c->u.clear.rgba, 0.0f, 0, 0, 0); break;
        case MC_CLEAR_DS: exec_add_clear(&e, c->u.clear.res, &c->u.clear.v, NULL, c->u.clear.depth, 1, c->u.clear.stencil, c->u.clear.flags); break;
        case MC_BARRIER:
            if (e.mode == 6) { e.f6_sync_needed = 1; if (e.cenc || e.benc) exec_end(&e); if (e.renc && !e.f6_enc_synced) exec_end(&e); break; }
            if (e.cenc || e.benc) exec_end(&e);
            break;
        case MC_DRAW: if (exec_begin_render(&e)) { e.draws++; e.pass_draws++; } break;
        case MC_COPY_T2T: case MC_COPY_B2T: case MC_COPY_T2B: case MC_COPY_BB: case MC_FILL_BB: case MC_FILL_TEX:
            if (!e.benc) { exec_end(&e); e.benc = new_enc('B'); } break;
        case MC_DISPATCH: if (!e.cenc) { exec_end(&e); e.cenc = new_enc('C'); } break;
        default: break;
        }
    }
    exec_rp_list_end(&e);
    exec_end(&e);
    while (e.npend) exec_flush_clear(&e, 0);
}
static void flush(struct mad_queue *q) { q->batches++; q->open_lists = 0; }
static void ecl(struct mad_queue *q, unsigned count, struct mad_list **lists) {
    struct mad_rp_carry rp_k; const struct mad_list *rp_want = NULL; unsigned i;
    memset(&rp_k, 0, sizeof rp_k); q->rp_carry = &rp_k; q->rp_next = NULL;
    q->rp_lists = (ID3D12CommandList *const *)lists; q->rp_nlists = count;
    for (i = 0; i < count; i++) {
        struct mad_list *l = lists[i];
        if (rp_k.renc && l != rp_want) mad_rp_close(q);
        q->rp_next = mad_rp_next(q, count, (ID3D12CommandList *const *)lists, i); rp_want = q->rp_next; q->rp_idx = i;
        replay(q, l);
        q->open_lists++;
        if (q->open_lists >= 48) { mad_rp_close(q); flush(q); }
    }
    mad_rp_close(q); q->rp_carry = NULL; q->rp_next = NULL;
}

/* ---- synthetic lists ---- */
static struct mad_alloc alloc0;
static struct mad_cmd store[64][64]; static struct mad_list lists[64]; static int nlists;
static struct mad_list *L(void) { struct mad_list *l = &lists[nlists]; memset(l, 0, sizeof *l); l->cmds = store[nlists++]; l->closed = 1; l->alloc = &alloc0; return l; }
static struct mad_cmd *push(struct mad_list *l, enum mad_ck k) { struct mad_cmd *c = &l->cmds[l->ncmds++]; memset(c, 0, sizeof *c); c->kind = k; return c; }
static void rts(struct mad_list *l, int n, struct mad_resource **rt, struct mad_resource *d) {
    struct mad_cmd *c = push(l, MC_RTS); int i;
    for (i = 0; i < n; i++) { c->u.rts.rt[i] = rt[i]; c->u.rts.v[i].layers = 1; }
    c->u.rts.n = n; c->u.rts.depth = d; c->u.rts.dv.layers = 1;
}
static void vpsc(struct mad_list *l) {
    struct mad_cmd *c = push(l, MC_VP); c->u.vp.Width = 1408; c->u.vp.Height = 648;
    c = push(l, MC_SCISSOR); c->u.scissor.right = 1408; c->u.scissor.bottom = 648;
}
static void draw(struct mad_list *l) { push(l, MC_DRAW)->u.draw.vcount = 3; }
static void clear_rt(struct mad_list *l, struct mad_resource *r, float v) {
    struct mad_cmd *c = push(l, MC_CLEAR_RT); c->u.clear.res = r; c->u.clear.v.layers = 1; c->u.clear.rgba[0] = v; }
static void barrier(struct mad_list *l, struct mad_resource *r) {
    struct mad_cmd *c = push(l, MC_BARRIER); c->u.barrier.cls_noref = BC_NONE; c->u.barrier.res[0] = r; c->u.barrier.cls[0] = BC_ALL; c->u.barrier.n = 1; }
static void barrier_all(struct mad_list *l, int cls) {
    struct mad_cmd *c = push(l, MC_BARRIER); c->u.barrier.all = 1; c->u.barrier.cls_noref = (UINT8)cls; }
static void copy(struct mad_list *l, struct mad_resource *dst, struct mad_resource *src_) {
    struct mad_cmd *c = push(l, MC_COPY_T2T); c->u.tt.dst = dst; c->u.tt.src = src_; }
static void discard(struct mad_list *l, struct mad_resource *r, int whole) { struct mad_cmd *c = push(l, MC_DISCARD); c->u.discard.res = r; c->u.discard.whole = whole; }

static struct mad_resource A, B, X, Y, D, G[4];
static void tex(struct mad_resource *r, obj_handle_t h, UINT w, UINT hgt, enum WMTPixelFormat pf, int depth) {
    memset(r, 0, sizeof *r); r->texture = h; r->width = w; r->height = hgt; r->tex_pf = pf; r->samples = 1; r->tex_mips = 1;
    r->is_depth = depth; r->has_stencil = depth && pf == WMTPixelFormatDepth32Float_Stencil8;
    r->desc.Dimension = D3D12_RESOURCE_DIMENSION_TEXTURE2D; r->desc.Width = w; r->desc.Height = hgt; r->desc.DepthOrArraySize = 1; r->desc.MipLevels = 1;
}
static struct mad_queue Q; static struct mad_device DEV; static struct mad_resource *GB[5];
static void reset(int defer, int carry, int disc, int mode) {
    LONG64 *z64[] = { (LONG64 *)&g_tr_load, (LONG64 *)&g_tr_clear, (LONG64 *)&g_tr_noload, (LONG64 *)&g_tr_store, (LONG64 *)&g_tr_nostore,
                      (LONG64 *)&g_rp_carried_b, (LONG64 *)&g_rp_disc_load_b };
    unsigned i;
    for (i = 0; i < sizeof z64 / sizeof z64[0]; i++) *z64[i] = 0;
    g_tr_passes = g_tr_clear_only = g_tr_deferred = g_tr_def_flushed = g_rp_carried = g_rp_disc_whole = 0;
    memset((void *)g_rp_miss, 0, sizeof g_rp_miss); memset((void *)g_rp_miss_b, 0, sizeof g_rp_miss_b);
    cfg_defer = defer; cfg_carry = carry; cfg_discard = disc; g_clear_defer = g_rp_carry = g_rp_discard = -1; g_mode = mode;
    nev = 0; nlists = 0; memset(&Q, 0, sizeof Q); Q.device = &DEV; g_capture_on = 0;
    g_rp_late = -1; op24_ok = 1; op24_probes = 0; n24 = 0; memset((void *)g_rp_dset, 0, sizeof g_rp_dset); g_rp_dset_full = 0;
}
/* the store actions op 24 set for the pass of event ev (-1: none was set) */
static int late_color(int ev, int k) { int i; for (i = 0; i < n24; i++) if (r24[i].enc == evs[ev].h) return r24[i].color[k] == 0xff ? -2 : r24[i].color[k]; return -1; }
static int late_ds(int ev, int stencil) { int i; for (i = 0; i < n24; i++) if (r24[i].enc == evs[ev].h) return stencil ? r24[i].stencil : r24[i].depth; return -1; }
static int bad;
#define EXPECT(c, what) do { if (!(c)) { printf("FAIL %s (line %d)\n", what, __LINE__); bad = 1; } } while (0)
static int count(char kind) { int i, n = 0; for (i = 0; i < nev; i++) n += evs[i].kind == kind; return n; }
static int first_pass_with(obj_handle_t t, int from) {
    int i, k;
    for (i = from; i < nev; i++) if (evs[i].kind == 'R') {
        for (k = 0; k < 8; k++) if (evs[i].rpi.colors[k].texture == t) return i;
        if (evs[i].rpi.depth.texture == t) return i;
    }
    return -1;
}
static int color_load(int ev, obj_handle_t t) { int k; for (k = 0; k < 8; k++) if (evs[ev].rpi.colors[k].texture == t) return evs[ev].rpi.colors[k].load_action; return -1; }
static int all_ended_once(void) { int i; for (i = 0; i < nev; i++) if (evs[i].ended != 1) return 0; return 1; }
static LONG64 sz(struct mad_resource *r, int st) { return mad_tr_bytes(r, 0, 1, st); }
static LONG64 pass_bytes(struct mad_resource **rt, int n, struct mad_resource *d) {
    LONG64 b = 0; int i; for (i = 0; i < n; i++) b += sz(rt[i], 0); if (d) b += sz(d, 0) + sz(d, 1); return b; }

int main(void) {
    struct mad_list *l1, *l2, *l3, *ls[64]; struct mad_resource *a1[1], *ab[2]; int p, q2, i;
    tex(&A, 0xa0, 1408, 648, WMTPixelFormatRGBA16Float, 0); tex(&B, 0xb0, 1408, 648, WMTPixelFormatRGBA8Unorm, 0);
    tex(&X, 0x50, 256, 256, WMTPixelFormatRG16Float, 0); tex(&Y, 0x60, 256, 256, WMTPixelFormatRG16Float, 0);
    tex(&D, 0xd0, 1408, 648, WMTPixelFormatDepth32Float_Stencil8, 1);
    for (i = 0; i < 4; i++) tex(&G[i], 0x70 + i, 1408, 648, i & 1 ? WMTPixelFormatRGBA16Float : WMTPixelFormatRGBA8Unorm, 0);
    for (i = 0; i < 4; i++) GB[i] = &G[i];
    a1[0] = &A; ab[0] = &A; ab[1] = &B;

    /* texel sizes */
    EXPECT(mad_tr_bpp(WMTPixelFormatRGBA16Float, 0) == 8 && mad_tr_bpp(WMTPixelFormatRGBA8Unorm, 0) == 4 && mad_tr_bpp(WMTPixelFormatR8Unorm, 0) == 1 &&
           mad_tr_bpp(WMTPixelFormatRGBA32Float, 0) == 16 && mad_tr_bpp(WMTPixelFormatDepth16Unorm, 0) == 2 &&
           mad_tr_bpp(WMTPixelFormatDepth32Float_Stencil8, 0) == 4 && mad_tr_bpp(WMTPixelFormatDepth32Float_Stencil8, 1) == 1 &&
           mad_tr_bpp(WMTPixelFormatDepth32Float, 1) == 0 && mad_tr_bpp(WMTPixelFormatBGRX8Unorm, 0) == 4 && mad_tr_bpp(WMTPixelFormatRG16Float, 0) == 4,
           "texel sizes");

    /* 1. gpu-traffic: one pass, colour + depth + stencil, loaded and stored */
    reset(-1, -1, -1, 1);
    l1 = L(); rts(l1, 1, a1, &D); vpsc(l1); draw(l1); draw(l1);
    ls[0] = l1; ecl(&Q, 1, ls);
    EXPECT(count('R') == 1 && g_tr_passes == 1, "one pass");
    EXPECT(g_tr_load == 1408LL * 648 * (8 + 4 + 1) && g_tr_store == g_tr_load && !g_tr_clear && !g_tr_noload && !g_tr_nostore, "bytes by action");
    EXPECT(all_ended_once() && evs[0].waits == 1 && evs[0].updates == 1, "pass ended once, fenced once");

    /* 2. clear-defer on (default): a clear of another texture leaves the pass open and becomes the Clear of X's pass */
    reset(-1, -1, -1, 1);
    l1 = L(); rts(l1, 1, a1, NULL); vpsc(l1); draw(l1); clear_rt(l1, &X, 0.25f); draw(l1);
    { struct mad_resource *x1[1] = { &X }; rts(l1, 1, x1, NULL); } draw(l1);
    ls[0] = l1; ecl(&Q, 1, ls);
    EXPECT(count('R') == 2 && !g_tr_clear_only && g_tr_deferred == 1, "clear-defer: no clear-only pass, the A pass not split");
    p = first_pass_with(0x50, 0);
    EXPECT(p == 1 && color_load(p, 0x50) == WMTLoadActionClear && evs[p].rpi.colors[0].clear_color.r == 0.25, "X's pass starts with the clear");
    EXPECT(color_load(0, 0xa0) == WMTLoadActionLoad && all_ended_once(), "A's pass loads once and ends once");
    /* the same with clear-defer = 0: the old order */
    reset(0, -1, -1, 1);
    l1 = L(); rts(l1, 1, a1, NULL); vpsc(l1); draw(l1); clear_rt(l1, &X, 0.25f); draw(l1);
    { struct mad_resource *x1[1] = { &X }; rts(l1, 1, x1, NULL); } draw(l1);
    ls[0] = l1; ecl(&Q, 1, ls);
    EXPECT(count('R') == 4 && g_tr_clear_only == 1 && !g_tr_deferred, "clear-defer = 0: the clear splits A's pass and gets a pass of its own");
    EXPECT(evs[1].kind == 'R' && evs[1].rpi.colors[0].texture == 0x50 && color_load(1, 0x50) == WMTLoadActionClear &&
           evs[2].rpi.colors[0].texture == 0xa0 && color_load(2, 0xa0) == WMTLoadActionLoad && color_load(3, 0x50) == WMTLoadActionLoad,
           "clear-defer = 0: clear-only pass before the next A pass, X loaded later");

    /* 3. a clear of the open pass's own texture still ends it */
    reset(-1, -1, -1, 1);
    l1 = L(); rts(l1, 1, a1, NULL); vpsc(l1); draw(l1); clear_rt(l1, &A, 0.5f); draw(l1);
    ls[0] = l1; ecl(&Q, 1, ls);
    EXPECT(count('R') == 2 && color_load(1, 0xa0) == WMTLoadActionClear && evs[0].ended == 1, "own texture: the pass ends, the next one clears");

    /* 4. a barrier naming the pending texture applies the clear there, before the dispatch (the old order ran the dispatch first) */
    for (i = 1; i >= 0; i--) {
        reset(i, -1, -1, 1);
        l1 = L(); rts(l1, 1, a1, NULL); vpsc(l1); draw(l1); clear_rt(l1, &X, 1.0f); barrier(l1, &X);
        push(l1, MC_DISPATCH); rts(l1, 1, a1, NULL); draw(l1);
        ls[0] = l1; ecl(&Q, 1, ls);
        p = first_pass_with(0x50, 0);
        for (q2 = 0; q2 < nev && evs[q2].kind != 'C'; q2++) ;
        if (i) EXPECT(p >= 0 && q2 < nev && p < q2 && g_tr_def_flushed == 1, "clear-defer: the barrier applies the clear before the dispatch");
        else EXPECT(p >= 0 && q2 < nev && p > q2, "clear-defer = 0: the old order (dispatch before the clear)");
    }
    /* a barrier that only adds resource-less UAV barriers leaves it pending; one with unrecorded resources applies it */
    reset(-1, -1, -1, 1);
    l1 = L(); rts(l1, 1, a1, NULL); vpsc(l1); draw(l1); clear_rt(l1, &X, 1.0f); barrier_all(l1, BC_RAW_UAV); draw(l1);
    { struct mad_resource *x1[1] = { &X }; rts(l1, 1, x1, NULL); } draw(l1);
    ls[0] = l1; ecl(&Q, 1, ls);
    EXPECT(count('R') == 2 && !g_tr_clear_only && color_load(first_pass_with(0x50, 0), 0x50) == WMTLoadActionClear, "UAV-only barrier: still pending");
    reset(-1, -1, -1, 1);
    l1 = L(); rts(l1, 1, a1, NULL); vpsc(l1); draw(l1); clear_rt(l1, &X, 1.0f); barrier_all(l1, BC_ALL); draw(l1);
    ls[0] = l1; ecl(&Q, 1, ls);
    EXPECT(g_tr_clear_only == 1 && g_tr_def_flushed == 1 && count('R') == 3, "barrier with unrecorded resources: applied");
    /* a copy naming it applies it first; the list end applies the rest */
    reset(-1, -1, -1, 1);
    l1 = L(); rts(l1, 1, a1, NULL); vpsc(l1); draw(l1); clear_rt(l1, &X, 1.0f); clear_rt(l1, &B, 1.0f); copy(l1, &Y, &X); draw(l1);
    ls[0] = l1; ecl(&Q, 1, ls);
    p = first_pass_with(0x50, 0);
    for (q2 = 0; q2 < nev && evs[q2].kind != 'B'; q2++) ;
    EXPECT(p >= 0 && p < q2 && first_pass_with(0xb0, 0) > q2 && g_tr_clear_only == 2, "copy: X first; B at the list end");
    /* another view of a texture with a clear pending (views may overlap): a pass binding the texture through it,
     * or a clear through it, applies the pending clear first */
    reset(-1, -1, -1, 1);
    l1 = L(); rts(l1, 1, a1, NULL); vpsc(l1); draw(l1);
    { struct mad_cmd *c = push(l1, MC_CLEAR_RT); c->u.clear.res = &X; c->u.clear.v.level = 1; c->u.clear.v.layers = 1; c->u.clear.rgba[0] = 0.5f; }
    draw(l1);
    { struct mad_resource *x1[1] = { &X }; rts(l1, 1, x1, NULL); } draw(l1);
    ls[0] = l1; ecl(&Q, 1, ls);
    EXPECT(count('R') == 3 && g_tr_clear_only == 1 && g_tr_def_flushed == 1 && first_pass_with(0x50, 0) == 1 && color_load(1, 0x50) == WMTLoadActionClear &&
           evs[1].rpi.colors[0].level == 1 && color_load(2, 0x50) == WMTLoadActionLoad && evs[2].rpi.colors[0].level == 0 && all_ended_once(),
           "a pass binding X through another view: X's pending clear (mip 1) goes first; A's pass was not split by it");
    reset(-1, -1, -1, 1);
    l1 = L(); rts(l1, 1, a1, NULL); vpsc(l1); draw(l1);
    { struct mad_cmd *c = push(l1, MC_CLEAR_RT); c->u.clear.res = &X; c->u.clear.v.level = 1; c->u.clear.v.layers = 1; c->u.clear.rgba[0] = 0.5f; }
    clear_rt(l1, &X, 0.75f); draw(l1);
    { struct mad_resource *x1[1] = { &X }; rts(l1, 1, x1, NULL); } draw(l1);
    ls[0] = l1; ecl(&Q, 1, ls);
    EXPECT(count('R') == 4 && g_tr_clear_only == 1 && g_tr_def_flushed == 1 && evs[1].rpi.colors[0].texture == 0x50 && evs[1].rpi.colors[0].level == 1 &&
           color_load(3, 0x50) == WMTLoadActionClear && evs[3].rpi.colors[0].clear_color.r == 0.75 && all_ended_once(),
           "two clears of X through two views: the first goes first, the second is the Clear of X's pass");

    /* 5. rp-discard: off = counted, Load kept; on = DontCare */
    for (i = 0; i <= 1; i++) {
        reset(-1, -1, i, 1);
        l1 = L(); discard(l1, &A, 1); rts(l1, 1, a1, &D); vpsc(l1); draw(l1);
        ls[0] = l1; ecl(&Q, 1, ls);
        EXPECT(g_rp_disc_whole == 1 && g_rp_disc_load_b == sz(&A, 0), "discard: the load it makes moot is counted");
        EXPECT(color_load(0, 0xa0) == (i ? WMTLoadActionDontCare : WMTLoadActionLoad) && evs[0].rpi.depth.load_action == WMTLoadActionLoad,
               i ? "rp-discard = 1: A not loaded, depth still loaded" : "rp-discard off: A loaded");
    }
    reset(-1, -1, 1, 1);   /* a barrier in between, a partial discard, a discard inside its own pass: Load */
    l1 = L(); discard(l1, &A, 1); barrier(l1, &A); rts(l1, 1, a1, NULL); vpsc(l1); draw(l1);
    discard(l1, &B, 0); { struct mad_resource *b1[1] = { &B }; rts(l1, 1, b1, NULL); } draw(l1);
    rts(l1, 1, a1, NULL); draw(l1); discard(l1, &A, 1); draw(l1);
    { struct mad_resource *b1[1] = { &B }; rts(l1, 1, b1, NULL); } draw(l1); rts(l1, 1, a1, NULL); draw(l1);
    ls[0] = l1; ecl(&Q, 1, ls);
    EXPECT(count('R') == 5 && color_load(0, 0xa0) == WMTLoadActionLoad && color_load(1, 0xb0) == WMTLoadActionLoad &&
           color_load(4, 0xa0) == WMTLoadActionLoad && !g_rp_disc_load_b, "rp-discard: barrier, partial, mid-pass discards keep the Load");
    EXPECT(mad_discard_whole(&A, NULL) == 1, "whole: no region");
    { D3D12_DISCARD_REGION rg = { 0, NULL, 0, 1 }; D3D12_RECT rc = { 0, 0, 8, 8 };
      EXPECT(mad_discard_whole(&A, &rg) == 1, "whole: every subresource");
      rg.NumSubresources = 1; EXPECT(mad_discard_whole(&D, &rg) == 0, "depth + stencil: two planes");
      rg.NumSubresources = 2; EXPECT(mad_discard_whole(&D, &rg) == 1, "depth + stencil: both planes");
      rg.NumRects = 1; rg.pRects = &rc; EXPECT(mad_discard_whole(&A, &rg) == 0, "rectangles: not whole"); }

    /* 6. rp-carry: the G-buffer over three lists of one call */
    for (i = 0; i <= 1; i++) {
        reset(-1, i, -1, 1);
        GB[4] = NULL;
        for (q2 = 0; q2 < 3; q2++) { ls[q2] = L(); rts(ls[q2], 4, GB, &D); vpsc(ls[q2]); draw(ls[q2]); draw(ls[q2]); }
        ecl(&Q, 3, ls);
        if (i) EXPECT(count('R') == 1 && g_rp_carried == 2 && g_rp_carried_b == 4 * pass_bytes(GB, 4, &D) && all_ended_once() &&
                      evs[0].waits == 1 && evs[0].updates == 1 && g_tr_load == pass_bytes(GB, 4, &D), "rp-carry: one pass for three lists");
        else EXPECT(count('R') == 3 && !g_rp_carried && g_rp_miss[RPM_OFF] == 2 && g_rp_miss_b[RPM_OFF] == 4 * pass_bytes(GB, 4, &D) &&
                    all_ended_once(), "rp-carry off: three passes, two counted as possible");
    }
    /* boundaries that stay */
    {
        static const char *what[] = { "barrier in the next list's prefix", "barrier after the last draw", "no scissor set again",
                                      "other targets", "blend factor left set", "capture on", "mode 6, pass not synced" };
        int k;
        for (k = 0; k < 7; k++) {
            reset(-1, 1, -1, k == 6 ? 6 : 1);
            l1 = L(); l2 = L();
            if (k == 6) { rts(l1, 1, ab + 1, NULL); vpsc(l1); draw(l1); }   /* the list's first pass syncs, the second does not */
            rts(l1, 4, GB, &D); vpsc(l1);
            if (k == 4) push(l1, MC_BLEND_FACTOR);
            draw(l1);
            if (k == 1) barrier(l1, &X);
            if (k == 0) barrier(l2, &X);
            if (k == 3) rts(l2, 1, a1, &D); else rts(l2, 4, GB, &D);
            if (k != 2) vpsc(l2); else push(l2, MC_VP);
            draw(l2);
            if (k == 5) g_capture_on = 1;
            ls[0] = l1; ls[1] = l2; ecl(&Q, 2, ls);
            g_capture_on = 0;
            EXPECT(!g_rp_carried && all_ended_once(), what[k]);
            EXPECT(k == 3 ? !g_rp_miss[RPM_PREFIX] && !g_rp_miss[RPM_STATE] :
                   k <= 2 || k == 4 ? g_rp_miss[RPM_PREFIX] == 1 : g_rp_miss[RPM_STATE] == 1, what[k]);
        }
    }
    /* mode 6 with a synced pass carries */
    reset(-1, 1, -1, 6);
    l1 = L(); l2 = L(); rts(l1, 4, GB, &D); vpsc(l1); draw(l1); rts(l2, 4, GB, &D); vpsc(l2); draw(l2);
    ls[0] = l1; ls[1] = l2; ecl(&Q, 2, ls);
    EXPECT(g_rp_carried == 1 && count('R') == 1 && all_ended_once(), "mode 6, synced pass: carried");
    /* pending clears go along; a clear of another texture after the last draw does not stop the carry */
    reset(-1, 1, -1, 1);
    l1 = L(); l2 = L(); rts(l1, 4, GB, &D); vpsc(l1); draw(l1); clear_rt(l1, &X, 0.75f);
    rts(l2, 4, GB, &D); vpsc(l2); draw(l2); { struct mad_resource *x1[1] = { &X }; rts(l2, 1, x1, NULL); } draw(l2);
    ls[0] = l1; ls[1] = l2; ecl(&Q, 2, ls);
    p = first_pass_with(0x50, 0);
    EXPECT(g_rp_carried == 1 && count('R') == 2 && !g_tr_clear_only && p == 1 && color_load(p, 0x50) == WMTLoadActionClear &&
           evs[p].rpi.colors[0].clear_color.r == 0.75 && all_ended_once(), "pending clear carried into the next list");
    /* the 48-list bound: nothing is carried into a commit */
    reset(-1, 1, -1, 1);
    Q.open_lists = 46;
    for (q2 = 0; q2 < 3; q2++) { ls[q2] = L(); rts(ls[q2], 4, GB, &D); vpsc(ls[q2]); draw(ls[q2]); }
    ecl(&Q, 3, ls);
    EXPECT(g_rp_carried == 1 && count('R') == 2 && Q.batches == 1 && all_ended_once(), "48-list bound: the 47th carries into the 48th only");
    /* the last list of a call: the next call's first list is counted (same command buffer, then after a commit) */
    reset(-1, 1, -1, 1);
    l1 = L(); rts(l1, 4, GB, &D); vpsc(l1); draw(l1); ls[0] = l1; ecl(&Q, 1, ls);
    l2 = L(); rts(l2, 4, GB, &D); vpsc(l2); draw(l2); ls[0] = l2; ecl(&Q, 1, ls);
    flush(&Q);
    l3 = L(); rts(l3, 4, GB, &D); vpsc(l3); draw(l3); ls[0] = l3; ecl(&Q, 1, ls);
    EXPECT(!g_rp_carried && g_rp_miss[RPM_CALL] == 1 && g_rp_miss[RPM_COMMIT] == 1 && g_rp_miss_b[RPM_CALL] == 2 * pass_bytes(GB, 4, &D) &&
           count('R') == 3 && all_ended_once(), "next call: counted, never carried");
    /* a carry handed to a list the loop did not replay is ended, fenced, before anything else */
    reset(-1, 1, -1, 1);
    l1 = L(); l2 = L(); rts(l1, 4, GB, &D); vpsc(l1); draw(l1); rts(l2, 4, GB, &D); vpsc(l2); draw(l2);
    { struct mad_rp_carry k0; memset(&k0, 0, sizeof k0); Q.rp_carry = &k0; Q.rp_next = l2; replay(&Q, l1);
      EXPECT(k0.renc && evs[0].ended == 0, "carried at the list end");
      srw_taken = 0; mad_rp_close(&Q);
      EXPECT(!k0.renc && evs[0].ended == 1 && evs[0].updates == 1, "mad_rp_close ends it with its fence update");
      EXPECT(srw_taken == 1 && !srw_held, "mad_rp_close: its static replay state under its lock, released"); Q.rp_carry = NULL; }
    /* the same with a store left open (rp-discard) and a pending clear going along: both settled there */
    reset(-1, 1, 1, 1); mad_rp_dset_add(&A);
    l1 = L(); l2 = L(); rts(l1, 1, a1, NULL); vpsc(l1); draw(l1); clear_rt(l1, &X, 0.5f); rts(l2, 1, a1, NULL); vpsc(l2); draw(l2);
    { struct mad_rp_carry k0; memset(&k0, 0, sizeof k0); Q.rp_carry = &k0; Q.rp_next = l2; replay(&Q, l1);
      EXPECT(k0.renc && k0.late && k0.npend == 1 && !n24 && count('R') == 1, "carried with its store left open and a pending clear");
      mad_rp_close(&Q);
      EXPECT(!k0.renc && evs[0].ended == 1 && evs[0].late_before_end && late_color(0, 0) == WMTStoreActionStore && count('R') == 2 &&
             first_pass_with(X.texture, 1) == 1 && color_load(1, X.texture) == WMTLoadActionClear && evs[1].ended == 1 && all_ended_once(),
             "mad_rp_close: the store decided (kept: no list to look at), then the pending clear applied");
      Q.rp_carry = NULL; }
    /* 7. rp-discard's store side: decided at the pass end through op 24 */
    { struct mad_resource *b1[1] = { &B };
    reset(-1, -1, 1, 1); mad_rp_dset_add(&A);
    l1 = L(); rts(l1, 1, a1, NULL); vpsc(l1); draw(l1); discard(l1, &A, 1);
    ls[0] = l1; ecl(&Q, 1, ls);
    EXPECT(op24_probes == 1 && evs[0].rpi.colors[0].store_action == WMTStoreActionUnknown && late_color(0, 0) == WMTStoreActionDontCare &&
           late_color(0, 1) == -2 && g_tr_nostore == sz(&A, 0) && !g_tr_store, "discard after the last draw: not stored");
    EXPECT(n24 == 1 && f24[0] && evs[0].ended == 1 && evs[0].late_before_end, "op 24 after the thread's queued commands went out, before endEncoding");
    reset(-1, -1, 1, 1); mad_rp_dset_add(&A);
    l1 = L(); rts(l1, 1, a1, NULL); vpsc(l1); draw(l1); rts(l1, 1, b1, NULL); draw(l1); rts(l1, 1, a1, NULL); draw(l1); discard(l1, &A, 1);
    ls[0] = l1; ecl(&Q, 1, ls);
    EXPECT(count('R') == 3 && late_color(0, 0) == WMTStoreActionStore && late_color(2, 0) == WMTStoreActionDontCare &&
           evs[1].rpi.colors[0].store_action == WMTStoreActionStore && late_color(1, 0) == -1, "a pass that loads it next: stored; B untouched");
    reset(-1, -1, 1, 1); mad_rp_dset_add(&A);   /* the next list of the call discards it; in the next call: kept */
    l1 = L(); rts(l1, 1, a1, NULL); vpsc(l1); draw(l1); l2 = L(); discard(l2, &A, 1);
    ls[0] = l1; ls[1] = l2; ecl(&Q, 2, ls);
    EXPECT(late_color(0, 0) == WMTStoreActionDontCare, "discarded by the next list of the call: not stored");
    reset(-1, -1, 1, 1); mad_rp_dset_add(&A);
    l1 = L(); rts(l1, 1, a1, NULL); vpsc(l1); draw(l1); ls[0] = l1; ecl(&Q, 1, ls);
    l2 = L(); discard(l2, &A, 1); ls[0] = l2; ecl(&Q, 1, ls);
    EXPECT(late_color(0, 0) == WMTStoreActionStore, "discarded only in the next call: stored");
    reset(-1, -1, 1, 1); mad_rp_dset_add(&A);
    l1 = L(); rts(l1, 1, a1, NULL); vpsc(l1); draw(l1); barrier(l1, &A); discard(l1, &A, 1);
    ls[0] = l1; ecl(&Q, 1, ls);
    EXPECT(late_color(0, 0) == WMTStoreActionStore, "a barrier first: stored");
    reset(-1, -1, 1, 1); mad_rp_dset_add(&A);   /* ended by a dispatch, then drawn again with A still bound: stored */
    l1 = L(); rts(l1, 1, a1, NULL); vpsc(l1); draw(l1); push(l1, MC_DISPATCH); draw(l1); discard(l1, &A, 1);
    ls[0] = l1; ecl(&Q, 1, ls);
    EXPECT(count('R') == 2 && late_color(0, 0) == WMTStoreActionStore && late_color(2, 0) == WMTStoreActionDontCare,
           "a later pass still bound to it loads it: stored; that pass, discarded after: not stored");
    reset(-1, -1, 1, 1); mad_rp_dset_add(&A);   /* discarded right after its last draw, the pass ends later */
    l1 = L(); rts(l1, 1, a1, NULL); vpsc(l1); draw(l1); discard(l1, &A, 1); rts(l1, 1, b1, NULL); draw(l1);
    ls[0] = l1; ecl(&Q, 1, ls);
    EXPECT(late_color(0, 0) == WMTStoreActionDontCare, "discard after the last draw, before the pass ends: not stored");
    reset(-1, -1, 1, 1); mad_rp_dset_add(&A); op24_ok = 0;
    l1 = L(); rts(l1, 1, a1, NULL); vpsc(l1); draw(l1); discard(l1, &A, 1); ls[0] = l1; ecl(&Q, 1, ls);
    EXPECT(evs[0].rpi.colors[0].store_action == WMTStoreActionStore && !n24 && !g_rp_late, "winemetal without op 24: Store at creation");
    reset(-1, -1, 0, 1); mad_rp_dset_add(&A);
    l1 = L(); rts(l1, 1, a1, NULL); vpsc(l1); draw(l1); discard(l1, &A, 1); ls[0] = l1; ecl(&Q, 1, ls);
    EXPECT(evs[0].rpi.colors[0].store_action == WMTStoreActionStore && !n24 && !op24_probes, "rp-discard off: no probe, Store");
    reset(-1, -1, 1, 1); mad_rp_dset_add(&D);   /* a depth-only clear next: depth not stored, stencil and colour stored */
    l1 = L(); rts(l1, 1, a1, &D); vpsc(l1); draw(l1);
    { struct mad_cmd *c = push(l1, MC_CLEAR_DS); c->u.clear.res = &D; c->u.clear.v.layers = 1; c->u.clear.flags = D3D12_CLEAR_FLAG_DEPTH; }
    draw(l1); ls[0] = l1; ecl(&Q, 1, ls);
    EXPECT(late_ds(0, 0) == WMTStoreActionDontCare && late_ds(0, 1) == WMTStoreActionStore && late_color(0, 0) == WMTStoreActionStore &&
           evs[1].rpi.depth.load_action == WMTLoadActionClear && g_tr_nostore == sz(&D, 0), "depth-only clear next: only depth not stored");
    reset(-1, 1, 1, 1); mad_rp_dset_add(&A);   /* a carried pass decides at its real end */
    l1 = L(); rts(l1, 1, a1, NULL); vpsc(l1); draw(l1); l2 = L(); rts(l2, 1, a1, NULL); vpsc(l2); draw(l2); discard(l2, &A, 1);
    ls[0] = l1; ls[1] = l2; ecl(&Q, 2, ls);
    EXPECT(g_rp_carried == 1 && count('R') == 1 && n24 == 1 && late_color(0, 0) == WMTStoreActionDontCare && all_ended_once(),
           "carried pass: decided once, at its end in the next list"); }
    /* f6-rar: only batches of read-to-read entries, only with the switch */
    { struct mad_cmd c; memset(&c, 0, sizeof c); c.kind = MC_BARRIER; c.u.barrier.cls_noref = BC_NONE;
      c.u.barrier.n = 2; c.u.barrier.res[0] = &A; c.u.barrier.res[1] = &B; c.u.barrier.cls[0] = BC_RAR; c.u.barrier.cls[1] = BC_RAR;
      cfg_rar = -1; g_f6_rar = -1; EXPECT(!f6_rar_only(&c), "f6-rar off by default");
      cfg_rar = 1; g_f6_rar = -1; EXPECT(f6_rar_only(&c), "f6-rar: read-to-read batch passes");
      c.u.barrier.cls[1] = BC_RAW_ATT; EXPECT(!f6_rar_only(&c), "f6-rar: a render-target-to-read entry syncs");
      c.u.barrier.cls[1] = BC_RAR; c.u.barrier.all = 1; c.u.barrier.cls_noref = BC_RAW_UAV; EXPECT(!f6_rar_only(&c), "f6-rar: a UAV barrier syncs");
      c.u.barrier.all = 0; c.u.barrier.n = 0; EXPECT(!f6_rar_only(&c), "f6-rar: an empty batch is not passed");
      cfg_rar = -1; g_f6_rar = -1; }
    /* the prefix test itself */
    reset(-1, 1, -1, 1);
    l1 = L(); rts(l1, 4, GB, &D); vpsc(l1); push(l1, MC_DISCARD)->u.discard.res = &X; clear_rt(l1, &X, 0); draw(l1);
    { struct mad_rtvp v[8], dv; memset(v, 0, sizeof v); memset(&dv, 0, sizeof dv); for (i = 0; i < 8; i++) v[i].layers = 1; dv.layers = 1;
      EXPECT(mad_rp_prefix(l1, GB, v, 4, &D, &dv, 0) == 2, "prefix: state, a discard and a clear of another texture continue the pass");
      EXPECT(mad_rp_prefix(l1, GB, v, 4, &D, &dv, 1) == 1, "prefix: blend set before, not set again");
      EXPECT(mad_rp_prefix(l1, GB, v, 3, &D, &dv, 0) == 0, "prefix: other targets");
      l2 = L(); rts(l2, 4, GB, &D); vpsc(l2); clear_rt(l2, &G[2], 0); draw(l2);
      EXPECT(mad_rp_prefix(l2, GB, v, 4, &D, &dv, 0) == 1, "prefix: a clear of one of the pass's textures ends it");
      l3 = L(); rts(l3, 0, NULL, NULL); rts(l3, 4, GB, &D); vpsc(l3); draw(l3);
      EXPECT(mad_rp_prefix(l3, GB, v, 4, &D, &dv, 0) == 1, "prefix: an attachment-less binding ends it"); }
    if (!bad) printf("ok\n");
    return bad;
}
'''

cc = os.environ.get("CC") if os.environ.get("CC") and "clang" in os.environ.get("CC") else shutil.which("clang")
std = "-std=gnu11"
if not cc:
    cc, std = shutil.which("gcc") or shutil.which("cc"), "-std=gnu2x"
if not cc:
    print("note: no host compiler; runtime checks skipped")
else:
    with tempfile.TemporaryDirectory() as t:
        t = Path(t)
        (t / "h.c").write_text(harness)
        exe = t / "h"
        r = subprocess.run([cc, std, "-Wall", "-Wno-unused-function", "-Wno-fixed-enum-extension", "-Wno-unknown-warning-option",
                            "-Wno-unused-variable", "-Wno-unused-but-set-variable", "-Wno-missing-braces",
                            "-DDXMT_NATIVE", "-I%s" % (root / "dxmt/src/winemetal"), "-o", str(exe), str(t / "h.c")],
                           capture_output=True, text=True)
        if r.returncode:
            print(r.stderr[-4000:])
        out = subprocess.run([str(exe)], capture_output=True, text=True).stdout if exe.exists() else ""
        check("render-pass runtime (traffic, clear-defer, rp-discard, rp-carry, census)", r.returncode == 0 and out.strip() == "ok")
        if out.strip() != "ok":
            print(out[:4000])

print("PASS" if ok else "FAILED")
sys.exit(0 if ok else 1)
