/* madeira-bcd FORK-ONLY (owner's decision, 2026-10-11): never part of a pull
 * request to upstream (willfaust/madeira).
 *
 * bcd-smoke-d3d9.exe, built for i386: a 32-bit program, so on the device it
 * runs through WoW64 and FEX's 32-bit module (xtajit.dll) into DXMT's D3D9
 * frontend (the i386 d3d9.dll shim and d3d9-emulated.dll, or the native one
 * with madeira.cfg d3d9 = native). Fixed-function only, so it needs no shader
 * compiler: per-vertex colours, a texture uploaded through LockRect,
 * MODULATE, a depth buffer, one world matrix per draw (73 draws a frame). */
#define COBJMACROS
#include "smoke_common.h"
#include <d3d9.h>

struct smoke_vertex { float x, y, z; DWORD colour; float u, v; };
#define SMOKE_FVF (D3DFVF_XYZ | D3DFVF_DIFFUSE | D3DFVF_TEX1)

/* 24 vertices, four a face (own colour and texture corners), 36 indices. */
static void smoke_cube(struct smoke_vertex *v, WORD *idx)
{
    static const float faces[6][4][3] = {
        { { .5f, -.5f, -.5f }, { .5f, .5f, -.5f }, { .5f, .5f, .5f }, { .5f, -.5f, .5f } },
        { { -.5f, -.5f, .5f }, { -.5f, .5f, .5f }, { -.5f, .5f, -.5f }, { -.5f, -.5f, -.5f } },
        { { -.5f, .5f, -.5f }, { -.5f, .5f, .5f }, { .5f, .5f, .5f }, { .5f, .5f, -.5f } },
        { { -.5f, -.5f, .5f }, { -.5f, -.5f, -.5f }, { .5f, -.5f, -.5f }, { .5f, -.5f, .5f } },
        { { .5f, -.5f, .5f }, { .5f, .5f, .5f }, { -.5f, .5f, .5f }, { -.5f, -.5f, .5f } },
        { { -.5f, -.5f, -.5f }, { -.5f, .5f, -.5f }, { .5f, .5f, -.5f }, { .5f, -.5f, -.5f } },
    };
    static const DWORD colours[6] = { 0xFFFF6060, 0xFF60FF60, 0xFF6060FF, 0xFFFFFF60, 0xFFFF60FF, 0xFF60FFFF };
    static const float uv[4][2] = { { 0, 1 }, { 0, 0 }, { 1, 0 }, { 1, 1 } };
    int f, k;
    for (f = 0; f < 6; f++) {
        for (k = 0; k < 4; k++) {
            struct smoke_vertex *p = &v[f * 4 + k];
            p->x = faces[f][k][0]; p->y = faces[f][k][1]; p->z = faces[f][k][2];
            p->colour = colours[f];
            p->u = uv[k][0]; p->v = uv[k][1];
        }
        idx[f * 6 + 0] = (WORD)(f * 4 + 0); idx[f * 6 + 1] = (WORD)(f * 4 + 1); idx[f * 6 + 2] = (WORD)(f * 4 + 2);
        idx[f * 6 + 3] = (WORD)(f * 4 + 0); idx[f * 6 + 4] = (WORD)(f * 4 + 2); idx[f * 6 + 5] = (WORD)(f * 4 + 3);
    }
}

/* Read the back buffer into system memory and check it. */
static int smoke_d3d9_read(IDirect3DDevice9 *dev, int read, unsigned long long *first_sum)
{
    IDirect3DSurface9 *bb = NULL, *sys = NULL;
    D3DLOCKED_RECT lr;
    HRESULT hr;
    int ok = 0;

    hr = IDirect3DDevice9_GetBackBuffer(dev, 0, 0, D3DBACKBUFFER_TYPE_MONO, &bb);
    if (SUCCEEDED(hr))
        hr = IDirect3DDevice9_CreateOffscreenPlainSurface(dev, SMOKE_W, SMOKE_H, D3DFMT_X8R8G8B8, D3DPOOL_SYSTEMMEM, &sys, NULL);
    if (SUCCEEDED(hr)) hr = IDirect3DDevice9_GetRenderTargetData(dev, bb, sys);
    if (SUCCEEDED(hr)) hr = IDirect3DSurface9_LockRect(sys, &lr, NULL, D3DLOCK_READONLY);
    if (SUCCEEDED(hr)) {
        ok = smoke_check_image(read, (const unsigned char *)lr.pBits, (unsigned)lr.Pitch, first_sum);
        IDirect3DSurface9_UnlockRect(sys);
    } else {
        smoke_log("check %d: reading the back buffer failed, hr %#lx", read, (unsigned long)hr);
    }
    if (sys) IDirect3DSurface9_Release(sys);
    if (bb) IDirect3DSurface9_Release(bb);
    return ok;
}

