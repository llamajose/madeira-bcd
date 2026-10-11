#!/usr/bin/env python3
"""A WoW64 caller's I/O status block is written as a 32-bit one only inside its
guest window (tools/patch-wine-iosb-owner.py); no Wine, iOS or device.

CATIA V5's 32-bit console helpers (DSYCheckVC8.exe, CATCheckVC9Level.exe, build
501) got a console of their own from the 64-bit kernelbase the CPU backend
loads, through an overlapped handle with a 64-bit IO_STATUS_BLOCK on the stack.
in_wow64_call() is TRUE for every caller of a WoW64 pseudo-process, so that
block's Pointer member (stack garbage: 0x10000) was registered as the block and
the completion stored a 32-bit block there, in a fault loop.

Patches copies of wine/dlls/ntdll/unix/{unix_private.h,file.c} (twice: the
second run must find them patched), compiles the patched set_async_iosb,
iosb_client_ptr, set_sync_iosb and ios_iosb_is_wow64 with stubs and checks:
  - a 32-bit program's block (inside the window, reached through the thunk's
    64-bit block): registered as the 32-bit block and written in the 32-bit
    layout, also on the synchronous path;
  - a 64-bit caller's block in the same process (outside the window, Pointer =
    0x10000): registered and written as itself in the 64-bit layout, never
    through Pointer;
  - a caller without a window, a synchronous handle and a 64-bit process keep
    the 64-bit layout; the window's edges; no window (base 0) leaves the
    answer to in_wow64_call();
  - MADEIRA_IO_STATUS_ADDRESS=0 restores in_wow64_call() alone;
and that CI applies the patch before ntdll-unix is built.
Needs python3 and a C compiler; skips when the wine submodule is not checked out.
"""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[2]
unix = root / 'wine' / 'dlls' / 'ntdll' / 'unix'
failures = 0


def require(condition, label):
    global failures
    print(('PASS: ' if condition else 'FAIL: ') + label)
    if not condition:
        failures += 1


workflow = (root / '.github' / 'workflows' / 'build-ipa.yml').read_text()
step = workflow.find('python3 tools/patch-wine-iosb-owner.py wine/dlls/ntdll/unix')
require(step >= 0 and 'python3 tests/host/check-iosb-owner.py' in workflow[step - 400:step],
        'CI checks and applies the patch')
require(0 <= step < workflow.index('- name: Build ntdll-unix'), 'before ntdll-unix is built')

if not (unix / 'file.c').exists():
    print('SKIP: wine submodule not checked out; the patched code is not compiled')
    print('FAILURES:', failures)
    sys.exit(1 if failures else 0)

