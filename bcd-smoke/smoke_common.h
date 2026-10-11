/* madeira-bcd FORK-ONLY (owner's decision, 2026-10-11): nothing under
 * bcd-smoke/ ever goes into a pull request to upstream (willfaust/madeira).
 *
 * What the D3D9 (32-bit) and D3D11 (64-bit) smoke tests share: the log line,
 * the clock, the window, the per-second frame report, the cube matrices and
 * the pixel checks. Each test draws one large spinning cube in the middle and
 * a grid of small ones behind it for SMOKE_SECONDS (or -seconds N), reads the
 * back buffer twice, and ends with one RESULT line: PASS when the corners show
 * the clear colour, the centre shows the cube, and the second read differs
 * from the first (the frames move).
 *
 * Why 60 s: the app detaches its debugger 20 s after the first present (+2 s),
 * and until then guest code runs about half as fast; a session whose program
 * has already ended at that point counts as a failed launch. The RESULT line
 * therefore also gives the average of the last SMOKE_TAIL seconds, which on
 * the device is the detached rate. */
#pragma once
#include <windows.h>
#include <math.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define SMOKE_W 1280
#define SMOKE_H 720
#define SMOKE_SECONDS 60
#define SMOKE_TAIL 20        /* the RESULT line's "last N s" rate */
#define SMOKE_GRID_X 12     /* the small cubes behind the large one */
#define SMOKE_GRID_Y 6
#define SMOKE_CLEAR_R 26     /* the clear colour (0.1, 0.2, 0.6) as 8-bit UNORM */
#define SMOKE_CLEAR_G 51
#define SMOKE_CLEAR_B 153

static const char *g_smoke_tag = "bcd-smoke";

/* One line in the session log. Under Wine through __wine_dbg_output, which
 * unix ntdll writes straight to the log from any process (a program started by
 * the app has no usable stderr); elsewhere OutputDebugStringA and stderr. The
 * line ends in '\n' and stays under ntdll's 1020-byte buffer. */
static void smoke_log(const char *fmt, ...)
{
    typedef int (__cdecl *dbg_output_fn)(const char *);
    static dbg_output_fn dbg_output;
    static int looked;
    char buf[960];
    size_t n;
    va_list ap;

    n = (size_t)snprintf(buf, sizeof buf, "[%s] ", g_smoke_tag);
    va_start(ap, fmt);
    vsnprintf(buf + n, sizeof buf - n - 1, fmt, ap);
    va_end(ap);
    n = strlen(buf);
    if (!n || buf[n - 1] != '\n') { buf[n] = '\n'; buf[n + 1] = 0; }
    if (!looked) {
        HMODULE nt = GetModuleHandleA("ntdll.dll");
        if (nt) dbg_output = (dbg_output_fn)(void *)GetProcAddress(nt, "__wine_dbg_output");
        looked = 1;
    }
    if (dbg_output) { dbg_output(buf); return; }
    OutputDebugStringA(buf);
    fputs(buf, stderr);
    fflush(stderr);
}

static double smoke_now(void)
{
    static LARGE_INTEGER freq;
    LARGE_INTEGER c;
    if (!freq.QuadPart) QueryPerformanceFrequency(&freq);
    QueryPerformanceCounter(&c);
    return (double)c.QuadPart / (double)freq.QuadPart;
}

/* -seconds N on the command line, else SMOKE_SECONDS (1..600). */
static int smoke_seconds(void)
{
    const char *cmd = GetCommandLineA(), *p = cmd ? strstr(cmd, "-seconds") : NULL;
    int s = p ? atoi(p + 8) : 0;
    return s >= 1 && s <= 600 ? s : SMOKE_SECONDS;
}

/* -dump on the command line: each image check also writes the back buffer it
 * read to bcd-smoke-<api>-<read>.bmp in the current folder. */
