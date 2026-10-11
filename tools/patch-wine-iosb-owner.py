#!/usr/bin/env python3
"""A WoW64 caller's I/O status block is a 32-bit one only inside its guest window.

The iOS build decides an I/O status block's layout with in_wow64_call(): a
pseudo-process with a guest window is a WoW64 one (ios_in_wow64_call, file.c),
so every block of its callers is written as an IO_STATUS_BLOCK32 through the
64-bit block's Pointer member, which the WoW64 thunks set to the 32-bit block
(iosb_32to64, dlls/wow64/wow64_private.h). But a WoW64 pseudo-process here also
runs 64-bit code that does I/O of its own: the CPU backend (xtajit) imports
kernelbase, and kernelbase's start-up gives a 32-bit console program a console
of its own (a second conhost) through an overlapped handle, with a 64-bit block
on its stack. Its Pointer member is whatever the stack held. That value was
registered with the server as the block (iosb_client_ptr) and the completion
wrote a 32-bit block there (set_async_iosb): 0x10000 in CATIA V5's installer
helpers on build 501 (DSYCheckVC8.exe, CATCheckVC9Level.exe), a store the
Mach handler could not place, retried until the SEGV loop breaker skipped it;
the helper then ended with ERROR_NOACCESS.

A 32-bit program's block always lies inside its pseudo-process's guest window
[B, B + 4 GB): the thunks hand the unix side B + its guest address. So the
block's own address decides: inside the window, the 32-bit layout as before;
anywhere else, the block is a 64-bit caller's and gets the 64-bit layout, and
iosb_client_ptr registers the block itself rather than its Pointer member.
Changed in three places: set_async_iosb and iosb_client_ptr (unix_private.h),
set_sync_iosb (file.c), through ios_iosb_is_wow64, defined in file.c next to
ios_in_wow64_call. A caller without a guest window is unaffected (in_wow64_call
is FALSE for it). env.MADEIRA_IO_STATUS_ADDRESS = 0 (read once): every block of
a WoW64 caller is a 32-bit one again.

Applied to the wine checkout by CI before ntdll-unix is built; the submodule is
not committed to. Idempotent; exits non-zero when an anchor is missing.
Usage: patch-wine-iosb-owner.py [wine/dlls/ntdll/unix]
"""
import os
import sys

unix_dir = sys.argv[1] if len(sys.argv) > 1 else "wine/dlls/ntdll/unix"
header_path = os.path.join(unix_dir, "unix_private.h")
file_path = os.path.join(unix_dir, "file.c")
MARKER = "madeira-bcd: ios_iosb_is_wow64"


def fail(message):
    sys.exit("patch-wine-iosb-owner: " + message)


def replace_once(text, old, new, what):
    if text.count(old) != 1:
        fail("%s anchor found %d times" % (what, text.count(old)))
    return text.replace(old, new)


header = open(header_path).read()
source = open(file_path).read()
done = (MARKER in header, MARKER in source)
if all(done):
    print("patch-wine-iosb-owner: already patched")
    sys.exit(0)
if any(done):
    fail("half patched (unix_private.h %s, file.c %s)" % done)

# unix_private.h: the helper, after in_wow64_call()
in_wow64 = ("static inline BOOL in_wow64_call(void)\n{\n#ifdef WINE_IOS\n"
            "    if (ios_in_wow64_call) return ios_in_wow64_call();\n#endif\n"
            "    return is_win64 && is_wow64();\n}\n")
helper = in_wow64 + r'''
#ifdef WINE_IOS
/* madeira-bcd: ios_iosb_is_wow64 (tools/patch-wine-iosb-owner.py), defined in file.c.
 * Is this I/O status block of a WoW64 caller a 32-bit program's?  Only one inside the
 * caller's guest window is; 64-bit code in a WoW64 process (the CPU backend's
 * kernelbase) has blocks of its own.  Weak: without it, every one is, as upstream. */
extern BOOL ios_iosb_is_wow64( const void *iosb ) __attribute__((weak));
static inline BOOL iosb_is_wow64( const void *iosb )
{
    return !ios_iosb_is_wow64 || ios_iosb_is_wow64( iosb );
}
#endif
'''
header = replace_once(header, in_wow64, helper, "in_wow64_call")

async_old = ("    if (in_wow64_call())\n    {\n"
             "        IO_STATUS_BLOCK32 *io = wine_server_get_ptr( iosb );\n")
async_new = ("#ifdef WINE_IOS\n"
             "    if (in_wow64_call() && iosb_is_wow64( wine_server_get_ptr( iosb ) ))\n"
             "#else\n"
             "    if (in_wow64_call())\n"
             "#endif\n"
             "    {\n"
             "        IO_STATUS_BLOCK32 *io = wine_server_get_ptr( iosb );\n")
