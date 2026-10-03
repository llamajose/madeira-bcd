#!/usr/bin/env python3
"""Implement ID3D11DeviceContext1::SwapDeviceContextState in DXMT.

Wine's d2d1 draws through the caller's D3D11 immediate context and brackets
every draw with SwapDeviceContextState, so its state never leaks into the
application's. DXMT's method was IMPLEMENT_ME, which aborts: the Rockstar
Games Launcher (Direct2D on the D3D11 device) exited with code 3 right after
"err: src\\d3d11\\d3d11_context_impl.cpp:SwapDeviceContextState is not
implemented." (2026-10-03 20:22 log).

A state object now holds a D3D11ContextState. Swapping moves the context's
current state into the object that was active (a fresh one stands for the
context's own state the first time), and the new object's state into the
context. DXMT's BindingSet move marks every bound slot dirty, so after the
encoder state is cleared the next draw binds everything again; the index
buffer (bound at set time) is re-emitted and the fixed-function state is
marked dirty. The active object's own copy is empty while it is swapped in,
as each object is active in one context at a time.

The first swap in a process logs "[d3d11] madeira-bcd: SwapDeviceContextState
in use (context state swap)" once; tools/build-d3d11-dll.sh also looks for that
string in the DLL it builds, to prove the patch is in.

Idempotent; fails by name if an anchor moves. Run from the repository root;
the optional argument is another d3d11 source directory to patch instead of
dxmt/src/d3d11 (tools/build-d3d11-dll.sh patches a copy that way).
"""
import pathlib
import sys

ROOT = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "dxmt/src/d3d11")
MARKER = "madeira-bcd: context state swap"


def edit(rel, pairs):
    path = ROOT / rel
    s = path.read_text()
    if MARKER in s:
        return False
    for old, new in pairs:
        if s.count(old) != 1:
            sys.exit(f"patch-dxmt-context-state-swap: anchor not found exactly once in {rel}: {old[:60]!r}")
        s = s.replace(old, new, 1)
    path.write_text(s)
    return True


changed = edit("d3d11_context_state.hpp", [(
    """  MTLD3D11DeviceContextState(MTLD3D11Device *pDevice)
      : MTLD3D11DeviceChild<ID3DDeviceContextState>(pDevice) {}
""",
    """  MTLD3D11DeviceContextState(MTLD3D11Device *pDevice)
      : MTLD3D11DeviceChild<ID3DDeviceContextState>(pDevice) {}

  /* madeira-bcd: context state swap -- the state this object holds while
     it is not the active one (tools/patch-dxmt-context-state-swap.py) */
  D3D11ContextState state;
""")])

changed |= edit("d3d11_context_impl.cpp", [
    ("""  SwapDeviceContextState(ID3DDeviceContextState *pState, ID3DDeviceContextState **ppPreviousState) override {
    IMPLEMENT_ME
  }
""",
     """  SwapDeviceContextState(ID3DDeviceContextState *pState, ID3DDeviceContextState **ppPreviousState) override {
    /* madeira-bcd: context state swap (tools/patch-dxmt-context-state-swap.py) */
    std::lock_guard<mutex_t> lock(mutex);
    static bool s_swapNoted = false;
    if (!std::exchange(s_swapNoted, true))
      Logger::info("[d3d11] madeira-bcd: SwapDeviceContextState in use (context state swap)");

    if (ppPreviousState)
      *ppPreviousState = nullptr;
    if (!pState)
      return;

    Com<MTLD3D11DeviceContextState> next = static_cast<MTLD3D11DeviceContextState *>(pState);
    Com<MTLD3D11DeviceContextState> previous = std::move(current_state_object_);
    if (previous == nullptr)
      previous = new MTLD3D11DeviceContextState(device);
    if (ppPreviousState)
      *ppPreviousState = previous.ref();
    if (previous.ptr() == next.ptr()) {
      current_state_object_ = std::move(next);
      return;
    }

    previous->state = std::move(state_);
    ResetEncodingContextState();
    state_ = std::move(next->state);
    next->state = {};
    current_state_object_ = std::move(next);

    if (state_.InputAssembler.IndexBuffer) {
      EmitST([buffer = state_.InputAssembler.IndexBuffer->buffer()](ArgumentEncodingContext &enc) mutable {
        enc.bindIndexBuffer(forward_rc(buffer));
      });
    }
    dirty_state.set(DirtyState::DepthStencilState);
    dirty_state.set(DirtyState::RasterizerState);
    dirty_state.set(DirtyState::BlendFactorAndStencilRef);
    dirty_state.set(DirtyState::Viewport);
    dirty_state.set(DirtyState::Scissors);
  }
"""),
    ("""protected:
  D3D11ContextState state_;
""",
     """protected:
  D3D11ContextState state_;
  /* madeira-bcd: context state swap -- the object swapped in last */
  Com<MTLD3D11DeviceContextState> current_state_object_;
"""),
])
print("patched" if changed else "already patched")