static void smoke_dump_bmp(int read, const unsigned char *bits, unsigned pitch)
{
    const char *cmd = GetCommandLineA();
    char name[64];
    BITMAPFILEHEADER fh;
    BITMAPINFOHEADER ih;
    HANDLE h;
    DWORD wr;
    int y;

    if (!cmd || !strstr(cmd, "-dump")) return;
    snprintf(name, sizeof name, "bcd-smoke-%s-%d.bmp", strchr(g_smoke_tag, ' ') ? strchr(g_smoke_tag, ' ') + 1 : "x", read);
    memset(&fh, 0, sizeof fh);
    memset(&ih, 0, sizeof ih);
    fh.bfType = 0x4d42;
    fh.bfOffBits = sizeof fh + sizeof ih;
    fh.bfSize = fh.bfOffBits + SMOKE_W * SMOKE_H * 4;
    ih.biSize = sizeof ih;
    ih.biWidth = SMOKE_W;
    ih.biHeight = -SMOKE_H;   /* top-down rows, as read */
    ih.biPlanes = 1;
    ih.biBitCount = 32;
    ih.biCompression = BI_RGB;
    h = CreateFileA(name, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE) return;
    WriteFile(h, &fh, sizeof fh, &wr, NULL);
    WriteFile(h, &ih, sizeof ih, &wr, NULL);
    for (y = 0; y < SMOKE_H; y++) WriteFile(h, bits + (size_t)y * pitch, SMOKE_W * 4, &wr, NULL);
    CloseHandle(h);
    smoke_log("check %d: image written to %s", read, name);
}

static LRESULT CALLBACK smoke_wndproc(HWND hwnd, UINT msg, WPARAM wp, LPARAM lp)
{
    if (msg == WM_DESTROY) { PostQuitMessage(0); return 0; }
    if (msg == WM_KEYDOWN && wp == VK_ESCAPE) { DestroyWindow(hwnd); return 0; }
    return DefWindowProcA(hwnd, msg, wp, lp);
}

static HWND smoke_window(const char *title)
{
    WNDCLASSA wc;
    RECT r = { 0, 0, SMOKE_W, SMOKE_H };
    memset(&wc, 0, sizeof wc);
    wc.lpfnWndProc = smoke_wndproc;
    wc.hInstance = GetModuleHandleA(NULL);
    wc.hCursor = LoadCursorA(NULL, (LPCSTR)IDC_ARROW);
    wc.lpszClassName = "bcd-smoke";
    RegisterClassA(&wc);
    AdjustWindowRect(&r, WS_OVERLAPPEDWINDOW, FALSE);
    return CreateWindowA("bcd-smoke", title, WS_OVERLAPPEDWINDOW | WS_VISIBLE, CW_USEDEFAULT, CW_USEDEFAULT,
                         r.right - r.left, r.bottom - r.top, NULL, NULL, wc.hInstance, NULL);
}

/* Handle the window's messages; 0 once it was closed. */
static int smoke_pump(void)
{
    MSG msg;
    while (PeekMessageA(&msg, NULL, 0, 0, PM_REMOVE)) {
        if (msg.message == WM_QUIT) return 0;
        TranslateMessage(&msg);
        DispatchMessageA(&msg);
    }
    return 1;
}

/* Frame statistics: one line a second, a summary at the end. */
struct smoke_stats {
    double start, prev, sec_start, worst_ms, sec_worst_ms, min_fps, max_fps, run_seconds, tail_start;
    unsigned frames, sec_frames, seconds, tail_frames0;
    int tail_marked;
};

static void smoke_stats_start(struct smoke_stats *s, int run_seconds)
{
    memset(s, 0, sizeof *s);
    s->start = s->prev = s->sec_start = smoke_now();
    s->min_fps = 1e9;
    s->run_seconds = run_seconds;
}

static void smoke_stats_frame(struct smoke_stats *s, unsigned draws)
{
    double now = smoke_now(), ms = (now - s->prev) * 1000.0;
    s->prev = now;
    s->frames++;
    s->sec_frames++;
    if (!s->tail_marked && now - s->start >= s->run_seconds - SMOKE_TAIL) {
        s->tail_marked = 1;
        s->tail_start = now;
        s->tail_frames0 = s->frames;
    }
    if (ms > s->worst_ms) s->worst_ms = ms;
    if (ms > s->sec_worst_ms) s->sec_worst_ms = ms;
    if (now - s->sec_start >= 1.0) {
        double fps = s->sec_frames / (now - s->sec_start);
        s->seconds++;
        if (fps < s->min_fps) s->min_fps = fps;
        if (fps > s->max_fps) s->max_fps = fps;
        smoke_log("t=%us fps=%.1f worst=%.1fms draws/frame=%u", s->seconds, fps, s->sec_worst_ms, draws);
        s->sec_start = now;
        s->sec_frames = 0;
        s->sec_worst_ms = 0;
    }
}

