#!/usr/bin/env python3
"""Late render-pass store actions for madeira-d3d12 (MadeiraCtl op 24).

On Apple's tile-based GPUs every attachment a render pass stores is a whole
pass over that surface in memory. madeira-d3d12 (madeira.cfg / a game's
config rp-discard = 1) can tell, when a pass ENDS, that a texture's next use
in the submission is a DiscardResource or a whole clear, so its store is moot;
Metal allows exactly that decision when the pass was created with
MTLStoreActionUnknown and the store action is set on the encoder before
endEncoding (setColorStoreAction:atIndex:, setDepthStoreAction:,
setStencilStoreAction:). This adds op 24 to winemetal's runtime-control entry
(_madeira_ctl, winemetal.h's struct madeira_ctl_args):

  24  ptr -> { uint64 encoder, uint8 colour[8], depth, stencil, pad[6] },
      len = its size (24): sets each store action that is not 0xff on the
      render encoder; encoder 0 is a probe. ret = 1.

madeira-d3d12 probes it once and only then creates passes with Unknown store
actions. Local mode only: in remote mode, and in a winemetal without this
patch, op 24 answers 0 and the passes keep their Store actions. The i386
entry does not forward op 24 (it carries a pointer). Native only.
Idempotent; fails by name if an anchor moves or case 24 is taken. Run from the
repository root.
"""
import pathlib
import re
import sys

PATH = pathlib.Path("dxmt/src/winemetal/unix/winemetal_unix.c")
MARKER = "madeira-bcd: late render-pass store actions"

ANCHOR_FN = "static NTSTATUS _madeira_ctl(void *args) {\n"
HELPERS = r'''/* madeira-bcd: late render-pass store actions (tools/patch-winemetal-rp-store.py).
 * MadeiraCtl op 24 from madeira-d3d12 (rp-discard = 1): the store actions of an
 * open render encoder whose pass said MTLStoreActionUnknown, set before
 * endEncoding; 0xff leaves one alone, encoder 0 is a probe. */
struct madeira_rp_store_req { uint64_t enc; uint8_t color[8], depth, stencil, pad[6]; };
static uint32_t madeira_rp_store(const struct madeira_rp_store_req *r) {
  id<MTLRenderCommandEncoder> enc;
  unsigned i;
  if (!r->enc) return 1;
  enc = (id<MTLRenderCommandEncoder>)(uintptr_t)r->enc;
  for (i = 0; i < 8; i++)
    if (r->color[i] != 0xff) [enc setColorStoreAction:(MTLStoreAction)r->color[i] atIndex:i];
  if (r->depth != 0xff) [enc setDepthStoreAction:(MTLStoreAction)r->depth];
  if (r->stencil != 0xff) [enc setStencilStoreAction:(MTLStoreAction)r->stencil];
  return 1;
}

'''
ANCHOR_END = "  default: break;\n  }\n  return STATUS_SUCCESS;\n}\n"
CASE = """  case 24:    /* madeira-bcd: late render-pass store actions (tools/patch-winemetal-rp-store.py) */
    if (!a->ptr || a->len < sizeof(struct madeira_rp_store_req) || wmtr_enabled()) break;
    a->ret = madeira_rp_store((const struct madeira_rp_store_req *)(uintptr_t)a->ptr);
    break;
"""


def main():
    src = PATH.read_text()
    if MARKER in src:
        print("patch-winemetal-rp-store: already applied")
        return 0
    if src.count(ANCHOR_FN) != 1:
        sys.exit("patch-winemetal-rp-store: _madeira_ctl anchor not found once")
    fn_at = src.index(ANCHOR_FN)
    fn_end = src.index("\n}\n", fn_at) + 3
    body = src[fn_at:fn_end]
    if not body.endswith(ANCHOR_END):
        sys.exit("patch-winemetal-rp-store: _madeira_ctl no longer ends with its default case")
    if re.search(r"^\s*case 24:", body, re.M):
        sys.exit("patch-winemetal-rp-store: _madeira_ctl already has a case 24 (pick a free op)")
    body = body[:-len(ANCHOR_END)] + CASE + ANCHOR_END
    src = src[:fn_at] + HELPERS + body + src[fn_end:]
    PATH.write_text(src)
    print("patch-winemetal-rp-store: MadeiraCtl op 24 sets the store actions of a render pass created with Unknown")
    return 0


if __name__ == "__main__":
    sys.exit(main())
