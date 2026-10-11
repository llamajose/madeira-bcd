#!/usr/bin/env python3
"""ntdll-sync.dll: Wine's PE ntdll with cheaper SRW locks and critical sections,
an opt-in ARM64EC ntdll (off by default since build 499).

tools/patch-wine-ntdll-sync.py changes dlls/ntdll/sync.c (a release wakes only
a waiter the lock word records: bit 15 of exclusive_waiters marks parked shared
waiters; no ml410 ring), adds x64 bodies for the six SRW exports and for
RtlEnterCriticalSection, RtlTryEnterCriticalSection and RtlLeaveCriticalSection
(sync_x64.c, EXP+#; the critical-section C code is unchanged) and exports
madeira_sync_x64_off. tools/build-wine-extra-dlls.sh builds
it as ntdll-sync.dll after checking that the unpatched tree rebuilds the
shipped ntdll.dll apart from its time stamps; build/ntdll-unix/loader_ios.c
maps it only with env.MADEIRA_NTDLL_SYNC = 1 or native. On build 498, where it
was the default, every 64-bit game died at start: ARM64EC code reached the x64
RtlEnterCriticalSection body through kernel32's forwarder.

Checks:
  1. the patch: exactly the five sync.c edits, one spec line, sync_x64.c and a
     SOURCES append; undoing the edits gives back the original files; a second
     run changes nothing; a moved anchor fails loudly;
  2. the x64 bodies' encodings (an x86_64 COFF assembler, when there is one)
     are what --verify expects, and no word of them looks like an x18 access
     to the x18 patcher, which walks all of .text;
  3. behaviour, compiled on this host from the real sync.c text (stock and
     patched) with stand-ins for the alert syscalls:
       - uncontended lock traffic makes no RtlWakeAddress* call any more
         (stock: one per exclusive release and per last shared release);
       - single-threaded try/acquire/release sequences return the same results
         and leave the same lock words as the stock code;
       - threads hammering a few locks (blocking, try and shared paths mixed)
         keep mutual exclusion, lose no wakeup (a watchdog) and leave every
         word 0; condition variables in exclusive and shared mode; shared
         waiters parked behind exclusive owners and waiters all wake;
       - on an x86_64 host the x64 bodies themselves run (ms_abi), mixed with
         the native functions on the same locks, and take no native call when
         uncontended; madeira_sync_x64_off sends them to the native bodies;
       - critical sections (Wine's code from sync.c, and on an x86_64 Linux
         host the x64 bodies, %gs set with arch_prctl): free, nested and try
         paths without a native call, the same results and fields as the
         native code, a release that meets a new waiter handed to the native
         code, and threads mixing both keep exclusion and lose no wakeup;
  4. --same-as and --verify on synthetic images (NTDLL_SYNC_DLL and
     NTDLL_STOCK_DLL check real builds too);
  5. the loader, the farm links, the build step, CI and the catalog.
Needs the wine submodule checked out and `cc` on PATH.
"""
from pathlib import Path
import importlib.util
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[2]
script = root / 'tools/patch-wine-ntdll-sync.py'
spec = importlib.util.spec_from_file_location('ntdllsync', script)
nts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nts)

ntdir = root / 'wine/dlls/ntdll'
if not (ntdir / 'sync.c').is_file():
    print('FAIL: wine/dlls/ntdll/sync.c missing (check out the wine submodule)')
    sys.exit(1)
cc = os.environ.get('CC') or shutil.which('cc') or shutil.which('clang') or shutil.which('gcc')
failures = []


def check(cond, what):
    print(('PASS: ' if cond else 'FAIL: ') + what)
    if not cond:
        failures.append(what)


# ---- 1. the patch ------------------------------------------------------------------
orig = {n: (ntdir / n).read_text() for n in ('sync.c', 'ntdll.spec', 'Makefile.in')}
with tempfile.TemporaryDirectory(prefix='madeira-ntdll-sync-') as d:
    tree = Path(d)
    (tree / 'dlls/ntdll').mkdir(parents=True)
    for n, t in orig.items():
        (tree / 'dlls/ntdll' / n).write_text(t)
    r = subprocess.run([sys.executable, str(script), str(tree)], capture_output=True, text=True)
    check(r.returncode == 0 and 'patched sync.c, ntdll.spec, sync_x64.c, Makefile.in' in r.stdout,
          'the patch applies to the submodule\'s sync.c, ntdll.spec and Makefile.in (%s)' % (r.stdout + r.stderr).strip())
    patched = {n: (tree / 'dlls/ntdll' / n).read_text() for n in orig}
    x64src = (tree / 'dlls/ntdll/sync_x64.c').read_text()
    back = patched['sync.c']
    for what, old, new in nts.SYNC_EDITS:
        check(patched['sync.c'].count(new) == 1, 'sync.c carries the %s edit once' % what)
        back = back.replace(new, old)
    check(back == orig['sync.c'], 'undoing the five sync.c edits gives back the submodule\'s sync.c byte for byte')
    check(patched['ntdll.spec'].replace(nts.SPEC_NEW, nts.SPEC_OLD) == orig['ntdll.spec'] and
          '@ extern -private -arch=arm64ec madeira_sync_x64_off' in patched['ntdll.spec'],
          'ntdll.spec only gains the madeira_sync_x64_off export (arm64ec)')
    a, b = orig['Makefile.in'].split('\n'), patched['Makefile.in'].split('\n')
    i = a.index('SOURCES = \\')
    j = i + 1
    while a[j].endswith('\\'):
        j += 1
    check(b[:j] == a[:j] and b[j] == a[j] + ' \\' and b[j + 1] == '\tsync_x64.c' and b[j + 2:] == a[j + 1:],
          'Makefile.in only appends sync_x64.c to SOURCES')
    check(x64src == nts.SYNC_X64_C and '#if 0\n#pragma makedep arm64ec_x64\n#endif' in x64src and
          '#ifdef __arm64ec_x64__' in x64src and
          all('__ASM_GLOBAL_FUNC( "EXP+#%s",' % n in x64src for n in nts.X64_BODIES),
          'sync_x64.c is an arm64ec_x64 source with the nine EXP+# bodies (six SRW, three critical-section)')
    r = subprocess.run([sys.executable, str(script), str(tree)], capture_output=True, text=True)
    again = {n: (tree / 'dlls/ntdll' / n).read_text() for n in orig}
    check(r.returncode == 0 and 'already patched' in r.stdout and again == patched, 'a second run changes nothing')
    for n, t in orig.items():
        (tree / 'dlls/ntdll' / n).write_text(t)
    (tree / 'dlls/ntdll/sync.c').write_text(orig['sync.c'].replace('        RtlWakeAddressAll( u.s );\n',
                                                                     '        RtlWakeAddressAll( (void *)u.s );\n'))
    r = subprocess.run([sys.executable, str(script), str(tree)], capture_output=True, text=True)
    check(r.returncode != 0 and 'RtlReleaseSRWLockExclusive anchor found 0 times' in r.stderr,
          'a moved anchor stops the patch with an error')

# the C stays Wine's: the ring writes are gone from every SRW path, the flag
# logic reads only the values its own compare-exchange saw
s = patched['sync.c']
srw = s[s.index('void WINAPI RtlAcquireSRWLockExclusive'):s.index('void WINAPI RtlInitializeConditionVariable')]
check('InterlockedIncrement( &ios_srw_ring_idx )' not in s and s.count('ios_srw_note( lock,') == 6,
      'ios_srw_note is empty; the six call sites stay (the ring index and table are never written)')
check('if (new.s.exclusive_waiters)\n        RtlWakeAddressSingle( &u.s->owners );\n'
      '    else if (old.s.exclusive_waiters & MADEIRA_SRW_SHARED_PARKED)\n        RtlWakeAddressAll( u.s );' in srw,
      'an exclusive release wakes an exclusive waiter if any, else parked shared waiters, else nobody')
check('if (!new.s.owners && (new.s.exclusive_waiters & MADEIRA_SRW_EXCLUSIVE))\n        RtlWakeAddressSingle( &u.s->owners );' in srw,
      'the last shared release wakes only when an exclusive waiter is counted')

# ---- 2. encodings and the x18 patcher -------------------------------------------


def c_string_bodies(src):
    """{name: asm text} from the __ASM_GLOBAL_FUNC( "EXP+#name", "..." "..." ) blocks."""
    out = {}
    for m in re.finditer(r'__ASM_GLOBAL_FUNC\( "EXP\+#(\w+)",(.*?)\)\n', src, re.S):
        lits = re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(2))
        text = ''.join(lits)
        out[m.group(1)] = text.encode().decode('unicode_escape')
    return out


asm_bodies = c_string_bodies(nts.SYNC_X64_C)
check(sorted(asm_bodies) == sorted(nts.X64_BODIES) and len(asm_bodies) == 9, 'the nine bodies are parsed from sync_x64.c')


def coff_text_sections(obj):
    d = obj.read_bytes()
    if struct.unpack_from('<H', d, 0)[0] != 0x8664:
        return None
    nsec, = struct.unpack_from('<H', d, 2)
    out = []
    for k in range(nsec):
        s = 20 + 40 * k
        name = d[s:s + 8].rstrip(b'\0')
        size, ptr = struct.unpack_from('<II', d, s + 16)
        if name == b'.text' and size:
            out.append(d[ptr:ptr + size])
    return out