header = replace_once(header, async_old, async_new, "set_async_iosb")

client_old = "    if (io && in_wow64_call()) return wine_server_client_ptr( io->Pointer );\n"
client_new = ("#ifdef WINE_IOS\n"
              "    if (io && in_wow64_call() && iosb_is_wow64( io->Pointer )) return wine_server_client_ptr( io->Pointer );\n"
              "#else\n" + client_old + "#endif\n")
header = replace_once(header, client_old, client_new, "iosb_client_ptr")

# file.c: the definition, after ios_in_wow64_call(), and set_sync_iosb
owner_end = ("    if (!mode || !ios_wow_base) return is_win64 && is_wow64();\n"
             "    return is_win64 && ios_wow_host_base() != 0;\n}\n#endif\n")
definition = owner_end + r'''
#ifdef WINE_IOS
/* madeira-bcd: ios_iosb_is_wow64 (tools/patch-wine-iosb-owner.py).
 *
 * A WoW64 pseudo-process here also runs 64-bit code that does I/O of its own: the
 * CPU backend (xtajit) imports kernelbase, and kernelbase's start-up gives a 32-bit
 * console program a console of its own through an overlapped handle, with a 64-bit
 * IO_STATUS_BLOCK on its stack.  in_wow64_call() is TRUE for every caller of such a
 * process, so that block's Pointer member -- whatever the stack held -- was
 * registered as the block and the completion wrote a 32-bit block there: 0x10000 in
 * CATIA V5's installer helpers (build 501), a store that faulted in a loop until the
 * SEGV loop breaker skipped it, and the helper ended with ERROR_NOACCESS.
 *
 * A 32-bit program's block always lies inside its pseudo-process's guest window
 * [B, B + 4 GB): the WoW64 thunks hand the unix side B + its guest address
 * (dlls/wow64/wow64_private.h).  A block anywhere else is a 64-bit caller's.  Without
 * a window this says nothing (TRUE): in_wow64_call() decides alone, as before.
 * MADEIRA_IO_STATUS_ADDRESS=0: every block of a WoW64 caller is a 32-bit one again. */
BOOL ios_iosb_is_wow64( const void *iosb )
{
    static int enabled = -1;
    int mode = __atomic_load_n( &enabled, __ATOMIC_RELAXED );
    ULONG_PTR base;

    if (mode < 0)
    {
        const char *env = getenv( "MADEIRA_IO_STATUS_ADDRESS" );
        mode = !(env && env[0] == '0');
        __atomic_store_n( &enabled, mode, __ATOMIC_RELAXED );
    }
    if (!mode || !ios_wow_base || !(base = ios_wow_host_base())) return TRUE;
    return (ULONG_PTR)iosb - base < ((ULONG_PTR)1 << 32);
}
#endif
'''
if source.count("BOOL ios_in_wow64_call(void)\n{") != 1:
    fail("ios_in_wow64_call definition not found")
start = source.index("BOOL ios_in_wow64_call(void)\n{")
close = source.index("\n}\n", start) + 1          # the function's closing brace
end = source.find(owner_end, start)
if end < 0 or source.count(owner_end) != 1 or end + len(owner_end) != close + len("}\n#endif\n"):
    fail("ios_in_wow64_call has changed shape")
source = source[:end] + definition + source[end + len(owner_end):]

sync_old = ("    if (in_wow64_call() && !(options & (FILE_SYNCHRONOUS_IO_ALERT | FILE_SYNCHRONOUS_IO_NONALERT)))\n"
            "    {\n        IO_STATUS_BLOCK32 *io32 = io->Pointer;\n")
sync_new = ("#ifdef WINE_IOS\n"
            "    if (in_wow64_call() && !(options & (FILE_SYNCHRONOUS_IO_ALERT | FILE_SYNCHRONOUS_IO_NONALERT)) &&\n"
            "        iosb_is_wow64( io->Pointer ))\n"
            "#else\n"
            "    if (in_wow64_call() && !(options & (FILE_SYNCHRONOUS_IO_ALERT | FILE_SYNCHRONOUS_IO_NONALERT)))\n"
            "#endif\n"
            "    {\n        IO_STATUS_BLOCK32 *io32 = io->Pointer;\n")
source = replace_once(source, sync_old, sync_new, "set_sync_iosb")
if source.index("BOOL ios_iosb_is_wow64( const void *iosb )") > source.index("static void set_sync_iosb("):
    fail("ios_iosb_is_wow64 must be defined before set_sync_iosb")

open(header_path, "w").write(header)
open(file_path, "w").write(source)
print("patch-wine-iosb-owner: a WoW64 caller's I/O status block is a 32-bit one only inside its guest window "
      "(set_async_iosb, iosb_client_ptr, set_sync_iosb; MADEIRA_IO_STATUS_ADDRESS=0 restores in_wow64_call alone)")
