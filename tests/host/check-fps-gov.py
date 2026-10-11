#!/usr/bin/env python3
"""The frame-cap power governor (opt-in) holds play unclamped for longer; no Wine runs.

iOS clamps the CPU once a leaky energy bucket fills: Horizon Zero Dawn
(builds 486-493) after 273-330 J of process CPU energy, spent mostly in menus
and loading at 2.75-3.8 W; Red Dead Redemption 2 (build 460) ran its menus at
57-60 fps for two minutes and was clamped before play. Once clamped the
process gets ~0.55 W and Horizon Zero Dawn runs ~19 fps. ECO as the actuator
(ios_power_gov_step) made GTA V Enhanced crash while loading (build 449), so
this governor moves a frame cap instead (server_ios.c ios_fps_gov_step):
env.MADEIRA_FPS_CAP = N is a fixed cap, env.MADEIRA_FPS_GOV_MW = P holds the
process CPU power at P mW by moving the cap between 60/40/30/24/20 fps.

Checks:
  1. the production ios_fps_gov_step, compiled on this host and driven by a
     game model on 250 ms samples (power = base + energy per frame x frames,
     loading phases that present nothing, a leaky bucket that clamps the CPU
     to 0.55 W once it holds C J above a leak L), for two models (an HZD-like
     30 fps menu, an RDR2-like 60 fps menu) and five buckets (L 1.0-1.5 W,
     C 190-320 J, the fits of the seven evidence logs and the brief's):
       - unset: nothing armed, the cap stays 0;
       - MADEIRA_FPS_CAP=30: the cap is 30 from the first sample;
       - MADEIRA_FPS_GOV_MW=1500: the 10 s averages hold 1.5 W within 0.25 W
         while the cap can reach it, no change closer than 3 s, the cap held
         while nothing presents; in every model and bucket the clamp comes
         later than without the governor (or never in the 10 min session) and
         play gets more unclamped seconds;
  2. ios_fps_cap_dxmt_pace (server_ios.c), compiled with a fake clock: unarmed
     it does nothing; with a 30 fps cap and frames ready every 10 ms it spaces
     them 33.3 ms apart on average; slower frames are not held; with a D3D12
     counter block published it only counts;
  3. mad_fps_cap_pace (madeira-d3d12 Present), compiled with stand-ins: a
     0 cap does nothing, a 30 fps cap holds a 10 ms frame to 33.3 ms with one
     NtDelayExecution per frame, a frame slower than the cap waits for
     nothing, a stall re-bases the deadline;
  4. textually: Present paces after its timing and never for DXGI_PRESENT_TEST;
     the counter block's first 20 fields match ios_xp_pe and fps_cap follows
     them; the xprobe writes the cap only into a block that long and only when
     armed; the winemetal patch runs in CI after the GPU trace patch, applies
     to winemetal_unix.c once and is idempotent; catalog and CI step.
"""
from pathlib import Path
import os
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "build/ntdll-unix/server_ios.c"
D3D = ROOT / "madeira-d3d12/src/pe/madeira_d3d12.c"
PATCH = ROOT / "tools/patch-winemetal-fps-cap.py"
WM = ROOT / "dxmt/src/winemetal/unix/winemetal_unix.c"
CAT = ROOT / "app/Madeira/ConfigCatalog.generated.swift"
GEN = ROOT / "build/tools/gen-config-catalog.py"
CI = ROOT / ".github/workflows/build-ipa.yml"

failures = []


def check(cond, what):
    print(("PASS " if cond else "FAIL ") + what)
    if not cond:
        failures.append(what)