int WINAPI WinMain(HINSTANCE inst, HINSTANCE prev, LPSTR cmd, int show)
{
    IDirect3D9 *d3d;
    IDirect3DDevice9 *dev = NULL;
    IDirect3DVertexBuffer9 *vb = NULL;
    IDirect3DIndexBuffer9 *ib = NULL;
    IDirect3DTexture9 *tex = NULL;
    D3DPRESENT_PARAMETERS pp;
    D3DADAPTER_IDENTIFIER9 id;
    struct smoke_stats st;
    struct smoke_vertex verts[24];
    WORD idx[36];
    unsigned long long first_sum = 0;
    const unsigned draws = 1 + SMOKE_GRID_X * SMOKE_GRID_Y;
    const int seconds = smoke_seconds();
    const char *why = NULL;
    int checks_ok = 0, checks_run = 0;
    double t0;
    HWND hwnd;
    HRESULT hr;
    void *p;

    (void)inst; (void)prev; (void)cmd; (void)show;
    g_smoke_tag = "bcd-smoke d3d9";
    smoke_log("start: %u-bit, %d s, %dx%d, %u draws a frame, fixed-function (texture x vertex colour, depth)",
              (unsigned)(sizeof(void *) * 8), seconds, SMOKE_W, SMOKE_H, draws);
    hwnd = smoke_window("Madeira D3D9 smoke test");
    if (!hwnd) { smoke_log("RESULT d3d9 FAIL: no window"); return 2; }

    d3d = Direct3DCreate9(D3D_SDK_VERSION);
    if (!d3d) { smoke_log("RESULT d3d9 FAIL: Direct3DCreate9 returned NULL"); return 2; }
    memset(&id, 0, sizeof id);
    if (SUCCEEDED(IDirect3D9_GetAdapterIdentifier(d3d, D3DADAPTER_DEFAULT, 0, &id)))
        smoke_log("adapter: \"%s\", driver \"%s\", vendor %#lx device %#lx", id.Description, id.Driver,
                  (unsigned long)id.VendorId, (unsigned long)id.DeviceId);

    memset(&pp, 0, sizeof pp);
    pp.BackBufferWidth = SMOKE_W;
    pp.BackBufferHeight = SMOKE_H;
    pp.BackBufferFormat = D3DFMT_X8R8G8B8;
    pp.BackBufferCount = 1;
    pp.SwapEffect = D3DSWAPEFFECT_DISCARD;
    pp.hDeviceWindow = hwnd;
    pp.Windowed = TRUE;
    pp.EnableAutoDepthStencil = TRUE;
    pp.AutoDepthStencilFormat = D3DFMT_D24S8;
    pp.PresentationInterval = D3DPRESENT_INTERVAL_IMMEDIATE;
    hr = IDirect3D9_CreateDevice(d3d, D3DADAPTER_DEFAULT, D3DDEVTYPE_HAL, hwnd, D3DCREATE_HARDWARE_VERTEXPROCESSING, &pp, &dev);
    if (FAILED(hr)) {
        smoke_log("CreateDevice with hardware vertex processing: hr %#lx; trying software", (unsigned long)hr);
        hr = IDirect3D9_CreateDevice(d3d, D3DADAPTER_DEFAULT, D3DDEVTYPE_HAL, hwnd, D3DCREATE_SOFTWARE_VERTEXPROCESSING, &pp, &dev);
    }
    if (FAILED(hr)) { smoke_log("RESULT d3d9 FAIL: CreateDevice hr %#lx", (unsigned long)hr); return 2; }

    smoke_cube(verts, idx);
    hr = IDirect3DDevice9_CreateVertexBuffer(dev, sizeof verts, D3DUSAGE_WRITEONLY, SMOKE_FVF, D3DPOOL_MANAGED, &vb, NULL);
    if (SUCCEEDED(hr) && SUCCEEDED(hr = IDirect3DVertexBuffer9_Lock(vb, 0, 0, &p, 0))) {
        memcpy(p, verts, sizeof verts);
        IDirect3DVertexBuffer9_Unlock(vb);
        hr = IDirect3DDevice9_CreateIndexBuffer(dev, sizeof idx, D3DUSAGE_WRITEONLY, D3DFMT_INDEX16, D3DPOOL_MANAGED, &ib, NULL);
    }
    if (SUCCEEDED(hr) && SUCCEEDED(hr = IDirect3DIndexBuffer9_Lock(ib, 0, 0, &p, 0))) {
        memcpy(p, idx, sizeof idx);
        IDirect3DIndexBuffer9_Unlock(ib);
        hr = IDirect3DDevice9_CreateTexture(dev, 64, 64, 1, 0, D3DFMT_A8R8G8B8, D3DPOOL_MANAGED, &tex, NULL);
    }
    if (SUCCEEDED(hr)) {
        D3DLOCKED_RECT lr;
        if (SUCCEEDED(hr = IDirect3DTexture9_LockRect(tex, 0, &lr, NULL, 0))) {
            int x, y;
            for (y = 0; y < 64; y++)
                for (x = 0; x < 64; x++)   /* white and light grey, 8x8 cells */
                    ((DWORD *)((BYTE *)lr.pBits + y * lr.Pitch))[x] = ((x / 8 + y / 8) & 1) ? 0xFFFFFFFF : 0xFFB0B0B0;
            IDirect3DTexture9_UnlockRect(tex, 0);
        }
    }
    if (FAILED(hr)) { smoke_log("RESULT d3d9 FAIL: creating the cube's buffers or texture, hr %#lx", (unsigned long)hr); return 2; }

    IDirect3DDevice9_SetRenderState(dev, D3DRS_LIGHTING, FALSE);
    IDirect3DDevice9_SetRenderState(dev, D3DRS_ZENABLE, D3DZB_TRUE);
    IDirect3DDevice9_SetRenderState(dev, D3DRS_CULLMODE, D3DCULL_NONE);
    IDirect3DDevice9_SetTexture(dev, 0, (IDirect3DBaseTexture9 *)tex);
    IDirect3DDevice9_SetTextureStageState(dev, 0, D3DTSS_COLOROP, D3DTOP_MODULATE);
    IDirect3DDevice9_SetTextureStageState(dev, 0, D3DTSS_COLORARG1, D3DTA_TEXTURE);
    IDirect3DDevice9_SetTextureStageState(dev, 0, D3DTSS_COLORARG2, D3DTA_DIFFUSE);
    IDirect3DDevice9_SetSamplerState(dev, 0, D3DSAMP_MINFILTER, D3DTEXF_LINEAR);
    IDirect3DDevice9_SetSamplerState(dev, 0, D3DSAMP_MAGFILTER, D3DTEXF_LINEAR);
    IDirect3DDevice9_SetFVF(dev, SMOKE_FVF);
    IDirect3DDevice9_SetStreamSource(dev, 0, vb, 0, sizeof(struct smoke_vertex));
    IDirect3DDevice9_SetIndices(dev, ib);
    {   /* the cube's matrix carries the whole transform (see smoke_cube_mvp) */
        D3DMATRIX ident;
        memset(&ident, 0, sizeof ident);
        ident._11 = ident._22 = ident._33 = ident._44 = 1.f;
        IDirect3DDevice9_SetTransform(dev, D3DTS_VIEW, &ident);
        IDirect3DDevice9_SetTransform(dev, D3DTS_PROJECTION, &ident);
    }

    smoke_stats_start(&st, seconds);
    t0 = st.start;
    for (;;) {
        double t = smoke_now() - t0;
        unsigned i;
        if (!smoke_pump()) { why = "the window was closed"; break; }
        if (t >= seconds) break;
        IDirect3DDevice9_Clear(dev, 0, NULL, D3DCLEAR_TARGET | D3DCLEAR_ZBUFFER,
                               D3DCOLOR_XRGB(SMOKE_CLEAR_R, SMOKE_CLEAR_G, SMOKE_CLEAR_B), 1.f, 0);
        if (FAILED(hr = IDirect3DDevice9_BeginScene(dev))) { why = "BeginScene failed"; break; }
        for (i = 0; i < draws; i++) {
            smoke_mat world = smoke_mat_transpose(smoke_cube_mvp(i, t));
            IDirect3DDevice9_SetTransform(dev, D3DTS_WORLD, (const D3DMATRIX *)world.m);
            IDirect3DDevice9_DrawIndexedPrimitive(dev, D3DPT_TRIANGLELIST, 0, 0, 24, 0, 12);
        }
        IDirect3DDevice9_EndScene(dev);
        if ((checks_run == 0 && t >= 1.0) || (checks_run == 1 && t >= 3.0)) {
            double r0 = smoke_now();
            checks_ok += smoke_d3d9_read(dev, checks_run, &first_sum);
            checks_run++;
            st.prev += smoke_now() - r0;   /* the read is not a frame's time */
        }
        hr = IDirect3DDevice9_Present(dev, NULL, NULL, NULL, NULL);
        if (FAILED(hr)) { smoke_log("Present: hr %#lx", (unsigned long)hr); why = "Present failed"; break; }
        smoke_stats_frame(&st, draws);
    }
    if (!why && checks_run < 2) why = "the run ended before both image checks";
    smoke_summary("d3d9", &st, checks_ok, checks_run, why);

    IDirect3DTexture9_Release(tex);
    IDirect3DIndexBuffer9_Release(ib);
    IDirect3DVertexBuffer9_Release(vb);
    IDirect3DDevice9_Release(dev);
    IDirect3D9_Release(d3d);
    DestroyWindow(hwnd);
    return why || checks_ok != 2 ? 1 : 0;
}