/* Upstream's DXMT cube maths (dxmt/tests/dx11/3DMaths.h), ported as is so the
 * D3D11 test feeds tests/dxmt/out-x64/cube's shaders exactly what upstream's
 * cube does: m[k] holds the k-th group of four values as the functions list
 * them, which is also the order the shader's constant buffer reads. The same
 * product works as a D3D9 world matrix transposed (smoke_mat_transpose). */
typedef struct { float m[4][4]; } smoke_mat;

static inline smoke_mat smoke_mat_of(const float v[16])
{
    smoke_mat r;
    memcpy(r.m, v, sizeof r.m);
    return r;
}

static inline smoke_mat smoke_mat_mul(smoke_mat a, smoke_mat b)   /* upstream's operator* */
{
    smoke_mat r;
    int i, j;
    for (j = 0; j < 4; j++)
        for (i = 0; i < 4; i++)
            r.m[j][i] = a.m[0][i] * b.m[j][0] + a.m[1][i] * b.m[j][1] + a.m[2][i] * b.m[j][2] + a.m[3][i] * b.m[j][3];
    return r;
}

static inline smoke_mat smoke_mat_transpose(smoke_mat a)
{
    smoke_mat r;
    int i, j;
    for (j = 0; j < 4; j++)
        for (i = 0; i < 4; i++) r.m[j][i] = a.m[i][j];
    return r;
}

static inline smoke_mat smoke_rotate_x(float rad)
{
    float s = sinf(rad), c = cosf(rad);
    const float v[16] = { 1, 0, 0, 0, 0, c, -s, 0, 0, s, c, 0, 0, 0, 0, 1 };
    return smoke_mat_of(v);
}

static inline smoke_mat smoke_rotate_y(float rad)
{
    float s = sinf(rad), c = cosf(rad);
    const float v[16] = { c, 0, s, 0, 0, 1, 0, 0, -s, 0, c, 0, 0, 0, 0, 1 };
    return smoke_mat_of(v);
}

static inline smoke_mat smoke_translate(float x, float y, float z)
{
    const float v[16] = { 1, 0, 0, x, 0, 1, 0, y, 0, 0, 1, z, 0, 0, 0, 1 };
    return smoke_mat_of(v);
}

static inline smoke_mat smoke_scale(float k)
{
    const float v[16] = { k, 0, 0, 0, 0, k, 0, 0, 0, 0, k, 0, 0, 0, 0, 1 };
    return smoke_mat_of(v);
}

static inline smoke_mat smoke_perspective(float aspect, float fov_y, float z_near, float z_far)
{
    float ys = tanf(0.5f * (3.14159265f - fov_y)), xs = ys / aspect, inv = 1.f / (z_near - z_far);
    const float v[16] = { xs, 0, 0, 0, 0, ys, 0, 0, 0, 0, z_far * inv, z_far * z_near * inv, 0, 0, -1, 0 };
    return smoke_mat_of(v);
}

/* Model-view-projection of cube `i`: i = 0 is the large cube at the origin,
 * 1..SMOKE_GRID_X*SMOKE_GRID_Y the small ones on a plane behind it. The camera
 * sits at z = 2 looking down -z, as in upstream's cube. */
static inline smoke_mat smoke_cube_mvp(unsigned i, double t)
{
    smoke_mat proj = smoke_perspective((float)SMOKE_W / SMOKE_H, 84.f * 3.14159265f / 180.f, 0.1f, 1000.f);
    smoke_mat view = smoke_translate(0, 0, -2);
    smoke_mat model;
    float a = (float)t;
    if (!i) {
        model = smoke_mat_mul(smoke_mat_mul(smoke_scale(1.2f), smoke_rotate_x(-0.6f * a)), smoke_rotate_y(0.4f * a));
    } else {
        unsigned gx = (i - 1) % SMOKE_GRID_X, gy = (i - 1) / SMOKE_GRID_X;
        float x = -6.f + 12.f * gx / (SMOKE_GRID_X - 1), y = -3.4f + 6.8f * gy / (SMOKE_GRID_Y - 1);
        model = smoke_mat_mul(smoke_mat_mul(smoke_scale(0.45f), smoke_rotate_y(a + 0.3f * i)), smoke_translate(x, y, -3.f));
    }
    return smoke_mat_mul(smoke_mat_mul(model, view), proj);
}