assembled = None
with tempfile.TemporaryDirectory(prefix='madeira-ntdll-sync-as-') as d:
    folder = Path(d)
    (folder / 'sync_x64.c').write_text(nts.SYNC_X64_C)
    for tool in (os.environ.get('X64_CC'), 'x86_64-w64-mingw32-clang', 'clang'):
        if not tool or not shutil.which(tool):
            continue
        r = subprocess.run([tool, '-target', 'x86_64-w64-windows-gnu', '-c', '-O2', '-D__WINE_PE_BUILD',
                            '-D__arm64ec_x64__', '-I', str(root / 'wine/include'), str(folder / 'sync_x64.c'),
                            '-o', str(folder / 'sync_x64.o')], capture_output=True, text=True)
        if r.returncode == 0:
            assembled = coff_text_sections(folder / 'sync_x64.o')
            if assembled:
                break
if not assembled:
    print('SKIP: no x86_64 COFF assembler here; the encodings rest on X64_BODIES (CI --verify reads the built DLL)')
else:
    ok = len(assembled) == len(nts.X64_BODIES)
    for name, body in zip(asm_bodies, assembled):     # one COMDAT .text per function, in source order
        want, mask = nts.body_pattern(name)
        ok = ok and len(body) >= len(want) and all(not m or b == w for b, w, m in zip(body, want, mask))
    check(ok, 'an x86_64 assembler encodes the nine bodies as --verify expects')

# every 4-byte word of each body (16-byte aligned, padded with int3 as lld does),
# with the rel32 fields set to a spread of plausible displacements
hits = []
for name in nts.X64_BODIES:
    want, mask = nts.body_pattern(name)
    for rel in (0x0004d0f0, 0x00052d40, 0xfffe18b0, 0xfffd9a20, 0xffff8004, 0x00000000):
        b = bytearray(want)
        k = 0
        while k < len(b):
            if not mask[k]:
                struct.pack_into('<I', b, k, rel)
                k += 4
            else:
                k += 1
        b += b'\xcc' * (-len(b) % 16)
        for w in range(0, len(b), 4):
            insn = struct.unpack_from('<I', b, w)[0]
            if nts.x18_role(insn):
                hits.append('%s+%#x %08x' % (name, w, insn))
check(not hits, 'no word of the x64 bodies looks like an x18 access to the x18 patcher %s' % hits[:4])
native = (root / 'build/ntdll-unix/virtual_ios.c').read_text()
role_c = native[native.index('static int ios_insn_x18_role(uint32_t insn)'):]
role_c = role_c[:role_c.index('\n}\n') + 3]
check('if ((top8 & 0x1F) == 0x11)' in role_c and '((insn >> 10) & 3) == 2' in role_c and
      '(top11 & 0x3E7) == 0x1C0' in role_c,
      'the x18 classifier mirrored in the patch script matches build/ntdll-unix/virtual_ios.c')

