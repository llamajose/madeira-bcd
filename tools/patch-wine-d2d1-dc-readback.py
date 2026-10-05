#!/usr/bin/env python3
"""Let Wine's ID2D1DCRenderTarget work without IDXGISurface1::GetDC.

BindDC creates a GDI-compatible target bitmap and asks its D3D11 texture for
IDXGISurface1, whose GetDC/ReleaseDC carry the pixels between the HDC and the
texture. DXMT's textures do not implement IDXGISurface1 ("D3D11Resource(tex2d):
Unknown interface query 4ae63092-..."), so dxgi_surface stayed NULL and
d2d_dc_render_target_present called GetDC through it: an AV reading address 0
(d2d1 rva 0x1f620). The Rockstar Games Launcher's crash handler turns that
into "Exception: 0xc0000005" and closes the launcher (2026-10-04 07:18 log,
right after "[folderfix] Requesting fix of non-admin folder locations").

Without a surface, BeginDraw now copies the HDC's pixels into the target
texture through a DIB section and UpdateSubresource, and Present reads the
texture back through a staging copy and draws it with StretchDIBits; that is
what GetDC/BitBlt did. With a surface nothing changes. Logged once:
`[d2d-dc] madeira-bcd: ...`. Built only into the d2d1.dll
tools/build-wine-extra-dlls.sh builds.

Usage: patch-wine-d2d1-dc-readback.py wine/dlls/d2d1/dc_render_target.c
Idempotent; fails by name if an anchor moved.
"""
import sys

path = sys.argv[1]
src = open(path).read()
if "madeira_dc_target_texture" in src:
    print("already patched"); sys.exit(0)

helpers = r'''/* madeira-bcd: see tools/patch-wine-d2d1-dc-readback.py. */
static ID3D11Texture2D *madeira_dc_target_texture(struct d2d_dc_render_target *render_target)
{
    ID3D11Texture2D *texture = NULL;
    ID2D1DeviceContext *context;
    struct d2d_bitmap *bitmap;
    ID2D1Image *image = NULL;
    static LONG said;

    if (FAILED(ID2D1RenderTarget_QueryInterface(render_target->dxgi_target,
            &IID_ID2D1DeviceContext, (void **)&context)))
        return NULL;
    ID2D1DeviceContext_GetTarget(context, &image);
    ID2D1DeviceContext_Release(context);
    if (!image)
        return NULL;
    bitmap = unsafe_impl_from_ID2D1Bitmap((ID2D1Bitmap *)image);
    ID3D11Resource_QueryInterface(bitmap->resource, &IID_ID3D11Texture2D, (void **)&texture);
    ID2D1Image_Release(image);
    if (texture && !InterlockedExchange(&said, 1))
        ERR("[d2d-dc] madeira-bcd: target texture has no IDXGISurface1, copying through a DIB and a staging read-back\n");
    return texture;
}

static void madeira_dc_dib_info(BITMAPINFO *info, LONG width, LONG height)
{
    memset(info, 0, sizeof(*info));
    info->bmiHeader.biSize = sizeof(info->bmiHeader);
    info->bmiHeader.biWidth = width;
    info->bmiHeader.biHeight = -height;
    info->bmiHeader.biPlanes = 1;
    info->bmiHeader.biBitCount = 32;
    info->bmiHeader.biCompression = BI_RGB;
}

/* GetDC(TRUE) + BitBlt from the bound HDC, without a DXGI surface. */
static void madeira_dc_upload(struct d2d_dc_render_target *render_target)
{
    const RECT *dst_rect = &render_target->dst_rect;
    ID3D11DeviceContext *context;
    D3D11_TEXTURE2D_DESC desc;
    ID3D11Texture2D *texture;
    ID3D11Device *device;
    HBITMAP dib, old;
    BITMAPINFO info;
    void *bits;
    HDC mem;

    if (!(texture = madeira_dc_target_texture(render_target)))
        return;
    ID3D11Texture2D_GetDesc(texture, &desc);
    madeira_dc_dib_info(&info, desc.Width, desc.Height);
    if ((mem = CreateCompatibleDC(render_target->hdc)))
    {
        if ((dib = CreateDIBSection(mem, &info, DIB_RGB_COLORS, &bits, NULL, 0)))
        {
            old = SelectObject(mem, dib);
            BitBlt(mem, 0, 0, desc.Width, desc.Height, render_target->hdc,
                    dst_rect->left, dst_rect->top, SRCCOPY);
            GdiFlush();
            ID3D11Texture2D_GetDevice(texture, &device);
            ID3D11Device_GetImmediateContext(device, &context);
            ID3D11DeviceContext_UpdateSubresource(context, (ID3D11Resource *)texture, 0, NULL,
                    bits, desc.Width * 4, 0);
            ID3D11DeviceContext_Release(context);
            ID3D11Device_Release(device);
            SelectObject(mem, old);
            DeleteObject(dib);
        }
        DeleteDC(mem);
    }
    ID3D11Texture2D_Release(texture);
}

/* GetDC(FALSE) + BitBlt to the bound HDC, without a DXGI surface. */
static void madeira_dc_readback(struct d2d_dc_render_target *render_target)
{
    const RECT *dst_rect = &render_target->dst_rect;
    ID3D11Texture2D *texture, *staging;
    D3D11_MAPPED_SUBRESOURCE map;
    ID3D11DeviceContext *context;
    D3D11_TEXTURE2D_DESC desc;
    ID3D11Device *device;
    BITMAPINFO info;

    if (!(texture = madeira_dc_target_texture(render_target)))
        return;
    ID3D11Texture2D_GetDesc(texture, &desc);
    desc.MipLevels = 1;
    desc.ArraySize = 1;
    desc.Usage = D3D11_USAGE_STAGING;
    desc.BindFlags = 0;
    desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ;
    desc.MiscFlags = 0;
    ID3D11Texture2D_GetDevice(texture, &device);
    if (SUCCEEDED(ID3D11Device_CreateTexture2D(device, &desc, NULL, &staging)))
    {
        ID3D11Device_GetImmediateContext(device, &context);
        ID3D11DeviceContext_CopySubresourceRegion(context, (ID3D11Resource *)staging, 0, 0, 0, 0,
                (ID3D11Resource *)texture, 0, NULL);
        if (SUCCEEDED(ID3D11DeviceContext_Map(context, (ID3D11Resource *)staging, 0, D3D11_MAP_READ, 0, &map)))
        {
            madeira_dc_dib_info(&info, map.RowPitch / 4, desc.Height);
            StretchDIBits(render_target->hdc, dst_rect->left, dst_rect->top, desc.Width, desc.Height,
                    0, 0, desc.Width, desc.Height, map.pData, &info, DIB_RGB_COLORS, SRCCOPY);
            ID3D11DeviceContext_Unmap(context, (ID3D11Resource *)staging, 0);
        }
        ID3D11DeviceContext_Release(context);
        ID3D11Texture2D_Release(staging);
    }
    ID3D11Device_Release(device);
    ID3D11Texture2D_Release(texture);
}

'''

