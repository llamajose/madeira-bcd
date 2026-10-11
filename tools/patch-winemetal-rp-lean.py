#!/usr/bin/env python3
"""Fill only the colour attachments a render pass uses (madeira.cfg rp-lean).

winemetal's _MTLCommandBuffer_renderCommandEncoder filled all eight colour
attachments of a fresh MTLRenderPassDescriptor for every render pass: ten
property sets each, every one through descriptor.colorAttachments[i] (two more
messages), and the first touch of a slot allocates its attachment descriptor,
freed again with the pass descriptor: ~240 Objective-C messages and eight
allocations and frees per pass, while a pass binds one to five targets. Horizon
Zero Dawn opens ~250-300 encoders a frame at 22-27 us each for open + end
(build 493, [perf] encode split), D3D11 games through DXMT as many as they draw
passes.

Metal does not use a colour attachment whose texture is nil, whatever its other
properties say, and a fresh descriptor's slot has no texture. A slot with
neither a texture nor a resolve texture is therefore left as the descriptor
made it, and a used slot's descriptor is looked up once and filled with the
same ten values as before. Depth, stencil and the pass-wide fields are set as
before, so Metal is handed the same pass. On by default; madeira.cfg / a game's
config rp-lean = 0 fills all eight slots again (read once per process).
Local mode only (the remote path sends the whole structure, unchanged).
Idempotent; fails by name if the anchor moves. Run from the repository root.
"""
import pathlib
import sys

PATH = pathlib.Path("dxmt/src/winemetal/unix/winemetal_unix.c")
MARKER = "madeira-bcd: rp-lean"

ANCHOR_FN = "static NTSTATUS\n_MTLCommandBuffer_renderCommandEncoder(void *obj) {\n"
HELPER = """/* madeira-bcd: rp-lean (tools/patch-winemetal-rp-lean.py). A render pass fills
 * only the colour attachments it uses: a slot with no texture and no resolve
 * texture is not part of the pass in Metal, and a fresh descriptor's slot has
 * none. madeira.cfg rp-lean = 0 fills all eight as before. */
static int madeira_rp_lean_on(void) {
  static int on = -1;
  if (on < 0) on = madeira_cfg_int("rp-lean", 1) ? 1 : 0;
  return on;
}

"""

LOOP_OLD = """  MTLRenderPassDescriptor *descriptor = [[MTLRenderPassDescriptor alloc] init];
  for (unsigned i = 0; i < 8; i++) {
    descriptor.colorAttachments[i].clearColor = MTLClearColorMake(
"""
LOOP_NEW = """  MTLRenderPassDescriptor *descriptor = [[MTLRenderPassDescriptor alloc] init];
  const int madeira_lean = madeira_rp_lean_on();   /* madeira-bcd: rp-lean */
  for (unsigned i = 0; i < 8; i++) {
    if (madeira_lean) {   /* madeira-bcd: rp-lean -- the slots the pass uses, one lookup each */
      if (!info->colors[i].texture && !info->colors[i].resolve_texture)
        continue;
      MTLRenderPassColorAttachmentDescriptor *madeira_att = descriptor.colorAttachments[i];
      madeira_att.clearColor = MTLClearColorMake(
          info->colors[i].clear_color.r, info->colors[i].clear_color.g, info->colors[i].clear_color.b,
          info->colors[i].clear_color.a
      );
      madeira_att.level = info->colors[i].level;
      madeira_att.slice = info->colors[i].slice;
      madeira_att.depthPlane = info->colors[i].depth_plane;
      madeira_att.texture = (id<MTLTexture>)info->colors[i].texture;
      madeira_att.loadAction = (MTLLoadAction)info->colors[i].load_action;
      madeira_att.storeAction = (MTLStoreAction)info->colors[i].store_action;
      madeira_att.resolveTexture = (id<MTLTexture>)info->colors[i].resolve_texture;
      madeira_att.resolveLevel = info->colors[i].resolve_level;
      madeira_att.resolveSlice = info->colors[i].resolve_slice;
      madeira_att.resolveDepthPlane = info->colors[i].resolve_depth_plane;
      continue;
    }
    descriptor.colorAttachments[i].clearColor = MTLClearColorMake(
"""

# The full loop as it stands, so a change to any of its ten sets is noticed
# (the lean branch must set exactly what the loop sets).
FULL_LOOP = """    descriptor.colorAttachments[i].clearColor = MTLClearColorMake(
        info->colors[i].clear_color.r, info->colors[i].clear_color.g, info->colors[i].clear_color.b,
        info->colors[i].clear_color.a
    );
    descriptor.colorAttachments[i].level = info->colors[i].level;
    descriptor.colorAttachments[i].slice = info->colors[i].slice;
    descriptor.colorAttachments[i].depthPlane = info->colors[i].depth_plane;
    descriptor.colorAttachments[i].texture = (id<MTLTexture>)info->colors[i].texture;
    descriptor.colorAttachments[i].loadAction = (MTLLoadAction)info->colors[i].load_action;
    descriptor.colorAttachments[i].storeAction = (MTLStoreAction)info->colors[i].store_action;
    descriptor.colorAttachments[i].resolveTexture = (id<MTLTexture>)info->colors[i].resolve_texture;
    descriptor.colorAttachments[i].resolveLevel = info->colors[i].resolve_level;
    descriptor.colorAttachments[i].resolveSlice = info->colors[i].resolve_slice;
    descriptor.colorAttachments[i].resolveDepthPlane = info->colors[i].resolve_depth_plane;
  }
"""


def main():
    src = PATH.read_text()
    if MARKER in src:
        print(f"{PATH}: rp-lean already patched")
        return 0
    for name, old in (("_MTLCommandBuffer_renderCommandEncoder", ANCHOR_FN), ("colour attachment loop", LOOP_OLD),
                      ("colour attachment sets", FULL_LOOP)):
        if src.count(old) != 1:
            print(f"::error::{PATH}: {name} anchor not found once -- dxmt moved, review tools/patch-winemetal-rp-lean.py")
            return 1
    fn = src.index(ANCHOR_FN)
    loop = src.index(LOOP_OLD)
    if not fn < loop < src.index(FULL_LOOP) < src.index("renderCommandEncoderWithDescriptor:descriptor", fn):
        print(f"::error::{PATH}: the colour attachment loop is no longer inside _MTLCommandBuffer_renderCommandEncoder")
        return 1
    src = src.replace(LOOP_OLD, LOOP_NEW)
    src = src.replace(ANCHOR_FN, HELPER + ANCHOR_FN)
    PATH.write_text(src)
    print(f"{PATH}: rp-lean patched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