# ---- 3. behaviour -------------------------------------------------------------------
HARNESS = r'''
#define _GNU_SOURCE
#include <pthread.h>
#include <sched.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <errno.h>
#include <stddef.h>
#include <unistd.h>
#include "wine/list.h"

typedef int32_t LONG; typedef uint32_t ULONG; typedef uint16_t USHORT; typedef uint8_t UCHAR;
typedef int64_t LONG64, LONGLONG; typedef uint64_t ULONG64, ULONGLONG;
typedef uintptr_t ULONG_PTR, DWORD_PTR, SIZE_T;
typedef uint32_t DWORD; typedef int BOOL; typedef unsigned char BOOLEAN;
typedef void *HANDLE; typedef LONG NTSTATUS;
typedef union { struct { uint32_t LowPart; int32_t HighPart; } u; LONGLONG QuadPart; } LARGE_INTEGER;
typedef struct { void *Ptr; } RTL_SRWLOCK, RTL_CONDITION_VARIABLE;
/* the x64 layout, as on Windows (LockCount 8, RecursionCount 12, OwningThread 16) */
typedef struct _RTL_CRITICAL_SECTION_DEBUG
{
    USHORT Type, CreatorBackTraceIndex;
    struct _RTL_CRITICAL_SECTION *CriticalSection;
    struct { void *Flink, *Blink; } ProcessLocksList;
    ULONG EntryCount, ContentionCount;
    ULONG_PTR Spare[1];
} RTL_CRITICAL_SECTION_DEBUG;
typedef struct _RTL_CRITICAL_SECTION
{
    RTL_CRITICAL_SECTION_DEBUG *DebugInfo;
    LONG LockCount;
    LONG RecursionCount;
    HANDLE OwningThread;
    HANDLE LockSemaphore;
    ULONG_PTR SpinCount;
} RTL_CRITICAL_SECTION;
typedef int SYSTEM_INFORMATION_CLASS;
#define WINAPI
#define TRUE 1
#define FALSE 0
#define STATUS_SUCCESS 0
#define STATUS_ALERTED 0x101
#define STATUS_TIMEOUT 0x102
#define STATUS_INVALID_PARAMETER ((NTSTATUS)0xC000000D)
#define STATUS_NOT_IMPLEMENTED ((NTSTATUS)0xC0000002)
#define RTL_CONDITION_VARIABLE_LOCKMODE_SHARED 0x1
#define ARRAY_SIZE(a) (sizeof(a) / sizeof((a)[0]))
#define C_ASSERT(e) _Static_assert(e, #e)
#define HandleToULong(h) ((ULONG)(ULONG_PTR)(h))
#define ULongToHandle(u) ((HANDLE)(ULONG_PTR)(u))
#define InterlockedCompareExchange(p, n, o) __sync_val_compare_and_swap((p), (o), (n))
#define InterlockedCompareExchangePointer(p, n, o) __sync_val_compare_and_swap((p), (o), (n))
#define InterlockedExchange(p, v) __atomic_exchange_n((p), (v), __ATOMIC_SEQ_CST)
#define InterlockedIncrement(p) __sync_add_and_fetch((p), 1)
#define InterlockedDecrement(p) __sync_sub_and_fetch((p), 1)
#define InterlockedIncrement64(p) __sync_add_and_fetch((p), 1)
#define InterlockedExchangeAdd16(p, v) __sync_fetch_and_add((p), (v))
#define YieldProcessor() __asm__ __volatile__( "" ::: "memory" )
#define TRACE(...) do { } while (0)
#define WARN(...) do { } while (0)
static volatile long errs;
#define ERR(...) do { __sync_add_and_fetch( &errs, 1 ); } while (0)
C_ASSERT( offsetof(RTL_CRITICAL_SECTION, LockCount) == 8 && offsetof(RTL_CRITICAL_SECTION, RecursionCount) == 12 &&
          offsetof(RTL_CRITICAL_SECTION, OwningThread) == 16 && offsetof(RTL_CRITICAL_SECTION, SpinCount) == 32 );

typedef struct { struct { HANDLE UniqueProcess, UniqueThread; } ClientId; void *Instrumentation[16]; } TEB;
static __thread TEB teb;
static TEB *NtCurrentTeb(void) { return &teb; }
static DWORD GetCurrentThreadId(void) { return HandleToULong( teb.ClientId.UniqueThread ); }

static struct { LONG64 woa_waits, woa_wake_single, woa_wake_all, cs_spin_acquired, cs_wakes, cs_contended; } ios_xp_nt;

static NTSTATUS NtQuerySystemInformation( SYSTEM_INFORMATION_CLASS c, void *p, ULONG l, ULONG *r )
{
    (void)c; (void)p; (void)l; (void)r;
    return STATUS_NOT_IMPLEMENTED;   /* no shared table: the copy's own futex_queues */
}

/* the alert syscalls: one auto-reset flag per thread, as on Windows */
#define MAXT 32
static struct { pthread_mutex_t m; pthread_cond_t c; int flag; } alerts[MAXT + 1];
static void alerts_init(void)
{
    int i;
    for (i = 0; i <= MAXT; i++) { pthread_mutex_init( &alerts[i].m, NULL ); pthread_cond_init( &alerts[i].c, NULL ); }
}
static NTSTATUS NtAlertThreadByThreadId( HANDLE tid )
{
    ULONG t = HandleToULong( tid );
    if (!t || t > MAXT) abort();
    pthread_mutex_lock( &alerts[t].m );
    alerts[t].flag = 1;
    pthread_cond_signal( &alerts[t].c );
    pthread_mutex_unlock( &alerts[t].m );
    return STATUS_SUCCESS;
}
static NTSTATUS NtAlertMultipleThreadByThreadId( HANDLE *tids, ULONG count, void *a, void *b )
{
    ULONG i;
    (void)a; (void)b;
    for (i = 0; i < count; i++) NtAlertThreadByThreadId( tids[i] );
    return STATUS_SUCCESS;
}
static NTSTATUS NtWaitForAlertByThreadId( const void *addr, const LARGE_INTEGER *timeout )
{
    ULONG t = GetCurrentThreadId();
    NTSTATUS ret = STATUS_ALERTED;
    struct timespec ts;
    (void)addr;
    if (timeout)
    {
        long long rel = timeout->QuadPart < 0 ? -timeout->QuadPart : 0;   /* 100 ns */
        clock_gettime( CLOCK_REALTIME, &ts );
        ts.tv_sec += rel / 10000000;
        ts.tv_nsec += (rel % 10000000) * 100;
        if (ts.tv_nsec >= 1000000000) { ts.tv_sec++; ts.tv_nsec -= 1000000000; }
    }
    pthread_mutex_lock( &alerts[t].m );
    while (!alerts[t].flag)
    {
        if (!timeout) pthread_cond_wait( &alerts[t].c, &alerts[t].m );
        else if (pthread_cond_timedwait( &alerts[t].c, &alerts[t].m, &ts ) == ETIMEDOUT) { ret = STATUS_TIMEOUT; break; }
    }
    if (ret == STATUS_ALERTED) alerts[t].flag = 0;
    pthread_mutex_unlock( &alerts[t].m );
    return ret;
}
NTSTATUS WINAPI RtlEnterCriticalSection( RTL_CRITICAL_SECTION *crit );
BOOL WINAPI RtlTryEnterCriticalSection( RTL_CRITICAL_SECTION *crit );
NTSTATUS WINAPI RtlLeaveCriticalSection( RTL_CRITICAL_SECTION *crit );
NTSTATUS WINAPI RtlWaitOnAddress( const void *addr, const void *cmp, SIZE_T size, const LARGE_INTEGER *timeout );
void WINAPI RtlWakeAddressAll( const void *addr );
void WINAPI RtlWakeAddressSingle( const void *addr );

#include "region.c"

/* ---- Wine's critical sections (cs.c: sync.c's functions as they are), with
 * stand-ins for the diagnostics and the semaphore calls a section without a
 * LockSemaphore handle never makes ---- */
#define STATUS_WAIT_0 0
#define WAIT_TIMEOUT 258
#define TRACE_ON(ch) 0
static BOOL RtlDllShutdownInProgress(void) { return FALSE; }
static NTSTATUS NtWaitForSingleObject( HANDLE h, BOOLEAN a, const LARGE_INTEGER *t ) { (void)h; (void)a; (void)t; abort(); }
static NTSTATUS NtReleaseSemaphore( HANDLE h, ULONG n, ULONG *p ) { (void)h; (void)n; (void)p; abort(); }
static void RtlRaiseStatus( NTSTATUS s ) { (void)s; abort(); }
static void ios_cs_dump( unsigned int tid, const char *what ) { (void)tid; (void)what; }
static void ios_cs_note( RTL_CRITICAL_SECTION *c, int op, void *caller ) { (void)c; (void)op; (void)caller; }
static void ios_cs_check_transition( RTL_CRITICAL_SECTION *c, const char *w ) { (void)c; (void)w; }
static void ios_cs_report_bad( RTL_CRITICAL_SECTION *c, const char *w ) { (void)c; (void)w; }
static ULONGLONG ios_xp_ticks(void) { return 0; }
static void ios_xp_cs_waited( RTL_CRITICAL_SECTION *c, ULONGLONG t0 ) { (void)c; (void)t0; __sync_add_and_fetch( &ios_xp_nt.cs_contended, 1 ); }
#include "cs.c"

/* ---- the x64 bodies (x86_64 hosts) ---- */
static volatile long native_calls;
#ifdef HAVE_X64
#ifdef __linux__
#include <sys/syscall.h>
#include <asm/prctl.h>
#endif
LONG host_sync_x64_off;
/* the critical-section bodies read GetCurrentThreadId() from %gs:0x48, as on
 * Windows: give each thread a %gs block with the id there (Linux only; glibc
 * does not use %gs on x86_64) */
static __thread ULONG_PTR x64_teb[16];
static __thread int x64_gs_set;
static int cs_x64 = 1;
static void x64_self( ULONG tid )
{
    x64_teb[0x48 / sizeof(ULONG_PTR)] = tid;
#ifdef __linux__
    if (!x64_gs_set && syscall( SYS_arch_prctl, ARCH_SET_GS, (unsigned long)x64_teb )) cs_x64 = 0;
#else
    cs_x64 = 0;
#endif
    x64_gs_set = 1;
}
#define MS __attribute__((ms_abi))
extern MS NTSTATUS x64_RtlEnterCriticalSection( RTL_CRITICAL_SECTION *c );
extern MS BOOL x64_RtlTryEnterCriticalSection( RTL_CRITICAL_SECTION *c );
extern MS NTSTATUS x64_RtlLeaveCriticalSection( RTL_CRITICAL_SECTION *c );
extern MS NTSTATUS x64_RtlLeaveCriticalSection_race( RTL_CRITICAL_SECTION *c );
MS NTSTATUS host_RtlEnterCriticalSection( RTL_CRITICAL_SECTION *c ) { __sync_add_and_fetch( &native_calls, 1 ); return RtlEnterCriticalSection( c ); }
MS BOOL host_RtlTryEnterCriticalSection( RTL_CRITICAL_SECTION *c ) { __sync_add_and_fetch( &native_calls, 1 ); return RtlTryEnterCriticalSection( c ); }
MS NTSTATUS host_RtlLeaveCriticalSection( RTL_CRITICAL_SECTION *c ) { __sync_add_and_fetch( &native_calls, 1 ); return RtlLeaveCriticalSection( c ); }
extern MS BOOLEAN x64_RtlTryAcquireSRWLockExclusive( RTL_SRWLOCK *l );
extern MS BOOLEAN x64_RtlTryAcquireSRWLockShared( RTL_SRWLOCK *l );
extern MS void x64_RtlAcquireSRWLockExclusive( RTL_SRWLOCK *l );
extern MS void x64_RtlAcquireSRWLockShared( RTL_SRWLOCK *l );
extern MS void x64_RtlReleaseSRWLockExclusive( RTL_SRWLOCK *l );
extern MS void x64_RtlReleaseSRWLockShared( RTL_SRWLOCK *l );
MS BOOLEAN host_RtlTryAcquireSRWLockExclusive( RTL_SRWLOCK *l ) { __sync_add_and_fetch( &native_calls, 1 ); return RtlTryAcquireSRWLockExclusive( l ); }
MS BOOLEAN host_RtlTryAcquireSRWLockShared( RTL_SRWLOCK *l ) { __sync_add_and_fetch( &native_calls, 1 ); return RtlTryAcquireSRWLockShared( l ); }
MS void host_RtlAcquireSRWLockExclusive( RTL_SRWLOCK *l ) { __sync_add_and_fetch( &native_calls, 1 ); RtlAcquireSRWLockExclusive( l ); }
MS void host_RtlAcquireSRWLockShared( RTL_SRWLOCK *l ) { __sync_add_and_fetch( &native_calls, 1 ); RtlAcquireSRWLockShared( l ); }
MS void host_RtlReleaseSRWLockExclusive( RTL_SRWLOCK *l ) { __sync_add_and_fetch( &native_calls, 1 ); RtlReleaseSRWLockExclusive( l ); }
MS void host_RtlReleaseSRWLockShared( RTL_SRWLOCK *l ) { __sync_add_and_fetch( &native_calls, 1 ); RtlReleaseSRWLockShared( l ); }
MS void host_RtlWakeAddressSingle( const void *a ) { RtlWakeAddressSingle( a ); }
MS void host_RtlWakeAddressAll( const void *a ) { RtlWakeAddressAll( a ); }
#include "x64.inc"
#define VIAS 2
#else
#define VIAS 1
static int cs_x64 = 0;
#endif

static BOOLEAN try_x( RTL_SRWLOCK *l, int via )
{
#ifdef HAVE_X64
    if (via) return x64_RtlTryAcquireSRWLockExclusive( l );
#endif
    return RtlTryAcquireSRWLockExclusive( l );
}
static BOOLEAN try_s( RTL_SRWLOCK *l, int via )
{
#ifdef HAVE_X64
    if (via) return x64_RtlTryAcquireSRWLockShared( l );
#endif
    return RtlTryAcquireSRWLockShared( l );
}
static void acq_x( RTL_SRWLOCK *l, int via )
{
#ifdef HAVE_X64
    if (via) { x64_RtlAcquireSRWLockExclusive( l ); return; }
#endif
    RtlAcquireSRWLockExclusive( l );
}
static void acq_s( RTL_SRWLOCK *l, int via )
{
#ifdef HAVE_X64
    if (via) { x64_RtlAcquireSRWLockShared( l ); return; }
#endif
    RtlAcquireSRWLockShared( l );
}
static void rel_x( RTL_SRWLOCK *l, int via )
{
#ifdef HAVE_X64
    if (via) { x64_RtlReleaseSRWLockExclusive( l ); return; }
#endif
    RtlReleaseSRWLockExclusive( l );
}
static void rel_s( RTL_SRWLOCK *l, int via )
{
#ifdef HAVE_X64
    if (via) { x64_RtlReleaseSRWLockShared( l ); return; }
#endif
    RtlReleaseSRWLockShared( l );
}
static unsigned word( RTL_SRWLOCK *l ) { return *(volatile unsigned *)l; }

static void self( int tid )
{
    teb.ClientId.UniqueThread = ULongToHandle( tid );
#ifdef HAVE_X64
    x64_self( tid );
#endif
}
static unsigned rnd( unsigned long long *s ) { *s = *s * 6364136223846793005ull + 1442695040888963407ull; return (unsigned)(*s >> 33); }

/* 1. uncontended traffic: wake calls made */
static void uncontended( int via )
{
    RTL_SRWLOCK l = { 0 };
    long long wa0 = ios_xp_nt.woa_wake_all, ws0 = ios_xp_nt.woa_wake_single;
    long n0 = native_calls;
    int i;
    for (i = 0; i < 1000; i++)
    {
        acq_x( &l, via ); rel_x( &l, via );
        acq_s( &l, via ); acq_s( &l, via ); rel_s( &l, via ); rel_s( &l, via );
        if (try_x( &l, via )) rel_x( &l, via );
        if (try_s( &l, via )) rel_s( &l, via );
    }
    printf( "uncontended via=%d wake_all=%lld wake_single=%lld native=%ld word=%08x\n", via,
            ios_xp_nt.woa_wake_all - wa0, ios_xp_nt.woa_wake_single - ws0, native_calls - n0, word( &l ) );
}

/* 2. single-threaded sequences: results and lock words (no call can block) */
static void sequences( int via )
{
    RTL_SRWLOCK l[3] = { { 0 } };
    int excl[3] = { 0 }, shared[3] = { 0 };
    unsigned long long s = 12345, h = 1469598103934665603ull;
    int i;
    for (i = 0; i < 200000; i++)
    {
        int k = rnd( &s ) % 3, op = rnd( &s ) % 6, r = -1;
        switch (op)
        {
        case 0: r = try_x( &l[k], via ); if (r) excl[k]++; break;
        case 1: r = try_s( &l[k], via ); if (r) shared[k]++; break;
        case 2: if (excl[k]) { rel_x( &l[k], via ); excl[k]--; r = 2; } break;
        case 3: if (shared[k]) { rel_s( &l[k], via ); shared[k]--; r = 3; } break;
        case 4: if (!excl[k] && !shared[k]) { acq_x( &l[k], via ); excl[k]++; r = 4; } break;
        case 5: if (!excl[k] && shared[k] < 3) { acq_s( &l[k], via ); shared[k]++; r = 5; } break;
        }
        h = (h ^ (unsigned)(r + 7)) * 1099511628211ull;
        h = (h ^ word( &l[k] )) * 1099511628211ull;
    }
    printf( "sequences via=%d hash=%016llx\n", via, h );
}

/* 3. threads hammering four locks */
#define NLOCK 4
static RTL_SRWLOCK hl[NLOCK];
static volatile long hx[NLOCK], hs[NLOCK], violations, done_threads;
static long plain[NLOCK], excl_sections[NLOCK];
static int hammer_vias, hammer_iters;
static void *hammer_thread( void *arg )
{
    int t = (int)(ULONG_PTR)arg, i;
    unsigned long long s = 0x9e3779b97f4a7c15ull * (t + 1);
    self( t );
    for (i = 0; i < hammer_iters; i++)
    {
        int k = rnd( &s ) % NLOCK, op = rnd( &s ) % 8, via = hammer_vias > 1 ? (int)(rnd( &s ) & 1) : 0;
        int spin = rnd( &s ) % 4;
        if (op < 3 || op == 6)   /* exclusive: blocking, or try in a loop */
        {
            if (op == 6) { while (!try_x( &hl[k], via )) sched_yield(); }
            else acq_x( &hl[k], via );
            if (__sync_add_and_fetch( &hx[k], 1 ) != 1 || hs[k]) __sync_add_and_fetch( &violations, 1 );
            plain[k]++;
            __sync_add_and_fetch( &excl_sections[k], 1 );
            if (!spin) sched_yield();
            __sync_sub_and_fetch( &hx[k], 1 );
            rel_x( &hl[k], hammer_vias > 1 ? (int)(rnd( &s ) & 1) : 0 );
        }
        else   /* shared: blocking, or try in a loop */
        {
            if (op == 7) { while (!try_s( &hl[k], via )) sched_yield(); }
            else acq_s( &hl[k], via );
            __sync_add_and_fetch( &hs[k], 1 );
            if (hx[k]) __sync_add_and_fetch( &violations, 1 );
            if (!spin) sched_yield();
            __sync_sub_and_fetch( &hs[k], 1 );
            rel_s( &hl[k], hammer_vias > 1 ? (int)(rnd( &s ) & 1) : 0 );
        }
    }
    __sync_add_and_fetch( &done_threads, 1 );
    return NULL;
}
static int wait_done( volatile long *done, long want, int seconds )
{
    struct timespec t0, t1;
    clock_gettime( CLOCK_MONOTONIC, &t0 );
    while (*done < want)
    {
        usleep( 2000 );
        clock_gettime( CLOCK_MONOTONIC, &t1 );
        if (t1.tv_sec - t0.tv_sec > seconds) return 0;
    }
    return 1;
}
static void hammer( int nthreads, int iters, int vias )
{
    pthread_t th[MAXT];
    long long wa0 = ios_xp_nt.woa_wake_all, ws0 = ios_xp_nt.woa_wake_single, w0 = ios_xp_nt.woa_waits;
    int i, ok = 1;
    hammer_vias = vias; hammer_iters = iters; done_threads = 0;
    for (i = 0; i < nthreads; i++) pthread_create( &th[i], NULL, hammer_thread, (void *)(ULONG_PTR)(i + 2) );
    if (!wait_done( &done_threads, nthreads, 240 ))
    {
        printf( "hammer HANG (a lost wakeup) words %08x %08x %08x %08x\n", word( &hl[0] ), word( &hl[1] ), word( &hl[2] ), word( &hl[3] ) );
        fflush( stdout );
        _exit( 3 );
    }
    for (i = 0; i < nthreads; i++) pthread_join( th[i], NULL );
    for (i = 0; i < NLOCK; i++) if (plain[i] != excl_sections[i] || word( &hl[i] )) ok = 0;
    printf( "hammer threads=%d vias=%d %s violations=%ld errs=%ld waits=%lld wake_all=%lld wake_single=%lld\n",
            nthreads, vias, ok ? "ok" : "BROKEN", violations, errs, ios_xp_nt.woa_waits - w0,
            ios_xp_nt.woa_wake_all - wa0, ios_xp_nt.woa_wake_single - ws0 );
}

/* 4. condition variables, exclusive and shared */
static RTL_SRWLOCK cl;
static RTL_CONDITION_VARIABLE cv_items, cv_gen;
static int queue_n, produced, consumed, generation;
static volatile long cv_done;
#define ITEMS 20000
static void *producer( void *arg )
{
    int i;
    self( (int)(ULONG_PTR)arg );
    for (i = 0; i < ITEMS; i++)
    {
        RtlAcquireSRWLockExclusive( &cl );
        queue_n++; produced++;
        if (!(i % 64)) generation++;
        RtlReleaseSRWLockExclusive( &cl );
        RtlWakeConditionVariable( &cv_items );
        if (!(i % 64)) RtlWakeAllConditionVariable( &cv_gen );
    }
    RtlAcquireSRWLockExclusive( &cl );
    generation = -1;
    RtlReleaseSRWLockExclusive( &cl );
    RtlWakeAllConditionVariable( &cv_gen );
    __sync_add_and_fetch( &cv_done, 1 );
    return NULL;
}
static void *consumer( void *arg )
{
    self( (int)(ULONG_PTR)arg );
    RtlAcquireSRWLockExclusive( &cl );
    for (;;)
    {
        while (!queue_n && consumed < 2 * ITEMS) RtlSleepConditionVariableSRW( &cv_items, &cl, NULL, 0 );
        if (consumed >= 2 * ITEMS) break;
        queue_n--; consumed++;
        if (consumed == 2 * ITEMS) { RtlWakeAllConditionVariable( &cv_items ); break; }
    }
    RtlReleaseSRWLockExclusive( &cl );
    __sync_add_and_fetch( &cv_done, 1 );
    return NULL;
}
static void *watcher( void *arg )
{
    int seen = 0, changes = 0;
    self( (int)(ULONG_PTR)arg );
    RtlAcquireSRWLockShared( &cl );
    while (generation != -1)
    {
        if (generation != seen) { seen = generation; changes++; }
        RtlSleepConditionVariableSRW( &cv_gen, &cl, NULL, RTL_CONDITION_VARIABLE_LOCKMODE_SHARED );
    }
    RtlReleaseSRWLockShared( &cl );
    (void)changes;
    __sync_add_and_fetch( &cv_done, 1 );
    return NULL;
}
static void condvars(void)
{
    pthread_t th[8];
    int i;
    for (i = 0; i < 2; i++) pthread_create( &th[i], NULL, producer, (void *)(ULONG_PTR)(10 + i) );
    for (i = 2; i < 5; i++) pthread_create( &th[i], NULL, consumer, (void *)(ULONG_PTR)(10 + i) );
    for (i = 5; i < 8; i++) pthread_create( &th[i], NULL, watcher, (void *)(ULONG_PTR)(10 + i) );
    if (!wait_done( &cv_done, 8, 240 ))
    {
        printf( "condvars HANG produced=%d consumed=%d word=%08x\n", produced, consumed, word( &cl ) );
        fflush( stdout );
        _exit( 3 );
    }
    for (i = 0; i < 8; i++) pthread_join( th[i], NULL );
    printf( "condvars %s produced=%d consumed=%d word=%08x errs=%ld\n",
            produced == 2 * ITEMS && consumed == 2 * ITEMS && !word( &cl ) ? "ok" : "BROKEN",
            produced, consumed, word( &cl ), errs );
}

/* 5. shared waiters parked behind an exclusive owner and an exclusive waiter */
static RTL_SRWLOCK pl;
static volatile long parked_in, parked_done, order_x_before_s, x_got;
static void *park_shared( void *arg )
{
    self( (int)(ULONG_PTR)arg );
    __sync_add_and_fetch( &parked_in, 1 );
    RtlAcquireSRWLockShared( &pl );
    if (x_got) __sync_add_and_fetch( &order_x_before_s, 1 );
    RtlReleaseSRWLockShared( &pl );
    __sync_add_and_fetch( &parked_done, 1 );
    return NULL;
}
static void *park_excl( void *arg )
{
    self( (int)(ULONG_PTR)arg );
    __sync_add_and_fetch( &parked_in, 1 );
    RtlAcquireSRWLockExclusive( &pl );
    x_got = 1;
    usleep( 20000 );
    RtlReleaseSRWLockExclusive( &pl );
    __sync_add_and_fetch( &parked_done, 1 );
    return NULL;
}
static void parked(void)
{
    pthread_t th[6];
    long long wa0 = ios_xp_nt.woa_wake_all;
    unsigned w_parked;
    int i;
    self( 30 );
    RtlAcquireSRWLockExclusive( &pl );
    pthread_create( &th[0], NULL, park_excl, (void *)(ULONG_PTR)20 );
    usleep( 50000 );
    for (i = 1; i < 6; i++) pthread_create( &th[i], NULL, park_shared, (void *)(ULONG_PTR)(20 + i) );
    while (parked_in < 6) usleep( 1000 );
    usleep( 100000 );
    w_parked = word( &pl );
    RtlReleaseSRWLockExclusive( &pl );
    if (!wait_done( &parked_done, 6, 60 ))
    {
        printf( "parked HANG word=%08x\n", word( &pl ) );
        fflush( stdout );
        _exit( 3 );
    }
    for (i = 0; i < 6; i++) pthread_join( th[i], NULL );
    printf( "parked ok word_while_parked=%08x word=%08x x_before_s=%ld wake_all=%lld errs=%ld\n",
            w_parked, word( &pl ), order_x_before_s, ios_xp_nt.woa_wake_all - wa0, errs );
}

#ifdef HAVE_X64
static void bodies(void)
{
    static const struct { const char *name; const void *fn; } f[] = {
        { "RtlTryAcquireSRWLockExclusive", (const void *)x64_RtlTryAcquireSRWLockExclusive },
        { "RtlTryAcquireSRWLockShared", (const void *)x64_RtlTryAcquireSRWLockShared },
        { "RtlAcquireSRWLockExclusive", (const void *)x64_RtlAcquireSRWLockExclusive },
        { "RtlAcquireSRWLockShared", (const void *)x64_RtlAcquireSRWLockShared },
        { "RtlReleaseSRWLockExclusive", (const void *)x64_RtlReleaseSRWLockExclusive },
        { "RtlReleaseSRWLockShared", (const void *)x64_RtlReleaseSRWLockShared },
        { "RtlEnterCriticalSection", (const void *)x64_RtlEnterCriticalSection },
        { "RtlTryEnterCriticalSection", (const void *)x64_RtlTryEnterCriticalSection },
        { "RtlLeaveCriticalSection", (const void *)x64_RtlLeaveCriticalSection },
    };
    unsigned i, k;
    for (i = 0; i < ARRAY_SIZE(f); i++)
    {
        printf( "body %s ", f[i].name );
        for (k = 0; k < 96; k++) printf( "%02x", ((const unsigned char *)f[i].fn)[k] );
        printf( "\n" );
    }
}
static void off_switch(void)
{
    RTL_SRWLOCK l = { 0 };
    long n0 = native_calls;
    host_sync_x64_off = 1;
    acq_x( &l, 1 ); rel_x( &l, 1 ); acq_s( &l, 1 ); rel_s( &l, 1 );
    if (try_x( &l, 1 )) rel_x( &l, 1 );
    if (try_s( &l, 1 )) rel_s( &l, 1 );
    host_sync_x64_off = 0;
    printf( "x64-off native=%ld word=%08x\n", native_calls - n0, word( &l ) );
}
#endif

/* 6. critical sections: Wine's code, and the x64 bodies where they can run */
static void cs_init( RTL_CRITICAL_SECTION *c, ULONG_PTR spin )
{
    memset( c, 0, sizeof(*c) );
    c->DebugInfo = no_debug_info_marker;
    c->LockCount = -1;
    c->SpinCount = spin;
}
static int cs_free( const RTL_CRITICAL_SECTION *c )
{
    return c->LockCount == -1 && !c->RecursionCount && !c->OwningThread && !c->LockSemaphore;
}
static NTSTATUS cs_enter( RTL_CRITICAL_SECTION *c, int via )
{
#ifdef HAVE_X64
    if (via) return x64_RtlEnterCriticalSection( c );
#endif
    return RtlEnterCriticalSection( c );
}
static BOOL cs_try( RTL_CRITICAL_SECTION *c, int via )
{
#ifdef HAVE_X64
    if (via) return x64_RtlTryEnterCriticalSection( c );
#endif
    return RtlTryEnterCriticalSection( c );
}
static NTSTATUS cs_leave( RTL_CRITICAL_SECTION *c, int via )
{
#ifdef HAVE_X64
    if (via) return x64_RtlLeaveCriticalSection( c );
#endif
    return RtlLeaveCriticalSection( c );
}

static void cs_uncontended( int via )
{
    RTL_CRITICAL_SECTION c;
    long n0 = native_calls;
    long long w0 = ios_xp_nt.cs_wakes;
    int i, bad = 0;
    cs_init( &c, 0 );
    for (i = 0; i < 1000; i++)
    {
        if (cs_enter( &c, via ) || c.RecursionCount != 1 || c.LockCount != 0 ||
            HandleToULong( c.OwningThread ) != GetCurrentThreadId()) bad++;
        if (cs_enter( &c, via ) || c.RecursionCount != 2 || c.LockCount != 1) bad++;
        if (cs_leave( &c, via ) || c.RecursionCount != 1 || c.LockCount != 0) bad++;
        if (cs_leave( &c, via ) || !cs_free( &c )) bad++;
        if (cs_try( &c, via ) != TRUE || cs_try( &c, via ) != TRUE || c.RecursionCount != 2 || c.LockCount != 1) bad++;
        cs_leave( &c, via );
        cs_leave( &c, via );
        if (!cs_free( &c )) bad++;
    }
    printf( "cs-uncontended via=%d bad=%d native=%ld wakes=%lld errs=%ld\n", via, bad, native_calls - n0,
            ios_xp_nt.cs_wakes - w0, errs );
}

/* two thread ids taking turns on one thread: try fails on the other's section,
 * enter only where it cannot block; results and every field hashed */
static void cs_sequences( int via )
{
    RTL_CRITICAL_SECTION c[3];
    int held[3][2] = { { 0 } }, i, me = 0;
    unsigned long long s = 777, h = 1469598103934665603ull;
    for (i = 0; i < 3; i++) cs_init( &c[i], i == 2 ? 4000 : 0 );
    self( 1 );
    for (i = 0; i < 200000; i++)
    {
        int k = rnd( &s ) % 3, op = rnd( &s ) % 5, r = -1;
        switch (op)
        {
        case 0: me = !me; self( me + 1 ); r = 9; break;
        case 1: if (!held[k][!me]) { r = cs_enter( &c[k], via ); held[k][me]++; } break;
        case 2: r = cs_try( &c[k], via ); if (r) held[k][me]++; break;
        default: if (held[k][me]) { r = cs_leave( &c[k], via ); held[k][me]--; } break;
        }
        h = (h ^ (unsigned)(r + 7)) * 1099511628211ull;
        h = (h ^ (unsigned)c[k].LockCount) * 1099511628211ull;
        h = (h ^ (unsigned)c[k].RecursionCount) * 1099511628211ull;
        h = (h ^ HandleToULong( c[k].OwningThread )) * 1099511628211ull;
    }
    self( 1 );
    printf( "cs-sequences via=%d hash=%016llx errs=%ld\n", via, h, errs );
}

#define NCS 4
static RTL_CRITICAL_SECTION hcs[NCS];
static volatile long cs_inside[NCS], cs_violations, cs_done;
static long cs_plain[NCS], cs_count[NCS];
static int cs_vias, cs_iters;
static void *cs_thread( void *arg )
{
    int t = (int)(ULONG_PTR)arg, i, j;
    unsigned long long s = 0x2545f4914f6cdd1dull * (t + 3);
    self( t );
    for (i = 0; i < cs_iters; i++)
    {
        int k = rnd( &s ) % NCS, op = rnd( &s ) % 4, spin = rnd( &s ) % 4, nest = rnd( &s ) % 3;
        int via = cs_vias > 1 ? (int)(rnd( &s ) & 1) : 0;
        if (op == 3) { while (!cs_try( &hcs[k], via )) sched_yield(); }
        else cs_enter( &hcs[k], via );
        if (__sync_add_and_fetch( &cs_inside[k], 1 ) != 1) __sync_add_and_fetch( &cs_violations, 1 );
        for (j = 0; j < nest; j++) cs_enter( &hcs[k], cs_vias > 1 ? (int)(rnd( &s ) & 1) : 0 );
        cs_plain[k]++;
        __sync_add_and_fetch( &cs_count[k], 1 );
        if (!spin) sched_yield();
        for (j = 0; j < nest; j++) cs_leave( &hcs[k], cs_vias > 1 ? (int)(rnd( &s ) & 1) : 0 );
        __sync_sub_and_fetch( &cs_inside[k], 1 );
        cs_leave( &hcs[k], cs_vias > 1 ? (int)(rnd( &s ) & 1) : 0 );
    }
    __sync_add_and_fetch( &cs_done, 1 );
    return NULL;
}
static void cs_hammer( int nthreads, int iters, int vias )
{
    pthread_t th[MAXT];
    long long w0 = ios_xp_nt.cs_contended, k0 = ios_xp_nt.cs_wakes;
    int i, ok = 1;
    for (i = 0; i < NCS; i++) { cs_init( &hcs[i], (i & 1) ? 4000 : 0 ); cs_plain[i] = cs_count[i] = 0; }
    cs_vias = vias; cs_iters = iters; cs_done = 0; cs_violations = 0;
    for (i = 0; i < nthreads; i++) pthread_create( &th[i], NULL, cs_thread, (void *)(ULONG_PTR)(i + 2) );
    if (!wait_done( &cs_done, nthreads, 240 ))
    {
        printf( "cs-hammer HANG (a lost wakeup) LockCount %d %d %d %d\n", (int)hcs[0].LockCount, (int)hcs[1].LockCount,
                (int)hcs[2].LockCount, (int)hcs[3].LockCount );
        fflush( stdout );
        _exit( 3 );
    }
    for (i = 0; i < nthreads; i++) pthread_join( th[i], NULL );
    for (i = 0; i < NCS; i++) if (cs_plain[i] != cs_count[i] || !cs_free( &hcs[i] )) ok = 0;
    printf( "cs-hammer threads=%d vias=%d %s violations=%ld errs=%ld waits=%lld wakes=%lld\n", nthreads, vias,
            ok ? "ok" : "BROKEN", cs_violations, errs, ios_xp_nt.cs_contended - w0, ios_xp_nt.cs_wakes - k0 );
}

#ifdef HAVE_X64
static void cs_off_switch(void)
{
    RTL_CRITICAL_SECTION c;
    long n0 = native_calls;
    cs_init( &c, 0 );
    host_sync_x64_off = 1;
    cs_enter( &c, 1 );
    cs_leave( &c, 1 );
    if (cs_try( &c, 1 )) cs_leave( &c, 1 );
    host_sync_x64_off = 0;
    printf( "cs-x64-off native=%ld free=%d\n", native_calls - n0, cs_free( &c ) );
}
/* the last leave meets a waiter that registers between its check and its
 * release (the body with one `lock incl` of LockCount injected just before its
 * compare-exchange): the fields go back and the native leave hands over */
static void cs_leave_race(void)
{
    RTL_CRITICAL_SECTION c;
    long n0;
    long long k0 = ios_xp_nt.cs_wakes;
    NTSTATUS st;
    int handed;
    cs_init( &c, 0 );
    x64_RtlEnterCriticalSection( &c );
    n0 = native_calls;
    st = x64_RtlLeaveCriticalSection_race( &c );
    handed = c.LockCount == 0 && !c.RecursionCount && !c.OwningThread && (ULONG_PTR)c.LockSemaphore == 1;
    RtlpWaitForCriticalSection( &c );   /* now be that waiter, as RtlEnterCriticalSection is after its wait */
    c.OwningThread = ULongToHandle( GetCurrentThreadId() );
    c.RecursionCount = 1;
    RtlLeaveCriticalSection( &c );
    printf( "cs-leave-race st=%d handed=%d native=%ld wakes=%lld free=%d\n", (int)st, handed, native_calls - n0,
            ios_xp_nt.cs_wakes - k0, cs_free( &c ) );
}
#endif

int main( int argc, char **argv )
{
    int via;
    (void)argc; (void)argv;
    alerts_init();
    self( 1 );
    setvbuf( stdout, NULL, _IOLBF, 0 );
    for (via = 0; via < VIAS; via++) uncontended( via );
    for (via = 0; via < VIAS; via++) sequences( via );
#ifdef HAVE_X64
    bodies();
    off_switch();
#endif
    hammer( 6, 60000, 1 );
    if (VIAS > 1) hammer( 6, 60000, 2 );
    condvars();
    parked();
    if (VIAS > 1 && !cs_x64) printf( "cs-x64 unavailable (no arch_prctl %%gs here)\n" );
    for (via = 0; via < (VIAS > 1 && cs_x64 ? 2 : 1); via++) cs_uncontended( via );
    for (via = 0; via < (VIAS > 1 && cs_x64 ? 2 : 1); via++) cs_sequences( via );
#ifdef HAVE_X64
    if (cs_x64) { cs_off_switch(); cs_leave_race(); }
#endif
    cs_hammer( 6, 40000, 1 );
    if (VIAS > 1 && cs_x64) cs_hammer( 6, 40000, 2 );
    printf( "errs=%ld\n", errs );
    return 0;
}
'''


