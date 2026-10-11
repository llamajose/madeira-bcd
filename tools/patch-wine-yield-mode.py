#!/usr/bin/env python3
"""madeira-bcd: opt-in yield primitive for NtYieldExecution (madeira.cfg yield-mode).

SwitchToThread, Sleep(0) and every non-alertable Sleep reach NtYieldExecution,
whose iOS body (wine/dlls/ntdll/unix/sync.c, ml1063/ml1124) yields with
sched_yield() and, after yield-streak back-to-back yields, sleeps yield-sleep-us
instead. On Darwin sched_yield() is the swtch_pri trap: when another thread
could use the core, it drops the caller to the lowest priority until it runs
again or one scheduler quantum (10 ms) has passed. Horizon Zero Dawn under the
power clamp (about 7 runnable threads on 4 efficiency cores) yields 1-2.6
thousand times a second in play; 14-18 % of its runnable-thread samples sit at
that trap ([cpu-split], x16 = -59), i.e. a yielding thread waits about a
millisecond in the run queue per yield, behind every thread that has not
yielded. Whether a shorter or no priority drop gives the game's workers the
cores sooner or only brings the yielder back sooner depends on the game, so
this is a switch, off by default:

  madeira.cfg yield-mode
    unset, pri   sched_yield(), as before
    switch       thread_switch(SWITCH_OPTION_NONE): hand the core to another
                 runnable thread without lowering the caller's priority
    depress      thread_switch(SWITCH_OPTION_DEPRESS, yield-depress-ms): the
                 same priority drop, bounded at yield-depress-ms (default 1,
                 1..10 ms) instead of a quantum

The adaptive sleep (yield-streak, yield-sleep-us) is unchanged and still comes
first. Usage: patch-wine-yield-mode.py wine/dlls/ntdll/unix/sync.c (idempotent).
"""
from pathlib import Path
import sys

MARKER = "madeira-bcd yield-mode"

HEAD = """/******************************************************************
 *		NtYieldExecution (NTDLL.@)
 */
NTSTATUS WINAPI NtYieldExecution(void)
{
"""
HELPER = """#ifdef WINE_IOS
#include <mach/mach.h>
#include <mach/thread_switch.h>
/* madeira-bcd yield-mode (tools/patch-wine-yield-mode.py): the yield primitive,
 * madeira.cfg yield-mode = pri (unset: sched_yield, i.e. swtch_pri, as before),
 * switch (thread_switch, no priority drop) or depress (the drop bounded at
 * yield-depress-ms, default 1). Read once. */
static int madeira_yield_mode = -1;
static mach_msg_timeout_t madeira_yield_depress_ms = 1;

static void madeira_yield(void)
{
    if (madeira_yield_mode < 0)
    {
        char v[16];
        int mode = 0;
        long long ms = madeira_cfg_int( "yield-depress-ms", 1 );

        if (madeira_cfg_get( "yield-mode", v, sizeof(v) ))
        {
            if (!strcmp( v, "switch" )) mode = 1;
            else if (!strcmp( v, "depress" )) mode = 2;
        }
        if (ms < 1) ms = 1;
        if (ms > 10) ms = 10;
        madeira_yield_depress_ms = (mach_msg_timeout_t)ms;
        if (mode == 1)
            fprintf( stderr, "[sync-census] madeira-bcd yield-mode = switch (thread_switch, no priority drop)\\n" );
        else if (mode == 2)
            fprintf( stderr, "[sync-census] madeira-bcd yield-mode = depress (thread_switch, priority drop for %lld ms)\\n", ms );
        __atomic_store_n( &madeira_yield_mode, mode, __ATOMIC_RELEASE );
    }
    switch (__atomic_load_n( &madeira_yield_mode, __ATOMIC_ACQUIRE ))
    {
    case 1: thread_switch( MACH_PORT_NULL, SWITCH_OPTION_NONE, 0 ); break;
    case 2: thread_switch( MACH_PORT_NULL, SWITCH_OPTION_DEPRESS, madeira_yield_depress_ms ); break;
    default: sched_yield(); break;
    }
}
#endif

"""
YIELD_OLD = """    ret = getrusage( RUSAGE_THREAD, &u1 );
#endif
    sched_yield();
#ifdef RUSAGE_THREAD
"""
YIELD_NEW = """    ret = getrusage( RUSAGE_THREAD, &u1 );
#endif
#ifdef WINE_IOS
    madeira_yield();   /* madeira-bcd yield-mode: sched_yield unless madeira.cfg says otherwise */
#else
    sched_yield();
#endif
#ifdef RUSAGE_THREAD
"""


def patch(source):
    if MARKER in source:
        return source
    replacements = ((HEAD, HELPER + HEAD), (YIELD_OLD, YIELD_NEW))
    for old, _ in replacements:
        if source.count(old) != 1:
            raise ValueError("yield-mode anchor missing or ambiguous: " + old.splitlines()[0])
    for old, new in replacements:
        source = source.replace(old, new)
    return source


def main():
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "wine/dlls/ntdll/unix/sync.c")
    source = path.read_text()
    try:
        result = patch(source)
    except ValueError as error:
        sys.exit(str(error))
    if result == source:
        print("already patched")
    else:
        path.write_text(result)
        print("patched")


if __name__ == "__main__":
    main()