/* Pixel checks on an 8-bit BGRA or BGRX image. */
static int smoke_px_near(const unsigned char *bgra, int r, int g, int b, int tol)
{
    return abs(bgra[2] - r) <= tol && abs(bgra[1] - g) <= tol && abs(bgra[0] - b) <= tol;
}

/* A sum over the centre 128x128 that changes when the cube turns. */
static unsigned long long smoke_centre_sum(const unsigned char *bits, unsigned pitch)
{
    unsigned long long sum = 0;
    unsigned x, y;
    for (y = SMOKE_H / 2 - 64; y < SMOKE_H / 2 + 64; y++)
        for (x = SMOKE_W / 2 - 64; x < SMOKE_W / 2 + 64; x++) {
            const unsigned char *p = bits + (size_t)y * pitch + (size_t)x * 4;
            sum += (unsigned long long)(p[0] + 3 * p[1] + 7 * p[2]) * (x * 31u + y * 17u + 1u);
        }
    return sum;
}

/* Inspect one read of the back buffer. read 0 records the centre sum, read 1
 * compares with it. Returns 1 when this read passes. */
static int smoke_check_image(int read, const unsigned char *bits, unsigned pitch, unsigned long long *first_sum)
{
    static const int corners[4][2] = { { 8, 8 }, { SMOKE_W - 9, 8 }, { 8, SMOKE_H - 9 }, { SMOKE_W - 9, SMOKE_H - 9 } };
    const unsigned char *c = bits + (size_t)(SMOKE_H / 2) * pitch + (size_t)(SMOKE_W / 2) * 4;
    unsigned long long sum = smoke_centre_sum(bits, pitch);
    int ok = 1, k;

    smoke_dump_bmp(read, bits, pitch);
    for (k = 0; k < 4; k++) {
        const unsigned char *p = bits + (size_t)corners[k][1] * pitch + (size_t)corners[k][0] * 4;
        if (!smoke_px_near(p, SMOKE_CLEAR_R, SMOKE_CLEAR_G, SMOKE_CLEAR_B, 3)) {
            smoke_log("check %d: corner %d,%d is %u,%u,%u, not the clear colour %u,%u,%u", read, corners[k][0],
                      corners[k][1], p[2], p[1], p[0], SMOKE_CLEAR_R, SMOKE_CLEAR_G, SMOKE_CLEAR_B);
            ok = 0;
        }
    }
    if (smoke_px_near(c, SMOKE_CLEAR_R, SMOKE_CLEAR_G, SMOKE_CLEAR_B, 3)) {
        smoke_log("check %d: the centre shows the clear colour: the cube was not drawn", read);
        ok = 0;
    }
    if (read == 0) *first_sum = sum;
    else if (sum == *first_sum) {
        smoke_log("check %d: the centre did not change since the first read: the frames do not move", read);
        ok = 0;
    }
    smoke_log("check %d: centre %u,%u,%u, centre sum %llu: %s", read, c[2], c[1], c[0], sum, ok ? "ok" : "FAILED");
    return ok;
}

static void smoke_summary(const char *api, const struct smoke_stats *s, int checks_ok, int checks_run, const char *why)
{
    double now = smoke_now(), total = now - s->start, tail = s->tail_marked ? now - s->tail_start : 0;
    int pass = !why && checks_run == 2 && checks_ok == 2;
    smoke_log("RESULT %s %s: %u frames in %.1f s, average %.1f fps, last %.0f s %.1f fps, per-second %.1f..%.1f fps, "
              "worst frame %.1f ms, image checks %d/%d%s%s",
              api, pass ? "PASS" : "FAIL", s->frames, total, total > 0 ? s->frames / total : 0.0, tail,
              tail > 0 ? (s->frames - s->tail_frames0) / tail : 0.0, s->seconds ? s->min_fps : 0.0, s->max_fps,
              s->worst_ms, checks_ok, checks_run, why ? "; " : "", why ? why : "");
}