def region(sync_c):
    a = sync_c.index('struct srw_lock\n{')
    b = sync_c.index('/*************************************************************************\n *           RtlInitializeSListHead')
    return sync_c[a:b]


# Wine's critical sections, as sync.c has them (the patch leaves them alone)
CS_PARTS = ('static void *no_debug_info_marker', 'static BOOL crit_section_has_debuginfo(',
            'static const char *crit_section_get_name(', 'static inline HANDLE get_semaphore(',
            'static inline NTSTATUS wait_semaphore(', 'NTSTATUS WINAPI RtlpWaitForCriticalSection(',
            'NTSTATUS WINAPI RtlpUnWaitCriticalSection(', 'NTSTATUS WINAPI RtlEnterCriticalSection(',
            'BOOL WINAPI RtlTryEnterCriticalSection(', 'NTSTATUS WINAPI RtlLeaveCriticalSection(')


def cs_region(sync_c):
    out = []
    for sig in CS_PARTS:
        i = sync_c.index(sig)
        out.append(sync_c[i:sync_c.index('\n', i) + 1] if sig.startswith('static void *')
                   else sync_c[i:sync_c.index('\n}\n', i) + 3])
    return '\n'.join(out)


check(cs_region(orig['sync.c']) == cs_region(patched['sync.c']),
      'the critical-section C code in sync.c is left as it is (the x64 bodies come on top)')