anchor_fn = "static HRESULT d2d_dc_render_target_present(IUnknown *outer_unknown)\n"

old_present = '''    if (!render_target->hdc)
        return D2DERR_WRONG_STATE;

    if (FAILED(hr = IDXGISurface1_GetDC(render_target->dxgi_surface, FALSE, &src_hdc)))
'''
new_present = '''    if (!render_target->hdc)
        return D2DERR_WRONG_STATE;

    if (!render_target->dxgi_surface)
    {
        madeira_dc_readback(render_target);
        return S_OK;
    }

    if (FAILED(hr = IDXGISurface1_GetDC(render_target->dxgi_surface, FALSE, &src_hdc)))
'''

old_begin = '''    TRACE("iface %p.\\n", iface);

    if (render_target->dxgi_surface)
    {
        if (SUCCEEDED(IDXGISurface1_GetDC(render_target->dxgi_surface, TRUE, &hdc)))
'''
new_begin = '''    TRACE("iface %p.\\n", iface);

    if (!render_target->dxgi_surface && render_target->hdc)
        madeira_dc_upload(render_target);
    else if (render_target->dxgi_surface)
    {
        if (SUCCEEDED(IDXGISurface1_GetDC(render_target->dxgi_surface, TRUE, &hdc)))
'''

for name, a in (("present", anchor_fn), ("present body", old_present), ("BeginDraw", old_begin)):
    if src.count(a) != 1:
        print(f"::error::patch-wine-d2d1-dc-readback: {name} anchor not found exactly once -- dc_render_target.c changed")
        sys.exit(1)
src = src.replace(anchor_fn, helpers + anchor_fn, 1)
src = src.replace(old_present, new_present, 1)
src = src.replace(old_begin, new_begin, 1)
open(path, "w").write(src)
print("patched")
