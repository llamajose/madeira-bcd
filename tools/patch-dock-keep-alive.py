#!/usr/bin/env python3
"""Keep a Dock game session while named helper processes still run.

Madeira Dock (madeira-dock/src/launch.c, upstream submodule) ends the session
3 s after Valve's client stops reporting the app as running. GTA V Enhanced
starts through PlayGTAV.exe, which only waits for the Rockstar Games
Launcher: when the launcher restarts itself after a self-update, PlayGTAV.exe
exits with code 0 ("Launch finished"), Steam reports the game ended, and the
Dock shut Valve's client down while the new launcher was still starting
(2026-10-03 23:21 log). The game itself later needs that client.

With MADEIRA_DOCK_KEEP_ALIVE=<exe>;<exe>;... (`env.MADEIRA_DOCK_KEEP_ALIVE =
Launcher.exe;GTA5_Enhanced.exe` in a game's settings) the end is postponed
while any process with one of those image names exists; it is checked once
per 3 s window (CreateToolhelp32Snapshot), and the session ends as before 3 s
after the last one is gone. Report field launch-keep-alive (1 the first time
it held the session). Off by default.

Usage: patch-dock-keep-alive.py madeira-dock/src/launch.c (idempotent)
"""
import sys

path = sys.argv[1]
src = open(path).read()
if "MADEIRA_DOCK_KEEP_ALIVE" in src:
    print("already patched"); sys.exit(0)

anchor_inc = '#include <wchar.h>\n'
inc = anchor_inc + '#include <tlhelp32.h>\n'

anchor_fn = '''int sh_launch(HMODULE module, void *engine, void *client_user,
              const struct sh_api *api, const struct sh_observer *o,
              int32_t pipe, int32_t user, uint64_t steamid, uint32_t appid,
              const struct dock_client_layout *layout)
{
    if (!layout'''
helper = '''/* madeira-bcd: MADEIRA_DOCK_KEEP_ALIVE, see tools/patch-dock-keep-alive.py. */
static bool keep_alive_running(void)
{
    static wchar_t list[1024];
    DWORD n = GetEnvironmentVariableW(L"MADEIRA_DOCK_KEEP_ALIVE", list, 1024);
    if (!n || n >= 1024) return false;
    HANDLE snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snap == INVALID_HANDLE_VALUE) return false;
    PROCESSENTRY32W entry;
    memset(&entry, 0, sizeof(entry));
    entry.dwSize = sizeof(entry);
    bool found = false;
    for (BOOL ok = Process32FirstW(snap, &entry); ok && !found; ok = Process32NextW(snap, &entry)) {
        const wchar_t *p = list;
        while (*p && !found) {
            const wchar_t *end = wcschr(p, L';');
            size_t len = end ? (size_t)(end - p) : wcslen(p);
            if (len && wcslen(entry.szExeFile) == len && !_wcsnicmp(entry.szExeFile, p, len)) found = true;
            p += len + (end ? 1 : 0);
        }
    }
    CloseHandle(snap);
    return found;
}

'''

anchor_end = '''            if (o->now_ms() - stopped_at >= 3000) {
                o->event("launch-game-ended", 1);'''
new_end = '''            if (o->now_ms() - stopped_at >= 3000 && keep_alive_running()) {
                /* madeira-bcd: MADEIRA_DOCK_KEEP_ALIVE -- a named helper (a
                 * self-updating launcher) still runs: hold the session. */
                static bool kept;
                if (!kept) { kept = true; o->event("launch-keep-alive", 1); }
                stopped_at = o->now_ms();
            }
            if (o->now_ms() - stopped_at >= 3000) {
                o->event("launch-game-ended", 1);'''

for a in (anchor_inc, anchor_fn, anchor_end):
    if src.count(a) != 1:
        print("patch-dock-keep-alive: anchor not found exactly once -- launch.c changed upstream", file=sys.stderr)
        sys.exit(1)
src = src.replace(anchor_inc, inc, 1)
src = src.replace(anchor_fn, helper + anchor_fn, 1)
src = src.replace(anchor_end, new_end, 1)
open(path, "w").write(src)
print("patched")