RACE = ('lock cmpxchgl %r8d,8(%rcx)', 'lock incl 8(%rcx)\n\tlock cmpxchgl %r8d,8(%rcx)')


def x64_inc(src):
    """The x64 bodies as host assembly: the same instructions, the targets renamed;
    plus RtlLeaveCriticalSection_race, the leave with a waiter's registration
    injected right before its release."""
    lines = []
    pfx = '_' if platform.system() == 'Darwin' else ''
    bodies = c_string_bodies(src)
    if bodies['RtlLeaveCriticalSection'].count(RACE[0]) == 1:
        bodies['RtlLeaveCriticalSection_race'] = bodies['RtlLeaveCriticalSection'].replace(*RACE)
    for name, text in bodies.items():
        text = re.sub(r'"#(\w+)"', lambda m: pfx + 'host_' + m.group(1), text)
        text = text.replace('madeira_sync_x64_off(%rip)', pfx + 'host_sync_x64_off(%rip)')
        sym = pfx + 'x64_' + name
        body = '.text\n.balign 16\n.globl %s\n%s%s:\n\t%s\n' % (
            sym, '' if pfx else '.type %s,@function\n' % sym, sym, text)
        lines.append('__asm__( ' + ' '.join('"%s\\n"' % l.replace('\\', '\\\\').replace('"', '\\"')
                                             for l in body.split('\n')) + ' );')
    return '\n'.join(lines) + '\n'