with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    for name in ('unix_private.h', 'file.c'):
        shutil.copy(unix / name, tmp / name)
    patch = [sys.executable, str(root / 'tools' / 'patch-wine-iosb-owner.py'), str(tmp)]
    first = subprocess.run(patch, capture_output=True, text=True)
    require(first.returncode == 0, 'the patch applies: ' + (first.stdout + first.stderr).strip()[:160])
    second = subprocess.run(patch, capture_output=True, text=True)
    require(second.returncode == 0 and 'already patched' in second.stdout, 'a second run finds it patched')
    header = (tmp / 'unix_private.h').read_text()
    source = (tmp / 'file.c').read_text()

    def function(text, signature):
        start = text.index(signature)
        return text[start:text.index('\n}\n', start) + 3]

    helper_start = header.index('#ifdef WINE_IOS\n/* madeira-bcd: ios_iosb_is_wow64')
    helper = header[helper_start:header.index('#endif\n', helper_start) + 7]
    code = (helper + '\n' + function(header, 'static inline void set_async_iosb(') + '\n'
            + function(header, 'static inline client_ptr_t iosb_client_ptr(') + '\n'
            + '#ifdef WINE_IOS\n' + function(source, 'BOOL ios_iosb_is_wow64( const void *iosb )') + '#endif\n'
            + function(source, 'static void set_sync_iosb('))

    harness = r'''
#define _DEFAULT_SOURCE
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#define WINE_IOS 1
typedef int BOOL;
typedef int32_t NTSTATUS;
typedef uint32_t ULONG;
typedef uintptr_t ULONG_PTR;
typedef uint64_t client_ptr_t;
#define TRUE 1
#define FALSE 0
#define FILE_SYNCHRONOUS_IO_ALERT 0x10
#define FILE_SYNCHRONOUS_IO_NONALERT 0x20
typedef struct { union { NTSTATUS Status; void *Pointer; }; ULONG_PTR Information; } IO_STATUS_BLOCK;
typedef struct { NTSTATUS Status; ULONG Information; } IO_STATUS_BLOCK32;
#define WriteRelease(p, v) __atomic_store_n((p), (v), __ATOMIC_RELEASE)
static inline void *wine_server_get_ptr( client_ptr_t p ) { return (void *)(uintptr_t)p; }
static inline client_ptr_t wine_server_client_ptr( const void *p ) { return (client_ptr_t)(uintptr_t)p; }
static int wow64_caller;                       /* in_wow64_call(): the caller's pseudo-process */
static inline BOOL in_wow64_call(void) { return wow64_caller; }
static ULONG_PTR window;                       /* ios_wow_host_base(): the caller's guest window */
ULONG_PTR ios_wow_base(void) { return window; }
static inline ULONG_PTR ios_wow_host_base(void) { return ios_wow_base ? ios_wow_base() : 0; }
''' + code + r'''
static int fails;
#define CHECK(c, ...) do { if (!(c)) { printf("FAIL: " __VA_ARGS__); printf("\n"); fails++; } \
                           else { printf("PASS: " __VA_ARGS__); printf("\n"); } } while (0)
static char *map_near( char *hint )
{
    void *p = mmap( hint, 0x10000, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANON, -1, 0 );
    return p == MAP_FAILED ? NULL : p;
}
int main( int argc, char **argv )
{
    int off = argc > 1 && argv[1][0] == '0';   /* run with MADEIRA_IO_STATUS_ADDRESS=0 */
    char *in = map_near( NULL ), *out = NULL;
    ULONG_PTR far[] = { 0x200000000ull, 0x400000000ull, 0x1000000000ull, 0x10000000000ull };
    unsigned i;
    IO_STATUS_BLOCK32 *io32;
    IO_STATUS_BLOCK *thunk, *native;

    for (i = 0; i < sizeof(far) / sizeof(far[0]) && in; i++)
    {
        out = map_near( in + far[i] );
        if (out && (ULONG_PTR)out - ((ULONG_PTR)in - 0x1000) >= (1ull << 32)) break;
        if (out) munmap( out, 0x10000 );
        out = NULL;
    }
    if (!in || !out) { printf("FAIL: no memory 4 GB away from the window\n"); return 1; }
    window = (ULONG_PTR)in - 0x1000;                      /* B: the window holds `in`, not `out` */
    io32 = (IO_STATUS_BLOCK32 *)(in + 0x100);             /* the 32-bit program's block */
    thunk = (IO_STATUS_BLOCK *)(out + 0x100);             /* the WoW64 thunk's 64-bit block */
    native = (IO_STATUS_BLOCK *)(out + 0x200);            /* kernelbase's own block, on its stack */
    thunk->Pointer = io32;
    native->Pointer = (void *)0x10000;                    /* what build 501's stack held */
    wow64_caller = 1;

    if (off)
    {
        CHECK( ios_iosb_is_wow64( native ) && iosb_client_ptr( native ) == 0x10000,
               "MADEIRA_IO_STATUS_ADDRESS=0: every block of a WoW64 caller is a 32-bit one again" );
        return fails != 0;
    }
    CHECK( iosb_client_ptr( thunk ) == wine_server_client_ptr( io32 ),
           "32-bit program: the thunk's block registers the 32-bit block" );
    set_async_iosb( wine_server_client_ptr( io32 ), 0x103, 0x1234 );
    CHECK( io32->Status == 0x103 && io32->Information == 0x1234, "32-bit program: completion in the 32-bit layout" );
    io32->Status = 0; io32->Information = 0;
    set_sync_iosb( thunk, 7, 9, 0 );
    CHECK( io32->Status == 7 && io32->Information == 9 && thunk->Information == 0,
           "32-bit program, overlapped handle: synchronous result in the 32-bit block" );
    thunk->Pointer = io32;
    set_sync_iosb( thunk, 5, 6, FILE_SYNCHRONOUS_IO_NONALERT );
    CHECK( thunk->Status == 5 && thunk->Information == 6, "synchronous handle: the 64-bit block, as before" );
    thunk->Pointer = io32;

    CHECK( iosb_client_ptr( native ) == wine_server_client_ptr( native ),
           "64-bit caller in a WoW64 process: its own block is registered, not Pointer (0x10000)" );
    set_async_iosb( wine_server_client_ptr( native ), 0x11, 0x2222 );
    CHECK( native->Status == 0x11 && native->Information == 0x2222,
           "64-bit caller in a WoW64 process: completion in the 64-bit layout" );
    native->Pointer = (void *)0x10000;
    set_sync_iosb( native, 3, 4, 0 );
    CHECK( native->Status == 3 && native->Information == 4,
           "64-bit caller, overlapped handle: synchronous result in its own block, Pointer not followed" );

    CHECK( ios_iosb_is_wow64( (void *)window ) && ios_iosb_is_wow64( (void *)(window + 0xfffffff8ull) ),
           "the window's first and last block are 32-bit ones" );
    CHECK( !ios_iosb_is_wow64( (void *)(window + 0x100000000ull) ) && !ios_iosb_is_wow64( (void *)(window - 8) )
           && !ios_iosb_is_wow64( (void *)0x10000 ), "just past either end, and 0x10000, are not" );
    window = 0;
    CHECK( ios_iosb_is_wow64( native ), "no window: in_wow64_call() decides alone" );
    wow64_caller = 0;
    native->Pointer = (void *)0x10000;
    CHECK( iosb_client_ptr( native ) == wine_server_client_ptr( native ), "64-bit process: its own block" );
    set_async_iosb( wine_server_client_ptr( native ), 1, 2 );
    CHECK( native->Status == 1 && native->Information == 2, "64-bit process: the 64-bit layout" );
    return fails != 0;
}
'''
    c = tmp / 't.c'
    c.write_text(harness)
    exe = tmp / 't'
    cc = os.environ.get('CC') or shutil.which('cc') or 'cc'
    build = subprocess.run([cc, '-std=gnu11', '-O1', '-Wall', '-Wno-address', '-o', str(exe), str(c)],
                           capture_output=True, text=True)
    require(build.returncode == 0, 'the patched code compiles' + ('' if build.returncode == 0
                                                                  else ': ' + build.stderr[-1500:]))
    if build.returncode == 0:
        env = {k: v for k, v in os.environ.items() if k != 'MADEIRA_IO_STATUS_ADDRESS'}
        run = subprocess.run([str(exe)], env=env, capture_output=True, text=True)
        sys.stdout.write(run.stdout)
        require(run.returncode == 0, 'every layout check')
        env['MADEIRA_IO_STATUS_ADDRESS'] = '0'
        run = subprocess.run([str(exe), '0'], env=env, capture_output=True, text=True)
        sys.stdout.write(run.stdout)
        require(run.returncode == 0, 'the kill switch')

print('FAILURES:', failures)
sys.exit(1 if failures else 0)
