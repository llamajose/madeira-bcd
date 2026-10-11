#!/usr/bin/env python3
"""The frame cap of the opt-in power governor for DXMT presents.

build/ntdll-unix/server_ios.c's frame-cap governor (env.MADEIRA_FPS_CAP = fps,
or env.MADEIRA_FPS_GOV_MW = a CPU power target) publishes a cap; madeira-d3d12
holds the game's thread in its own Present for it. A D3D11 game's frames reach
the display through DXMT's encode thread and winemetal's presentDrawable, which
this patch makes call ios_fps_cap_dxmt_pace() (same binary, server_ios.c) at
the frame boundary, before frame generation and the vsync-mode present:
  - unarmed (the default): it returns at once, nothing else changes;
  - armed: it counts the present (the governor's frame rate) and, when a cap is
    set and no D3D12 runtime paces itself, waits for the presenting thread's
    next deadline with mach_wait_until; the [frame] limiter time gets the wait.
The app's FPS limit (vsync modes 1/3/4, afterMinimumDuration) is untouched and
applies after it, so the lower of the two wins.

Native only (winemetal unix side), not in the i386 farm key.
Idempotent; fails by name if the anchor moves. Run from the repository root.
"""
import pathlib
import sys

PATH = pathlib.Path("dxmt/src/winemetal/unix/winemetal_unix.c")
MARKER = "madeira-bcd fps-gov"

OLD = """  int mode = g_madeira_vsync_mode;
  /* ml1050: this call IS the frame boundary on the encode thread. */
"""
NEW = """  int mode = g_madeira_vsync_mode;
  {   /* madeira-bcd fps-gov (tools/patch-winemetal-fps-cap.py): the governor's frame cap, opt-in */
    extern unsigned long long ios_fps_cap_dxmt_pace(void);
    unsigned long long fps_cap_ns = ios_fps_cap_dxmt_pace();
    if (fps_cap_ns)
      ios_frame_limiter(fps_cap_ns);
  }
  /* ml1050: this call IS the frame boundary on the encode thread. */
"""


def main():
    s = PATH.read_text()
    if MARKER in s:
        print("winemetal_unix.c: frame cap already patched")
        return 0
    if s.count(OLD) != 1:
        sys.exit(f"patch-winemetal-fps-cap: anchor found {s.count(OLD)} times (want 1) in {PATH}:\n{OLD}")
    if "extern void ios_frame_limiter(unsigned long long ns);" not in s:
        sys.exit(f"patch-winemetal-fps-cap: ios_frame_limiter is not declared in {PATH}")
    PATH.write_text(s.replace(OLD, NEW))
    print("winemetal_unix.c: frame cap patched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