x86 = platform.machine().lower() in ('x86_64', 'amd64')
results = {}
with tempfile.TemporaryDirectory(prefix='madeira-ntdll-sync-run-') as d:
    folder = Path(d)
    (folder / 'harness.c').write_text(HARNESS)
    for kind, text in (('stock', orig['sync.c']), ('patched', patched['sync.c'])):
        sub = folder / kind
        sub.mkdir()
        (sub / 'region.c').write_text(region(text))
        (sub / 'cs.c').write_text(cs_region(text))
        (sub / 'x64.inc').write_text(x64_inc(nts.SYNC_X64_C) if kind == 'patched' else '')
        exe = sub / 'run'
        flags = ['-std=gnu11', '-O2', '-pthread', '-w', '-I', str(sub), '-I', str(root / 'wine/include')]
        if kind == 'patched' and x86:
            flags.append('-DHAVE_X64')
        r = subprocess.run([cc] + flags + [str(folder / 'harness.c'), '-o', str(exe)], capture_output=True, text=True)
        if r.returncode:
            print(r.stderr[-3000:])
        check(r.returncode == 0, 'the %s SRW, condition variable, WaitOnAddress and critical-section code compiles '
              'on the host' % kind)
        if r.returncode:
            continue
        r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=900)
        print('\n'.join('    ' + l for l in r.stdout.strip().split('\n')))
        check(r.returncode == 0, 'the %s model runs to the end (exit %d)' % (kind, r.returncode))
        results[kind] = r.stdout

stock_out, patched_out = results.get('stock', ''), results.get('patched', '')


def grab(out, pattern):
    m = re.search(pattern, out)
    return m.groups() if m else None


u0 = grab(stock_out, r'(?m)^uncontended via=0 wake_all=(\d+) wake_single=(\d+) native=\d+ word=(\w+)')
u1 = grab(patched_out, r'(?m)^uncontended via=0 wake_all=(\d+) wake_single=(\d+) native=\d+ word=(\w+)')
check(u0 == ('2000', '2000', '00000000'),
      'stock: 2000 RtlWakeAddressAll and 2000 RtlWakeAddressSingle calls for 1000 uncontended rounds (%s)' % (u0,))
