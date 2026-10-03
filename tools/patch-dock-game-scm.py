#!/usr/bin/env python3
"""Start Wine's service manager in a Dock game session on request.

Madeira Dock (madeira-dock/src/launch.c, upstream submodule) starts Wine's
services.exe only for the one-time install batch (`dockhost.exe
--start-services`) and for Valve's CEG step. A game whose launcher drives its
own Windows service -- GTA V Enhanced: Rockstar Games Launcher runs
`RockstarService.exe start` -- then finds no service manager in the game
session and every start fails with RPC error 1722.

With MADEIRA_DOCK_GAME_SCM=1 (`env.MADEIRA_DOCK_GAME_SCM = 1` in a game's
settings) sh_launch() starts it the way `--start-services` does
(sh_install_scm_start: left running, ends with the session), on a worker
thread bounded like that step, before Steam's LaunchApp. Report fields
game-scm (the install-scm outcome) and game-scm-timeout. Off by default.

Usage: patch-dock-game-scm.py madeira-dock/src/launch.c (idempotent)
"""
import sys

path = sys.argv[1]
src = open(path).read()
if "MADEIRA_DOCK_GAME_SCM" in src:
    print("already patched"); sys.exit(0)

anchor_fn = '''int sh_launch(HMODULE module, void *engine, void *client_user,
              const struct sh_api *api, const struct sh_observer *o,
              int32_t pipe, int32_t user, uint64_t steamid, uint32_t appid,
              const struct dock_client_layout *layout)
{
    if (!layout'''
worker = '''/* madeira-bcd: see tools/patch-dock-game-scm.py. */
#define SH_GAME_SCM_STEP_MS 20000
static volatile int32_t game_scm_outcome;

static DWORD WINAPI game_scm_worker(void *observer)
{
    game_scm_outcome = sh_install_scm_start((const struct sh_observer *)observer);
    return 0;
}

'''
anchor_call = '''    o->event("launch-install-directory-verified", 1);
'''
call = '''    /* madeira-bcd: MADEIRA_DOCK_GAME_SCM=1, see tools/patch-dock-game-scm.py. */
    if (wide_flag(L"MADEIRA_DOCK_GAME_SCM", L'1')) {
        HANDLE worker = CreateThread(NULL, 0, game_scm_worker, (void *)o, 0, NULL);
        if (!worker) o->event("game-scm", sh_install_scm_start(o));
        else {
            if (WaitForSingleObject(worker, SH_GAME_SCM_STEP_MS) == WAIT_OBJECT_0) o->event("game-scm", game_scm_outcome);
            else o->event("game-scm-timeout", 1);
            CloseHandle(worker);
        }
    }
'''
if src.count(anchor_fn) != 1 or src.count(anchor_call) != 1 or "static bool wide_flag" not in src:
    print("patch-dock-game-scm: anchors not found exactly once -- launch.c changed upstream", file=sys.stderr)
    sys.exit(1)
src = src.replace(anchor_fn, worker + anchor_fn, 1)
src = src.replace(anchor_call, anchor_call + call, 1)
open(path, "w").write(src)
print("patched")