def func_body(src, signature):
    i = src.find(signature)
    if i < 0:
        return ""
    j = src.find("{", i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    return ""


def build(code, name, tmp):
    c = Path(tmp) / (name + ".c")
    exe = Path(tmp) / name
    c.write_text(code)
    cc = os.environ.get("CC", "cc")
    base = [cc, "-std=gnu11", "-O1", "-Wall", "-Werror", "-Wno-unused-function", "-Wno-unused-variable",
            "-Wno-unused-const-variable", str(c), "-o", str(exe), "-lm"]
    r = subprocess.run(base[:1] + ["-fsanitize=address,undefined", "-fno-sanitize-recover=all"] + base[1:],
                       capture_output=True, text=True)
    if r.returncode != 0:
        r = subprocess.run(base, capture_output=True, text=True)
    return (exe if r.returncode == 0 else None), r.stderr


server = SERVER.read_text(encoding="utf-8")
d3d = D3D.read_text(encoding="utf-8")
step = func_body(server, "static void ios_fps_gov_step( uint64_t now, double dt_ms, double mj, uint64_t presents, double paced_ms )")
pace = func_body(server, "unsigned long long ios_fps_cap_dxmt_pace( void )")
check(bool(step) and bool(pace), "ios_fps_gov_step and ios_fps_cap_dxmt_pace found")
decl_at = server.find("volatile int ios_fps_cap;")
decls = server[decl_at:server.find("static void ios_fps_gov_step(")]
check(decl_at > 0 and "static const int ios_fps_steps[] = { 60, 40, 30, 24, 20 };" in decls, "steps 60/40/30/24/20")

# ---- 1. the governor against a game model ------------------------------------
model = r'''
#define _GNU_SOURCE
#include <math.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
static char last_log[512]; static double avg_w[200]; static int navg, held_logs, nlogs;
static void wine_log_write( const char *fmt, ... ) __attribute__((format(printf, 1, 2)));
static void wine_log_write( const char *fmt, ... )
{
    va_list ap; va_start( ap, fmt ); vsnprintf( last_log, sizeof(last_log), fmt, ap ); va_end( ap ); nlogs++;
    if (getenv( "VERBOSE" )) puts( last_log );
    if (!strncmp( last_log, "[fps-gov] last", 14 )) avg_w[navg++] = atof( strstr( last_log, "s: " ) + 3 );
    if (strstr( last_log, "cap held at" )) held_logs++;
}
''' + decls + step + r'''
/* phases: kind 0 = loading (no presents), 1 = menu, 2 = play */
struct phase { double until_s; int kind; double base_w, frame_j, max_fps; };
static const struct phase hzd[] = {      /* menu at 30 fps ~3.7 W (493a 09:10:28-09:11:19), play 2-3 W, ~19 fps clamped */
    { 20, 0, 3.0, 0, 0 }, { 80, 1, 1.0, 0.090, 30 }, { 95, 0, 3.5, 0, 0 }, { 600, 2, 0.6, 0.075, 30 } };
static const struct phase rdr2[] = {     /* menus 57-60 fps at ~2-3 W for two minutes (460), play up to 40 fps */
    { 30, 0, 2.5, 0, 0 }, { 150, 1, 0.4, 0.032, 60 }, { 165, 0, 3.5, 0, 0 }, { 600, 2, 0.8, 0.060, 40 } };
static const struct phase steady[] = {   /* one long 60 fps menu: the averages */
    { 600, 1, 0.4, 0.032, 60 } };
int main( void )
{
    const struct phase *ph = !strcmp( getenv( "MODEL" ), "rdr2" ) ? rdr2 : !strcmp( getenv( "MODEL" ), "steady" ) ? steady : hzd;
    double leak = atof( getenv( "LEAK" ) ), cap_j = atof( getenv( "CAP_J" ) );
    double t = 0, dt = 0.25, bucket = 0, clamp_s = -1, frames = 0, play_unclamped = 0, play_s = 0, play_frames = 0;
    double energy = 0, frac = 0;
    uint64_t now = 0, presents = 0, last_change_ns = 0, min_gap = ~0ull;
    int i = 0, last_cap = -1, changes = 0, first_cap = -1;
    while (t < 600)
    {
        const struct phase *p = ph; double fps, w;
        while (t >= p->until_s) p++;
        if (p->kind == 0) { fps = 0; w = p->base_w; }
        else
        {
            fps = p->max_fps;
            if (fps > 60) fps = 60;                                   /* the display's 60 cap */
            if (ios_fps_cap && fps > ios_fps_cap) fps = ios_fps_cap;
            w = p->base_w + p->frame_j * fps;
        }
        if (clamp_s >= 0)                                             /* clamped: ~0.55 W, CPU-bound frame rate */
        {
            if (p->kind) { double f = (0.55 - p->base_w * 0.3) / (p->frame_j * 0.3); if (fps > f) fps = f; if (fps < 0) fps = 0; }
            w = 0.55;
        }
        bucket += (w - leak) * dt; if (bucket < 0) bucket = 0;
        if (clamp_s < 0 && bucket >= cap_j) clamp_s = t;
        energy += w * dt;
        frac += fps * dt; while (frac >= 1) { presents++; frac -= 1; }
        frames += fps * dt;
        if (p->kind == 2) { play_s += dt; play_frames += fps * dt; if (clamp_s < 0) play_unclamped += dt; }
        now += 250000000ull; t += dt; i++;
        ios_fps_gov_step( now, dt * 1000, w * dt * 1000, presents, 0 );
        if (first_cap < 0) first_cap = ios_fps_cap;
        if (ios_fps_cap != last_cap)
        {
            if (last_cap > 0) { changes++; if (last_change_ns && now - last_change_ns < min_gap) min_gap = now - last_change_ns; last_change_ns = now; }
            last_cap = ios_fps_cap;
        }
    }
    printf( "%d %d %.1f %.1f %.1f %.1f %.1f %d %llu %d %d", ios_fps_gov_armed, first_cap, clamp_s, play_unclamped,
            play_s ? play_frames / play_s : 0, energy, frames, changes,
            (unsigned long long)(min_gap == ~0ull ? 0 : min_gap / 1000000), held_logs, navg );
    for (i = 0; i < navg; i++) printf( " %.3f", avg_w[i] );
    printf( "\n" );
    return 0;
}
'''

buckets = [(1.0, 240), (1.25, 210), (1.5, 190), (1.0, 320), (1.5, 320)]
with tempfile.TemporaryDirectory(prefix="madeira-fps-gov-") as tmp:
    exe, err = build(model, "gov", tmp)
    check(exe is not None, "ios_fps_gov_step compiles against the model" + ("" if exe else ": " + err[-900:]))
    if exe:
        clean = {k: v for k, v in os.environ.items() if not k.startswith("MADEIRA_")}

        def run(model_name, leak, cap_j, **sw):
            env = dict(clean, MODEL=model_name, LEAK=str(leak), CAP_J=str(cap_j), **sw)
            r = subprocess.run([str(exe)], capture_output=True, text=True, env=env)
            assert r.returncode == 0, r.stdout + r.stderr
            f = r.stdout.split()
            return dict(armed=int(f[0]), first=int(f[1]), clamp=float(f[2]), unclamped=float(f[3]), play_fps=float(f[4]),
                        energy=float(f[5]), frames=float(f[6]), changes=int(f[7]), gap=int(f[8]), held=int(f[9]),
                        avgs=[float(x) for x in f[11:]])

        off = run("hzd", 1.0, 240)
        check(off["armed"] == 0 and off["first"] == 0 and off["changes"] == 0, "unset: nothing armed, the cap stays 0")
        fixed = run("rdr2", 1.0, 240, MADEIRA_FPS_CAP="30")
        check(fixed["armed"] == 1 and fixed["first"] == 30 and fixed["changes"] == 0, "MADEIRA_FPS_CAP=30: cap 30 from the first sample")
        bad = run("rdr2", 1.0, 240, MADEIRA_FPS_CAP="5")
        check(bad["armed"] == 0 and bad["first"] == 0, "MADEIRA_FPS_CAP outside 10-120 is ignored")
        lines = []
        for m in ("hzd", "rdr2"):
            for leak, cap_j in buckets:
                o = run(m, leak, cap_j)
                g = run(m, leak, cap_j, MADEIRA_FPS_GOV_MW="1500")
                c30 = run(m, leak, cap_j, MADEIRA_FPS_CAP="30")
                lines.append("    %-4s L=%.2f W C=%3d J | off: clamp %5.1f s, %5.1f s unclamped play, play %4.1f fps | "
                             "cap 30: clamp %5.1f s, %5.1f s | gov 1.5 W: clamp %5.1f s, %5.1f s unclamped play, play %4.1f fps, "
                             "%d changes" % (m, leak, cap_j, o["clamp"], o["unclamped"], o["play_fps"],
                                             c30["clamp"], c30["unclamped"], g["clamp"], g["unclamped"], g["play_fps"], g["changes"]))
                later = g["clamp"] < 0 or (o["clamp"] >= 0 and g["clamp"] > o["clamp"] + 10)
                check(o["clamp"] >= 0 and later and g["unclamped"] > o["unclamped"],
                      "%s L=%.2f C=%d: the governor delays the clamp and adds unclamped play (%.0f -> %.0f s)"
                      % (m, leak, cap_j, o["unclamped"], g["unclamped"]))
                check(g["gap"] >= 3000, "%s L=%.2f C=%d: no cap change closer than 3 s (%d ms)" % (m, leak, cap_j, g["gap"]))
                check(g["held"] >= 1, "%s: the cap is held while nothing presents" % m)
        print("\n".join(lines))
        g = run("steady", 1.0, 1e9, MADEIRA_FPS_GOV_MW="1500")   # a long 60 fps menu, no clamp: the averages
        reach = g["avgs"][2:]
        check(len(reach) >= 50 and all(abs(a - 1.5) <= 0.25 for a in reach) and abs(sum(reach) / len(reach) - 1.5) <= 0.05,
              "a 60 fps menu at 2.3 W: every 10 s average after the first 20 s holds 1.5 W within 0.25 W, %.3f W overall"
              % (sum(reach) / max(len(reach), 1)))
        g = run("steady", 1.0, 1e9, MADEIRA_FPS_GOV_MW="1500", MADEIRA_FPS_GOV_MIN="40")
        check(g["changes"] == 1 and abs(g["avgs"][-1] - 1.68) < 0.05,
              "MADEIRA_FPS_GOV_MIN=40: the cap stops at 40 although 1.5 W would need less (%.2f W)" % g["avgs"][-1])

# ---- 2. ios_fps_cap_dxmt_pace with a fake clock -------------------------------
pace_h = r'''
#define _GNU_SOURCE
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
typedef struct { uint32_t numer, denom; } mach_timebase_info_data_t;
static uint64_t clk;   /* ticks of 1/24 MHz... here 1 ns: numer = denom = 1 */
static void mach_timebase_info( mach_timebase_info_data_t *t ) { t->numer = 125; t->denom = 3; }
static uint64_t mach_absolute_time( void ) { return clk; }
static int waits;
static int mach_wait_until( uint64_t when ) { waits++; if (when > clk) clk = when; return 0; }
volatile uint64_t ios_xp_pe_block;
''' + decls + pace + r'''
int main( int argc, char **argv )
{
    int armed = atoi( argv[1] ), cap = atoi( argv[2] ), work_ms = atoi( argv[3] ), block = atoi( argv[4] ), i;
    uint64_t first = 0, last = 0;
    ios_fps_gov_armed = armed; ios_fps_cap = cap; ios_xp_pe_block = block ? 0x1000 : 0;
    for (i = 0; i < 61; i++)
    {
        clk += (uint64_t)work_ms * 1000000ull * 3 / 125;   /* the frame's own work */
        ios_fps_cap_dxmt_pace();
        if (i == 1) first = clk;
        last = clk;
    }
    printf( "%llu %d %.2f\n", (unsigned long long)ios_fps_presents, waits, (double)(last - first) * 125 / 3 / 1e6 / 59 );
    return 0;
}
'''
with tempfile.TemporaryDirectory(prefix="madeira-fps-pace-") as tmp:
    exe, err = build(pace_h, "pace", tmp)
    check(exe is not None, "ios_fps_cap_dxmt_pace compiles with a fake clock" + ("" if exe else ": " + err[-900:]))
    if exe:
        def prun(*a):
            r = subprocess.run([str(exe)] + [str(x) for x in a], capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
            f = r.stdout.split()
            return int(f[0]), int(f[1]), float(f[2])
        n, w, ms = prun(0, 30, 10, 0)
        check(n == 0 and w == 0 and abs(ms - 10) < 0.01, "unarmed: no count, no wait (%s)" % ((n, w, ms),))
        n, w, ms = prun(1, 30, 10, 0)
        check(n == 61 and w >= 58 and abs(ms - 33.33) < 0.1, "30 fps cap, 10 ms frames: %.2f ms apart, %d waits" % (ms, w))
        n, w, ms = prun(1, 30, 50, 0)
        check(n == 61 and w == 0 and abs(ms - 50) < 0.01, "frames slower than the cap are not held (%.2f ms)" % ms)
        n, w, ms = prun(1, 30, 10, 1)
        check(n == 61 and w == 0, "a published D3D12 block: counted, not paced (D3D12 paces in its Present)")
        n, w, ms = prun(1, 0, 10, 0)
        check(n == 61 and w == 0, "armed with no cap: counted, not paced")

# ---- 3. mad_fps_cap_pace (madeira-d3d12) with stand-ins ------------------------
mpace = func_body(d3d, "static void mad_fps_cap_pace(struct mad_swapchain *s, LONG64 now)")
check(bool(mpace), "mad_fps_cap_pace found in madeira_d3d12.c")
mpace_h = r'''
#define _GNU_SOURCE
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
typedef int64_t LONG64; typedef int64_t LONGLONG; typedef int32_t LONG; typedef unsigned char BOOLEAN; typedef uint32_t DWORD;
typedef union { LONGLONG QuadPart; } LARGE_INTEGER; typedef void *HMODULE;
#define WINAPI
#define FALSE 0
static LONG64 clk;   /* QPC ticks at 10 MHz */
static int delays, sleeps, lookups;
static struct { volatile LONG64 fps_cap; LONG64 qpf, t_cap, n_cap; } g_xp;
struct mad_swapchain { LONG64 cap_next; };
static LONG64 mad_qpc( void ) { return clk; }
static LONG WINAPI fake_delay( BOOLEAN a, const LARGE_INTEGER *rel ) { (void)a; delays++; clk += -rel->QuadPart; return 0; }
static HMODULE GetModuleHandleA( const char *n ) { (void)n; lookups++; return (HMODULE)1; }
static void *GetProcAddress( HMODULE m, const char *n ) { (void)m; (void)n; return (void *)fake_delay; }
static void Sleep( DWORD ms ) { sleeps++; clk += (LONG64)ms * 10000; }
static void InterlockedExchangeAdd64( LONG64 *p, LONG64 v ) { *p += v; }
static void InterlockedIncrement64( LONG64 *p ) { (*p)++; }
''' + mpace + r'''
int main( int argc, char **argv )
{
    struct mad_swapchain s = { 0 };
    int cap = atoi( argv[1] ), work_ms = atoi( argv[2] ), stall_at = atoi( argv[3] ), i;
    LONG64 first = 0, last = 0;
    g_xp.qpf = 10000000; g_xp.fps_cap = cap;
    for (i = 0; i < 61; i++)
    {
        clk += (LONG64)work_ms * 10000 + (i == stall_at ? 2000000 : 0);   /* the frame's work (and one 200 ms stall) */
        mad_fps_cap_pace( &s, mad_qpc() );
        if (i == 1) first = clk;
        last = clk;
    }
    printf( "%d %d %d %.2f %lld\n", delays, sleeps, lookups, (double)(last - first) / 10000.0 / 59, (long long)g_xp.n_cap );
    return 0;
}
'''
with tempfile.TemporaryDirectory(prefix="madeira-fps-pe-") as tmp:
    exe, err = build(mpace_h, "pe", tmp)
    check(exe is not None, "mad_fps_cap_pace compiles with stand-ins" + ("" if exe else ": " + err[-900:]))
    if exe:
        def mrun(*a):
            r = subprocess.run([str(exe)] + [str(x) for x in a], capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
            f = r.stdout.split()
            return int(f[0]), int(f[1]), int(f[2]), float(f[3]), int(f[4])
        d, sl, lk, ms, nc = mrun(0, 10, -1)
        check(d == 0 and sl == 0 and lk == 0 and abs(ms - 10) < 0.01, "cap 0: nothing waits, nothing is looked up")
        d, sl, lk, ms, nc = mrun(30, 10, -1)
        check(d >= 58 and sl == 0 and lk == 1 and abs(ms - 33.33) < 0.05 and nc == d,
              "cap 30, 10 ms frames: %.2f ms apart, one NtDelayExecution a frame, looked up once" % ms)
        d, sl, lk, ms, nc = mrun(30, 50, -1)
        check(d == 0 and abs(ms - 50) < 0.01, "frames slower than the cap wait for nothing (%.2f ms)" % ms)
        d, sl, lk, ms, nc = mrun(30, 10, 30)
        check(d >= 56 and abs(ms - (33.33 * 58 + 210) / 59) < 1.0, "a 200 ms stall re-bases the deadline, no catch-up burst (%.2f ms)" % ms)

# ---- 4. wiring -------------------------------------------------------------------
present = func_body(d3d, "static HRESULT STDMETHODCALLTYPE swap_Present(IDXGISwapChain4 *T, UINT sync, UINT flags)")
check("if (g_xp.fps_cap && !(flags & DXGI_PRESENT_TEST)) mad_fps_cap_pace((struct mad_swapchain *)T, t1.QuadPart);" in present
      and present.index("mad_fps_cap_pace") > present.index("InterlockedExchangeAdd64(&g_perf_present_ticks"),
      "Present paces after its own timing, never for DXGI_PRESENT_TEST")
mx = re.search(r"static struct mad_xp \{(.*?)\} g_xp;", d3d, re.S).group(1)
mx = re.sub(r"/\*.*?\*/", "", mx, flags=re.S)
pe_fields = re.findall(r"\b([a-z_]+)\b", re.search(r"struct ios_xp_pe \{(.*?)\};", server, re.S).group(1).replace("int64_t", ""))
xp_fields = re.findall(r"\b([a-z_]+)\b", mx.replace("LONG64", "").replace("volatile", ""))
check(xp_fields[:len(pe_fields)] == pe_fields and xp_fields[len(pe_fields):len(pe_fields) + 3] == ["fps_cap", "t_cap", "n_cap"],
      "madeira-d3d12's counter block: ios_xp_pe's %d fields, then fps_cap, t_cap, n_cap (ios_xp_pe_cap)" % len(pe_fields))
check("struct ios_xp_pe_cap { int64_t fps_cap, t_cap, n_cap; };" in server, "ios_xp_pe_cap mirrors them")
xprobe = func_body(server, "static void ios_xprobe_main( void )")
check("ios_xp_pe_len >= sizeof(pe) + sizeof(struct ios_xp_pe_cap)" in xprobe
      and "if (pc && pe.magic == 0x3130305058444d4dLL && ios_fps_gov_armed && pc->fps_cap != ios_fps_cap) pc->fps_cap = ios_fps_cap;" in xprobe,
      "the xprobe writes the cap only into a live block that long (its magic read this sample), and only when armed")
check(xprobe.index("ios_fps_gov_step(") > xprobe.index("ios_power_gov_step(") and
      xprobe.index("ios_fps_gov_step(") < xprobe.index('wine_log_write( "%s", line );'),
      "the governor runs every [xp] sample with that sample's ri_energy_nj")
check('if (ios_xp_pe_block && ios_xp_pe_len >= sizeof(pe)) { memcpy( &pe, (void *)(uintptr_t)ios_xp_pe_block, sizeof(pe) ); }' in xprobe,
      "the [xp] line still reads the same 20 fields")
ci = CI.read_text(encoding="utf-8")
p_wm = ci.find("python3 tools/patch-winemetal-fps-cap.py")
check(p_wm > ci.find("python3 tools/patch-winemetal-gpu-trace.py") > 0, "CI applies the winemetal patch after the GPU trace patch")
if WM.is_file():
    # CI runs this check after every winemetal patch step: start from the file as committed
    _wm_git = subprocess.run(["git", "-C", str(ROOT / "dxmt"), "show", "HEAD:src/winemetal/unix/winemetal_unix.c"],
                             capture_output=True, text=True)
    WM_TEXT = _wm_git.stdout if _wm_git.returncode == 0 and _wm_git.stdout else WM.read_text()
    with tempfile.TemporaryDirectory(prefix="madeira-fps-wm-") as tmp:
        t = Path(tmp)
        (t / "dxmt/src/winemetal/unix").mkdir(parents=True)
        (t / "dxmt/src/winemetal/unix/winemetal_unix.c").write_text(WM_TEXT)
        r1 = subprocess.run([sys.executable, str(PATCH)], cwd=t, capture_output=True, text=True)
        r2 = subprocess.run([sys.executable, str(PATCH)], cwd=t, capture_output=True, text=True)
        out = (t / "dxmt/src/winemetal/unix/winemetal_unix.c").read_text()
        fn = func_body(out, "_MTLCommandBuffer_presentDrawable(void *obj)")
        check(r1.returncode == 0 and "frame cap patched" in r1.stdout and r2.returncode == 0 and "already patched" in r2.stdout,
              "the winemetal patch applies once and is idempotent")
        check(fn.count("ios_fps_cap_dxmt_pace()") == 1 and fn.index("ios_fps_cap_dxmt_pace()") < fn.index("madeira_fg_enabled()")
              and fn.index("ios_fps_cap_dxmt_pace()") > fn.index("int mode = g_madeira_vsync_mode;"),
              "presentDrawable calls ios_fps_cap_dxmt_pace once, after reading the mode, before frame generation")
        (t / "dxmt/src/winemetal/unix/winemetal_unix.c").write_text(WM_TEXT.replace("int mode = g_madeira_vsync_mode;", "int mode = 1;"))
        r3 = subprocess.run([sys.executable, str(PATCH)], cwd=t, capture_output=True, text=True)
        check(r3.returncode != 0 and "anchor" in r3.stderr, "a moved anchor fails the patch by name")
else:
    print("SKIP winemetal patch application: no dxmt sources (check out the submodule)")
gen = GEN.read_text(encoding="utf-8")
cat = CAT.read_text(encoding="utf-8")
for key in ("env.MADEIRA_FPS_CAP", "env.MADEIRA_FPS_GOV_MW"):
    check(('"%s"' % key) in gen and key in cat, "the catalog documents %s" % key)
for key in ("env.MADEIRA_FPS_GOV_BAND_MJ", "env.MADEIRA_FPS_GOV_MIN", "env.MADEIRA_FPS_GOV_MAX"):
    check(key in cat, "the catalog lists %s" % key)
check("- name: Check fps-gov" in ci and "python3 tests/host/check-fps-gov.py" in ci, "CI runs this check")

if failures:
    print("FAILED: %d" % len(failures))
    sys.exit(1)
print("OK")