check(u1 == ('0', '0', '00000000'), 'patched: no RtlWakeAddress* call for the same uncontended rounds (%s)' % (u1,))
q0 = grab(stock_out, r'(?m)^sequences via=0 hash=(\w+)')
q1 = grab(patched_out, r'(?m)^sequences via=0 hash=(\w+)')
check(q0 and q0 == q1, 'single-threaded try/acquire/release sequences: the same results and lock words as stock')
for kind, out in (('stock', stock_out), ('patched', patched_out)):
    hs = re.findall(r'(?m)^hammer threads=\d+ vias=(\d) (\w+) violations=(\d+) errs=(\d+) waits=(\d+)', out)
    check(hs and all(h[1] == 'ok' and h[2] == '0' and h[3] == '0' for h in hs) and int(hs[0][4]) > 0,
          '%s: 6 threads x 60000 lock operations keep mutual exclusion, lose no wakeup, leave every word 0 '
          '(and do block: %s waits)' % (kind, hs[0][4] if hs else '?'))
    check(re.search(r'condvars ok produced=40000 consumed=40000 word=00000000 errs=0', out) is not None,
          '%s: condition variables in exclusive and shared mode deliver every item' % kind)
    p = grab(out, r'parked ok word_while_parked=(\w+) word=(\w+) x_before_s=(\d+) wake_all=(\d+) errs=(\d+)')
    check(p is not None and p[1] == '00000000' and p[2] == '5' and p[4] == '0',
          '%s: five shared waiters parked behind an exclusive owner and waiter all wake, after the writer (%s)'
          % (kind, p,))
p = grab(patched_out, r'parked ok word_while_parked=(\w+)')
check(p is not None and int(p[0], 16) & 0x8000 and (int(p[0], 16) & 0x7ffe) == 2,
      'patched: while they wait the word carries the parked flag and one exclusive waiter (%s)' % (p,))
if x86 and 'patched' in results:
    u = grab(patched_out, r'(?m)^uncontended via=1 wake_all=(\d+) wake_single=(\d+) native=(\d+) word=(\w+)')
    check(u == ('0', '0', '0', '00000000'),
          'x64 bodies: uncontended rounds never leave the x64 code (no native call, no wake) (%s)' % (u,))
    q = grab(patched_out, r'(?m)^sequences via=1 hash=(\w+)')
    check(q and q == q0, 'x64 bodies: the same sequence results and lock words as stock')
    h = grab(patched_out, r'(?m)^hammer threads=6 vias=2 (\w+) violations=(\d+) errs=(\d+)')
    check(h == ('ok', '0', '0'), 'x64 bodies and native functions mixed on the same locks: exclusion kept, no lost wakeup')
    o = grab(patched_out, r'(?m)^x64-off native=(\d+) word=(\w+)')
    check(o == ('8', '00000000'), 'madeira_sync_x64_off = 1 sends every x64 call (eight here) to the native body (%s)' % (o,))
    ok = True
    for name in nts.X64_BODIES:
        m = re.search(r'body %s (\w+)' % name, patched_out)
        want, mask = nts.body_pattern(name)
        got = bytes.fromhex(m.group(1)) if m else b''
        ok = ok and len(got) >= len(want) and all(not mk or g == w for g, w, mk in zip(got, want, mask))
    check(ok, 'the host assembler encodes the bodies as --verify expects')
else:
    print('SKIP: not an x86_64 host; the x64 bodies are checked by encoding only (and by --verify in CI)')

# critical sections: Wine's code in both builds; the x64 bodies where %gs can be set
for kind, out in (('stock', stock_out), ('patched', patched_out)):
    c = grab(out, r'(?m)^cs-uncontended via=0 bad=(\d+) native=\d+ wakes=(\d+) errs=(\d+)')
    check(c == ('0', '0', '0'), '%s: critical sections, 1000 rounds of enter, nested enter, try and leave: the '
          'fields as Windows keeps them, no wake (%s)' % (kind, c,))
    h = grab(out, r'(?m)^cs-hammer threads=6 vias=1 (\w+) violations=(\d+) errs=(\d+) waits=(\d+)')
    check(h is not None and h[:3] == ('ok', '0', '0') and int(h[3]) > 0,
          '%s: 6 threads x 40000 critical-section entries (nested, try, spin count 0 and 4000) keep exclusion, '
          'lose no wakeup and leave every section free (and do wait: %s)' % (kind, h[3] if h else '?'))
cq0 = grab(stock_out, r'(?m)^cs-sequences via=0 hash=(\w+) errs=0')
cq1 = grab(patched_out, r'(?m)^cs-sequences via=0 hash=(\w+) errs=0')
check(cq0 is not None and cq0 == cq1, 'critical sections: the same results and fields in stock and patched builds')
if x86 and platform.system() == 'Linux' and 'patched' in results:
    if 'cs-x64 unavailable' in patched_out:
        print('SKIP: %gs cannot be set here (arch_prctl); the critical-section bodies are checked by encoding only')
    else:
        c = grab(patched_out, r'(?m)^cs-uncontended via=1 bad=(\d+) native=(\d+) wakes=(\d+) errs=(\d+)')
        check(c == ('0', '0', '0', '0'), 'x64 critical-section bodies: enter, nested enter, try and leave of a '
              'section nobody else wants never leave the x64 code (%s)' % (c,))
        cq = grab(patched_out, r'(?m)^cs-sequences via=1 hash=(\w+) errs=0')
        check(cq is not None and cq == cq0, 'x64 critical-section bodies: the same results and fields as Wine\'s '
              'code, a section held by the other thread id included')
        o = grab(patched_out, r'(?m)^cs-x64-off native=(\d+) free=(\d)')
        check(o == ('4', '1'), 'madeira_sync_x64_off = 1 sends the critical-section calls to the native bodies (%s)' % (o,))
        r = grab(patched_out, r'(?m)^cs-leave-race st=(-?\d+) handed=(\d) native=(\d+) wakes=(\d+) free=(\d)')
        check(r == ('0', '1', '1', '1', '1'), 'a last leave that meets a new waiter puts the fields back and the '
              'native leave hands the section over (%s)' % (r,))
        h = grab(patched_out, r'(?m)^cs-hammer threads=6 vias=2 (\w+) violations=(\d+) errs=(\d+) waits=(\d+)')
        check(h is not None and h[:3] == ('ok', '0', '0'),
              'x64 bodies and Wine\'s code mixed on the same critical sections: exclusion kept, no lost wakeup (%s)' % (h,))
else:
    print('SKIP: not an x86_64 Linux host; the critical-section bodies are checked by encoding only')

# ---- 4. --same-as / --verify ------------------------------------------------------


def synth(bodies_x64, stamp=0x6ac26b05, flag=True, marker=True, text_size=0x1100):
    """A PE32+ ARM64EC-shaped image: .text 0x1000 (ARM64EC 0x1000-0x1800, X64 0x1800-),
    .hexpthk, .rdata (exports, load config, CHPE code map, debug directory), .data."""
    img = bytearray(0x8000)
    struct.pack_into('<H', img, 0, 0x5a4d)
    struct.pack_into('<I', img, 0x3c, 0x80)
    pe = 0x80
    struct.pack_into('<IHHI', img, pe, 0x4550, 0x8664, 4, stamp)
    struct.pack_into('<H', img, pe + 20, 240)
    opt = pe + 24
    struct.pack_into('<H', img, opt, 0x20b)
    struct.pack_into('<Q', img, opt + 24, 0x180000000)
    struct.pack_into('<I', img, opt + 56, 0x8000)
    struct.pack_into('<I', img, opt + 108, 16)
    sec = opt + 240
    layout = ((b'.text', 0x1000, text_size, 0x60000020), (b'.hexpthk', 0x4000, 0x200, 0x60000020),
              (b'.rdata', 0x5000, 0x2000, 0x40000040), (b'.data', 0x7000, 0x100, 0xc0000040))
    for k, (name, va, size, ch) in enumerate(layout):
        s = sec + 40 * k
        img[s:s + 8] = name.ljust(8, b'\0')
        struct.pack_into('<IIII', img, s + 8, size, va, max(size, 0x1000) if va < 0x7000 else 0x1000, va)
        struct.pack_into('<I', img, s + 36, ch)
    names = sorted(list(nts.X64_BODIES) + ['RtlPcToFileHeader'] + ([nts.FLAG] if flag else []))
    rvas = {}
    x = 0x1800
    for n in nts.X64_BODIES:
        want, mask = nts.body_pattern(n)
        rvas[n] = x
        if bodies_x64:
            img[x:x + len(want)] = want
        else:
            img[x:x + 14] = nts.FFS + b'\0' * 4
        x += (len(want) + 15) & ~15
    # RtlPcToFileHeader: a fast-forward sequence to a body at 0x1100 in .text
    rvas['RtlPcToFileHeader'] = 0x4000
    img[0x4000:0x400e] = nts.FFS + struct.pack('<i', 0x1100 - (0x4000 + 14))
    if flag:
        rvas[nts.FLAG] = 0x7010
    e = 0x5000
    struct.pack_into('<IIIIIIIIII', img, e, 0, 0, 0, 0, 1, len(names), len(names), e + 0x40, e + 0x80, e + 0xc0)
    for k, n in enumerate(names):
        struct.pack_into('<I', img, e + 0x40 + 4 * k, rvas[n])
        struct.pack_into('<I', img, e + 0x80 + 4 * k, e + 0x100 + 48 * k)
        struct.pack_into('<H', img, e + 0xc0 + 2 * k, k)
        img[e + 0x100 + 48 * k:e + 0x100 + 48 * k + len(n)] = n.encode()
    struct.pack_into('<II', img, opt + 112, e, 0x600)
    cfg = 0x5800
    struct.pack_into('<II', img, opt + 112 + 10 * 8, cfg, 0x140)
    struct.pack_into('<Q', img, cfg + 0xC8, 0x180000000 + 0x5a00)
    struct.pack_into('<III', img, 0x5a00, 1, 0x5b00, 2)
    struct.pack_into('<IIII', img, 0x5b00, 0x1000 | 1, 0x800, 0x1800 | 2, 0x3000)
    dbg = 0x5c00
    struct.pack_into('<II', img, opt + 112 + 6 * 8, dbg, 28)
    struct.pack_into('<II', img, dbg, 0, stamp)
    if marker:
        img[0x6000:0x6000 + len(nts.MARKER)] = nts.MARKER
    return bytes(img)


with tempfile.TemporaryDirectory(prefix='madeira-ntdll-sync-pe-') as d:
    folder = Path(d)

    def run(*args):
        return subprocess.run([sys.executable, str(script)] + [str(a) for a in args], capture_output=True, text=True)

    a, b, c = folder / 'a.dll', folder / 'b.dll', folder / 'c.dll'
    a.write_bytes(synth(False, stamp=0x11111111))
    b.write_bytes(synth(False, stamp=0x22222222))
    c.write_bytes(synth(False, stamp=0x11111111, marker=False))
    r1, r2 = run('--same-as', a, b), run('--same-as', a, c)
    check(r1.returncode == 0 and r2.returncode != 0 and 'beyond their time stamps' in r2.stderr,
          '--same-as ignores the COFF and debug-directory time stamps and nothing else')
    good, stock = folder / 'sync.dll', folder / 'stock.dll'
    good.write_bytes(synth(True))
    stock.write_bytes(synth(False, flag=False, marker=False))
    r = run('--verify', good, stock)
    check(r.returncode == 0, '--verify accepts the x64 bodies in an X64 range with the flag and marker (%s)'
          % (r.stdout + r.stderr).strip()[-300:])
    r = run('--verify', stock)
    check(r.returncode != 0 and 'does not match' in r.stderr and 'not exported' in r.stderr and 'marker' in r.stderr,
          '--verify refuses fast-forward sequences, a missing flag export and a missing marker')
    tight = folder / 'tight.dll'
    tight.write_bytes(synth(True, text_size=0x2fe0))
    r = run('--verify', tight)
    check(r.returncode != 0 and 'RtlPcToFileHeader trampoline' in r.stderr,
          '--verify refuses an image with no room left for the RtlPcToFileHeader trampoline after .text')
    r = run('--verify', good, good)
    check(r.returncode != 0 and 'exports differ' in r.stderr, '--verify compares the export names with the stock DLL')
for env_dll, env_stock in ((os.environ.get('NTDLL_SYNC_DLL'), os.environ.get('NTDLL_STOCK_DLL')),):
    if env_dll:
        r = subprocess.run([sys.executable, str(script), '--verify', env_dll] + ([env_stock] if env_stock else []),
                           capture_output=True, text=True)
        check(r.returncode == 0, '--verify accepts ' + env_dll + ' ' + (r.stdout + r.stderr).strip()[-200:])

# ---- 5. integration ------------------------------------------------------------------
loader = (root / 'build/ntdll-unix/loader_ios.c').read_text()
fn = loader[loader.index('static char *ios_ec_ntdll_name('):]
fn = fn[:fn.index('\n}\n') + 3]
check('ntdll-sync.dll' in fn and '!strcmp( pe_dir, "/arm64ec-windows" )' in fn and 'access( name, R_OK )' in fn,
      'the loader maps arm64ec-windows/ntdll-sync.dll when the bundle has it, else ntdll.dll')
mode = loader[loader.index('static int ios_ntdll_sync_mode( void )'):]
mode = mode[:mode.index('\n}\n')]
check('getenv( "MADEIRA_NTDLL_SYNC" )' in mode and "e[0] == '1' && !e[1]" in mode and '"native"' in mode and
      mode.rstrip().endswith('return 0;'),
      'off by default: only env.MADEIRA_NTDLL_SYNC = 1 or native maps ntdll-sync.dll (native: no x64 bodies)')
ld = loader[loader.index('static void load_ntdll(void)'):]
ld = ld[:ld.index('\n}\n')]
check('else name = ios_ec_ntdll_name( dll_dir, pe_dir );' in ld, 'load_ntdll (the session ntdll) uses it')
ch = loader[loader.index('static int ios_load_child_ec_ntdll( PEB *child_peb )'):]
ch = ch[:ch.index('\n}\n')]
check('else name = ios_ec_ntdll_name( dll_dir, pe_dir );' in ch and 'ios_ntdll_sync_publish( module, exports );' in ch,
      'a cross-arch child\'s private ARM64EC ntdll uses it too')
lf = loader[loader.index('static void load_ntdll_functions( HMODULE module )'):]
lf = lf[:lf.index('\n}\n')]
check('ios_ntdll_sync_publish( module, exports );' in lf, 'the session ntdll gets madeira_sync_x64_off published')
pub = loader[loader.index('static void ios_ntdll_sync_publish('):]
pub = pub[:pub.index('\n}\n')]
check('"madeira_sync_x64_off"' in pub and 'if (!off) return;' in pub and 'ios_jit_sync_write( off' in pub,
      'only an image exporting madeira_sync_x64_off is written to')
bridge = (root / 'app/Madeira/WineProcessBridge.m').read_text()
blk = bridge[bridge.index('const char *ntSync = getenv("MADEIRA_NTDLL_SYNC");'):]
blk = blk[:blk.index('\n            }\n')]
check('@"ntdll-sync.dll"' in blk and '@"ntdll.dll"' in blk and 'if (use_arm64ec) [dirs addObject:sys32Dir];' in blk and
      'drive_c/windows/sysx64' in blk and "ntSync[0] == '1' && !ntSync[1]" in blk and '!strcmp(ntSync, "native")' in blk,
      'system32 (ARM64EC sessions) and sysx64 ntdll.dll link to the variant the loader maps, only when = 1 or native')
check(bridge.index('LOG("Symlinked %d DLLs from %{public}s to %{public}s"') < bridge.index('const char *ntSync = getenv("MADEIRA_NTDLL_SYNC");'),
      'relinked after the farm')
check('unsetenv("MADEIRA_NTDLL_SYNC");' in bridge, 'a game\'s MADEIRA_NTDLL_SYNC does not reach a later game of the app run')
sh = (root / 'tools/build-wine-extra-dlls.sh').read_text()
step = sh[sh.index('if [ -n "$NTS" ]; then'):]
step = step[:step.index('\nfi\n') + 4]
check('--same-as "$B/ntdll-stock.dll" "$SHIP/ntdll.dll"' in step and
      step.index('--same-as') < step.index('tools/patch-wine-ntdll-sync.py" "$R/wine"'),
      'the build first checks that the unpatched tree rebuilds the shipped ntdll.dll')
check(step.index('--verify "$SHIP/ntdll-sync.dll.tmp" "$SHIP/ntdll.dll"') < step.index('mv "$SHIP/ntdll-sync.dll.tmp" "$SHIP/ntdll-sync.dll"'),
      'ntdll-sync.dll is verified before it ships')
check('-ffile-prefix-map=$ntsrc=.. -fno-file-reproducible' in step and '+ 0x50000' in step and
      '"$MINGW/arm64ec-w64-mingw32-strip"' in step,
      'built as build/wine-pe/build-ntdll.sh builds ntdll.dll (../ paths, stripped, padded to SizeOfImage + 0x50000)')
check('git -C "$R/wine" checkout -- dlls/ntdll/sync.c dlls/ntdll/ntdll.spec dlls/ntdll/Makefile.in' in step and
      'rm -f "$R/wine/dlls/ntdll/sync_x64.c" "$nt" "$B"/dlls/ntdll/arm64ec-windows/*.o' in step,
      'the tree and the cached ntdll objects are restored afterwards')
check('"$SHIP/ntdll.dll' not in sh.replace('"$SHIP/ntdll.dll" ]', '').replace('"$SHIP/ntdll.dll"; then', '').replace(
      '"$SHIP/ntdll.dll"; then', ''), 'the shipped ntdll.dll is only read, never written')
check(sh.index('rm -f "$R/wine/dlls/ucrtbase/x64math.c"') < sh.index('if [ -n "$NTS" ]; then'),
      'built from the tree the other patches have been undone in')
ci = (root / '.github/workflows/build-ipa.yml').read_text()
check('- name: Check ntdll-sync\n        run: python3 tests/host/check-ntdll-sync.py' in ci and
      ci.index('- name: Check quiet-log') < ci.index('- name: Check ntdll-sync'), 'CI runs this check after quiet-log')
check('python3 tools/patch-wine-ntdll-sync.py --verify app/Madeira/arm64ec-windows/ntdll-sync.dll' in ci,
      'CI verifies the shipped ntdll-sync.dll')
cat = (root / 'build/tools/gen-config-catalog.py').read_text()
gen = (root / 'app/Madeira/ConfigCatalog.generated.swift').read_text()
check('"env.MADEIRA_NTDLL_SYNC"' in cat and 'key: "env.MADEIRA_NTDLL_SYNC"' in gen and
      '("", "Off (default)")' in cat and '("1", "On")' in cat,
      'the catalog documents the switch (off by default, 1 on, native)')
rec = (root / 'app/Madeira/GameRecommendations.swift').read_text()
check('MADEIRA_NTDLL_SYNC' not in rec, 'no game list sets it')

if failures:
    print('FAILED: %d check(s)' % len(failures))
    sys.exit(1)
print('all ntdll-sync checks passed')
