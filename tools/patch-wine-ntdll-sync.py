#!/usr/bin/env python3
"""madeira-bcd: cheaper SRW locks and critical sections in Wine's PE ntdll (ntdll-sync.dll).

Horizon Zero Dawn calls the SRW lock exports about 0.9 million times a second
in play and 2-3 million in its menus (build 493, [xp-api-top]: release
exclusive 0.30 M/s, try-acquire exclusive 0.25, try-acquire shared 0.12,
release shared 0.12, acquire exclusive 0.10) and the critical-section exports
another 0.13 million (leave 68k, enter 36k, try-enter 25k), every call from
x64 code a transition out of the emulator and back. Behind them the ntdll fork did more
than the lock needs ([xp-api]: RtlWakeAddressAll 250-290k/s and
RtlWakeAddressSingle 90-120k/s in play, 0.5-0.8 M/s while loading, with
1.3-2.6k waits a second):
  - every acquire and release wrote the ml410 ring: an interlocked increment
    of one process-wide index and two stores into a shared 512-entry table;
  - an exclusive release with no exclusive waiter always called
    RtlWakeAddressAll (Wine keeps no count of shared waiters), and the last
    shared release always called RtlWakeAddressSingle, waiter or not: each a
    process-wide interlocked counter (ml1131) and a spinlock round trip on the
    futex-table bucket.

This patch keeps Wine's SRW algorithm and lock word and changes only that:
  sync.c    bit 15 of exclusive_waiters says shared waiters may be parked on the
            word. A shared acquirer sets it (in the same compare-exchange that
            decides to wait) before it waits; the exclusive release that leaves
            no exclusive owner or waiter clears it in its own compare-exchange
            and then wakes them. A release wakes only when the word says
            somebody waits: an exclusive waiter if the count says so, else the
            shared waiters if the flag says so; the last shared owner wakes an
            exclusive waiter only if the count says so. Every decision reads
            the value its own atomic operation saw, so a waiter that registered
            first is always woken (the futex compare catches the rest). The
            flag is set only while the rest of exclusive_waiters is non-zero,
            so every "!exclusive_waiters" test means what it meant (exclusive
            waiter count limited to 2^14-1 instead of 2^15-1). The ring writes
            are gone (ios_srw_note is empty; its only reader is the PUMP-beacon
            park dump).
  sync_x64.c x64 bodies (EXP+#<export>, a `#pragma makedep arm64ec_x64` source
            as ntdll's signal_x86_64.c is) for the six SRW exports: the
            uncontended paths in x64 code -- one lock cmpxchg on the same word
            -- and a jump to the native body for everything else, or to
            RtlWakeAddressSingle / RtlWakeAddressAll when a release must wake.
            And for RtlEnterCriticalSection, RtlTryEnterCriticalSection and
            RtlLeaveCriticalSection (sync.c's code unchanged): a free section
            taken with one lock cmpxchg of LockCount, a nested enter or leave
            of the owner, and a last leave nobody waits for; a section another
            thread holds or waits for goes to the native body, which waits,
            spins and wakes as before.
            lld uses EXP+#<export> as the export's x64 thunk when an object
            defines it and still redirects ARM64EC callers (and GetProcAddress
            from ARM64EC code) to the native bodies.
  ntdll.spec exports madeira_sync_x64_off: the unix loader sets it for
            env.MADEIRA_NTDLL_SYNC = native, which sends the x64 bodies straight
            to the native ones.

The result ships as arm64ec-windows/ntdll-sync.dll NEXT TO the untouched
ntdll.dll (tools/build-wine-extra-dlls.sh, which first checks that the same
tree and flags rebuild the shipped ntdll.dll byte for byte apart from its time
stamps). build/ntdll-unix/loader_ios.c maps it instead of ntdll.dll unless
env.MADEIRA_NTDLL_SYNC = 0, and app/Madeira/WineProcessBridge.m links
system32\\ntdll.dll to the same file.

Usage:
  patch-wine-ntdll-sync.py WINE_SRC              patch dlls/ntdll (idempotent)
  patch-wine-ntdll-sync.py --verify DLL [STOCK]  check a built ntdll-sync.dll
      (the nine exports are the x64 bodies in X64 code ranges, no word of them
      looks like an x18 access, RtlPcToFileHeader's trampoline still fits after
      .text, the marker and madeira_sync_x64_off are there; with STOCK, the
      export names are the shipped ntdll.dll's plus madeira_sync_x64_off)
  patch-wine-ntdll-sync.py --same-as A B         A and B are the same image
      apart from their time stamps (COFF header and debug directory)
"""
import os
import struct
import sys

MARK = "madeira-bcd ntdll-sync"
SOURCE = "sync_x64.c"
MARKER = b"madeira-ntdll-sync v1"
FLAG = "madeira_sync_x64_off"
# the linker's x64 thunk for an ARM64EC export: mov rax,rsp; mov [rax+20h],rbx;
# push rbp; pop rbp; jmp rel32
FFS = bytes.fromhex("488bc448895820555de9")

# ---- sync.c --------------------------------------------------------------------

DEFS_OLD = "C_ASSERT( sizeof(struct srw_lock) == 4 );\n"
DEFS_NEW = DEFS_OLD + """
/* madeira-bcd ntdll-sync (tools/patch-wine-ntdll-sync.py): bit 15 of
 * exclusive_waiters says shared waiters may be parked on the whole word. A
 * shared acquirer sets it in the compare-exchange that decides to wait; the
 * exclusive release that leaves no exclusive owner or waiter clears it in its
 * own compare-exchange and wakes them. So a release wakes only when its word
 * says somebody waits. The flag is set only while the rest of
 * exclusive_waiters is non-zero, so "!exclusive_waiters" keeps its meaning. */
#define MADEIRA_SRW_SHARED_PARKED 0x8000
#define MADEIRA_SRW_EXCLUSIVE     0x7fff   /* held bit and exclusive waiter count */

/* Read by the x64 bodies (sync_x64.c) from the image's .data, written by the
 * unix loader for env.MADEIRA_NTDLL_SYNC = native; native code never reads it. */
LONG madeira_sync_x64_off = 0;
static const char madeira_ntdll_sync_marker[] __attribute__((used)) = "madeira-ntdll-sync v1";
"""

NOTE_OLD = """static void ios_srw_note( const void *lock, unsigned int mode )
{
    ULONG i = (ULONG)InterlockedIncrement( &ios_srw_ring_idx );
    ios_srw_ring[i & 511].lock = lock;
    ios_srw_ring[i & 511].tid_mode =
        (HandleToULong( NtCurrentTeb()->ClientId.UniqueThread ) << 2) | mode;
}
"""
NOTE_NEW = """static void ios_srw_note( const void *lock, unsigned int mode )
{
    /* madeira-bcd ntdll-sync: no ring. Every lock operation of every thread
     * incremented one shared index and wrote a shared table; its only reader
     * is the PUMP-beacon park dump below. */
    (void)lock; (void)mode;
}
"""

SHARED_OLD = """        do
        {
            old.s = *u.s;
            new = old;

            if (!old.s.exclusive_waiters)
            {
                /* Not locked exclusive, and no exclusive waiters.
                 * We can try to grab it. */
                ++new.s.owners;
                wait = FALSE;
            }
            else
            {
                wait = TRUE;
            }
        } while (InterlockedCompareExchange( u.l, new.l, old.l ) != old.l);

        if (!wait)
        {
            ios_srw_note( lock, 2 );
            return;
        }
        RtlWaitOnAddress( u.s, &new.s, sizeof(struct srw_lock), NULL );
"""
SHARED_NEW = """        do
        {
            old.s = *u.s;
            new = old;

            if (!old.s.exclusive_waiters)
            {
                /* Not locked exclusive, and no exclusive waiters.
                 * We can try to grab it. */
                ++new.s.owners;
                wait = FALSE;
            }
            else
            {
                /* madeira-bcd ntdll-sync: say so before parking, so the release
                 * that ends the exclusive phase wakes us */
                new.s.exclusive_waiters = (short)((USHORT)old.s.exclusive_waiters | MADEIRA_SRW_SHARED_PARKED);
                wait = TRUE;
            }
        } while (InterlockedCompareExchange( u.l, new.l, old.l ) != old.l);

        if (!wait)
        {
            ios_srw_note( lock, 2 );
            return;
        }
        RtlWaitOnAddress( u.s, &new.s, sizeof(struct srw_lock), NULL );
"""

REL_X_OLD = """        if (!(old.s.exclusive_waiters & 1)) ERR("Lock %p is not owned exclusive!\\n", lock);

        new.s.owners = 0;
        new.s.exclusive_waiters &= ~1;
    } while (InterlockedCompareExchange( u.l, new.l, old.l ) != old.l);

    ios_srw_note( lock, 1 );
    if (new.s.exclusive_waiters)
        RtlWakeAddressSingle( &u.s->owners );
    else
        RtlWakeAddressAll( u.s );
}
"""
REL_X_NEW = """        if (!(old.s.exclusive_waiters & 1)) ERR("Lock %p is not owned exclusive!\\n", lock);

        new.s.owners = 0;
        new.s.exclusive_waiters &= ~1;
        /* madeira-bcd ntdll-sync: the last exclusive one out also clears the
         * parked flag, and wakes those shared waiters below */
        if (!(new.s.exclusive_waiters & MADEIRA_SRW_EXCLUSIVE)) new.s.exclusive_waiters = 0;
    } while (InterlockedCompareExchange( u.l, new.l, old.l ) != old.l);

    ios_srw_note( lock, 1 );
    if (new.s.exclusive_waiters)
        RtlWakeAddressSingle( &u.s->owners );
    else if (old.s.exclusive_waiters & MADEIRA_SRW_SHARED_PARKED)
        RtlWakeAddressAll( u.s );
}
"""

REL_S_OLD = """    ios_srw_note( lock, 3 );
    if (!new.s.owners)
        RtlWakeAddressSingle( &u.s->owners );
}
"""
REL_S_NEW = """    ios_srw_note( lock, 3 );
    /* madeira-bcd ntdll-sync: only an exclusive waiter waits for the last owner */
    if (!new.s.owners && (new.s.exclusive_waiters & MADEIRA_SRW_EXCLUSIVE))
        RtlWakeAddressSingle( &u.s->owners );
}
"""

SYNC_EDITS = [("srw defines", DEFS_OLD, DEFS_NEW), ("ios_srw_note", NOTE_OLD, NOTE_NEW),
              ("RtlAcquireSRWLockShared", SHARED_OLD, SHARED_NEW),
              ("RtlReleaseSRWLockExclusive", REL_X_OLD, REL_X_NEW),
              ("RtlReleaseSRWLockShared", REL_S_OLD, REL_S_NEW)]

# ---- ntdll.spec -----------------------------------------------------------------

SPEC_OLD = "@ extern -private ios_xp_nt\n"
SPEC_NEW = SPEC_OLD + "# madeira-bcd ntdll-sync: sync_x64.c's switch, set by ntdll-unix\n" \
                      "@ extern -private -arch=arm64ec madeira_sync_x64_off\n"

# ---- sync_x64.c -------------------------------------------------------------------

SYNC_X64_C = r'''/*
 * madeira-bcd: x64 bodies for ntdll's SRW lock and critical-section exports
 * (tools/patch-wine-ntdll-sync.py; shipped as ntdll-sync.dll, mapped instead
 * of ntdll.dll unless env.MADEIRA_NTDLL_SYNC = 0).
 *
 * An x64 caller of an ARM64EC export leaves the emulator for the native body
 * and comes back after it. These bodies take the uncontended paths in x64
 * code -- one lock cmpxchg on the lock word, the protocol of the native
 * functions in sync.c -- and jump to the native function for every other case,
 * or to RtlWakeAddressSingle / RtlWakeAddressAll when a release must wake.
 * lld uses EXP+#<export> as the export's x64 thunk when an object defines it
 * and still redirects ARM64EC callers to the native bodies.
 *
 * SRW lock word (struct srw_lock in sync.c): bits 0-15 exclusive_waiters (bit 0
 * held exclusive, bits 1-14 twice the exclusive waiter count, bit 15 shared
 * waiters parked), bits 16-31 owners.
 *
 * RTL_CRITICAL_SECTION: LockCount at 8 (-1 free, else the owner's levels - 1
 * plus the waiters), RecursionCount at 12, OwningThread at 16; the thread id
 * is the TEB's ClientId.UniqueThread, %gs:0x48. A section nobody else holds or
 * waits for is entered and left here; everything else is the native code's.
 *
 * madeira_sync_x64_off (set by the unix loader for env.MADEIRA_NTDLL_SYNC =
 * native) sends every call straight to the native body. It is read from the
 * image's .data, which is the copy x64 code sees; native code never reads it.
 */

#if 0
#pragma makedep arm64ec_x64
#endif

#ifdef __arm64ec_x64__

#include "wine/asm.h"

__ASM_GLOBAL_FUNC( "EXP+#RtlTryAcquireSRWLockExclusive",
                   "cmpl $0,madeira_sync_x64_off(%rip)\n\t"
                   "jne 9f\n\t"
                   "movl (%rcx),%eax\n"
                   "1:\ttestl $0xffff0000,%eax\n\t"      /* owners: taken */
                   "jnz 2f\n\t"
                   "movl %eax,%edx\n\t"
                   "orl $0x10001,%edx\n\t"               /* owners = 1, held */
                   "lock cmpxchgl %edx,(%rcx)\n\t"
                   "jnz 1b\n\t"
                   "movl $1,%eax\n\t"
                   "ret\n"
                   "2:\txorl %eax,%eax\n\t"
                   "ret\n"
                   "9:\tjmp \"#RtlTryAcquireSRWLockExclusive\"" )

__ASM_GLOBAL_FUNC( "EXP+#RtlTryAcquireSRWLockShared",
                   "cmpl $0,madeira_sync_x64_off(%rip)\n\t"
                   "jne 9f\n\t"
                   "movl (%rcx),%eax\n"
                   "1:\ttestl $0xffff,%eax\n\t"          /* held, exclusive waiters or parked */
                   "jnz 2f\n\t"
                   "leal 0x10000(%rax),%edx\n\t"         /* ++owners */
                   "lock cmpxchgl %edx,(%rcx)\n\t"
                   "jnz 1b\n\t"
                   "movl $1,%eax\n\t"
                   "ret\n"
                   "2:\txorl %eax,%eax\n\t"
                   "ret\n"
                   "9:\tjmp \"#RtlTryAcquireSRWLockShared\"" )

__ASM_GLOBAL_FUNC( "EXP+#RtlAcquireSRWLockExclusive",
                   "cmpl $0,madeira_sync_x64_off(%rip)\n\t"
                   "jne 9f\n\t"
                   "xorl %eax,%eax\n\t"                  /* only a free word nobody waits on */
                   "movl $0x10001,%edx\n\t"
                   "lock cmpxchgl %edx,(%rcx)\n\t"
                   "jnz 9f\n\t"
                   "ret\n"
                   "9:\tjmp \"#RtlAcquireSRWLockExclusive\"" )

__ASM_GLOBAL_FUNC( "EXP+#RtlAcquireSRWLockShared",
                   "cmpl $0,madeira_sync_x64_off(%rip)\n\t"
                   "jne 9f\n\t"
                   "movl (%rcx),%eax\n"
                   "1:\ttestl $0xffff,%eax\n\t"
                   "jnz 9f\n\t"
                   "leal 0x10000(%rax),%edx\n\t"
                   "lock cmpxchgl %edx,(%rcx)\n\t"
                   "jnz 1b\n\t"
                   "ret\n"
                   "9:\tjmp \"#RtlAcquireSRWLockShared\"" )

__ASM_GLOBAL_FUNC( "EXP+#RtlReleaseSRWLockExclusive",
                   "cmpl $0,madeira_sync_x64_off(%rip)\n\t"
                   "jne 9f\n\t"
                   "movl (%rcx),%eax\n"
                   "1:\ttestl $1,%eax\n\t"               /* not held exclusive: native reports it */
                   "jz 9f\n\t"
                   "movl %eax,%edx\n\t"
                   "andl $0x7ffe,%edx\n\t"               /* exclusive waiters left? */
                   "jz 2f\n\t"                           /* none: the word becomes 0 */
                   "movl %eax,%edx\n\t"
                   "andl $0xfffe,%edx\n"                 /* keep the waiters and the parked flag */
                   "2:\tlock cmpxchgl %edx,(%rcx)\n\t"
                   "jnz 1b\n\t"
                   "testl %edx,%edx\n\t"
                   "jnz 3f\n\t"
                   "testl $0x8000,%eax\n\t"              /* shared waiters were parked */
                   "jnz 4f\n\t"
                   "ret\n"
                   "3:\taddq $2,%rcx\n\t"                /* one exclusive waiter, on &owners */
                   "jmp \"#RtlWakeAddressSingle\"\n"
                   "4:\tjmp \"#RtlWakeAddressAll\"\n"
                   "9:\tjmp \"#RtlReleaseSRWLockExclusive\"" )

__ASM_GLOBAL_FUNC( "EXP+#RtlReleaseSRWLockShared",
                   "cmpl $0,madeira_sync_x64_off(%rip)\n\t"
                   "jne 9f\n\t"
                   "movl (%rcx),%eax\n"
                   "1:\ttestl $1,%eax\n\t"               /* held exclusive: native reports it */
                   "jnz 9f\n\t"
                   "cmpl $0x10000,%eax\n\t"              /* no owners: native reports it */
                   "jb 9f\n\t"
                   "leal -0x10000(%rax),%edx\n\t"        /* --owners */
                   "lock cmpxchgl %edx,(%rcx)\n\t"
                   "jnz 1b\n\t"
                   "cmpl $0x10000,%edx\n\t"              /* owners left */
                   "jae 2f\n\t"
                   "testl $0x7ffe,%edx\n\t"              /* the last one out wakes an exclusive waiter */
                   "jnz 3f\n"
                   "2:\tret\n"
                   "3:\taddq $2,%rcx\n\t"
                   "jmp \"#RtlWakeAddressSingle\"\n"
                   "9:\tjmp \"#RtlReleaseSRWLockShared\"" )

__ASM_GLOBAL_FUNC( "EXP+#RtlEnterCriticalSection",
                   "cmpl $0,madeira_sync_x64_off(%rip)\n\t"
                   "jne 9f\n\t"
                   "movl $-1,%eax\n\t"
                   "xorl %edx,%edx\n\t"
                   "lock cmpxchgl %edx,8(%rcx)\n\t"      /* LockCount -1 -> 0: free, ours now */
                   "jnz 1f\n\t"
                   "movl %gs:0x48,%eax\n\t"              /* GetCurrentThreadId() */
                   "movq %rax,16(%rcx)\n\t"              /* OwningThread */
                   "movl $1,12(%rcx)\n\t"                /* RecursionCount */
                   "xorl %eax,%eax\n\t"
                   "ret\n"
                   "1:\tmovl %gs:0x48,%eax\n\t"
                   "cmpq %rax,16(%rcx)\n\t"              /* ours already: one level deeper */
                   "jne 9f\n\t"
                   "lock incl 8(%rcx)\n\t"
                   "incl 12(%rcx)\n\t"
                   "xorl %eax,%eax\n\t"
                   "ret\n"
                   "9:\tjmp \"#RtlEnterCriticalSection\"" )

__ASM_GLOBAL_FUNC( "EXP+#RtlTryEnterCriticalSection",
                   "cmpl $0,madeira_sync_x64_off(%rip)\n\t"
                   "jne 9f\n\t"
                   "movl $-1,%eax\n\t"
                   "xorl %edx,%edx\n\t"
                   "lock cmpxchgl %edx,8(%rcx)\n\t"
                   "jnz 1f\n\t"
                   "movl %gs:0x48,%eax\n\t"
                   "movq %rax,16(%rcx)\n\t"
                   "movl $1,12(%rcx)\n\t"
                   "movl $1,%eax\n\t"
                   "ret\n"
                   "1:\tmovl %gs:0x48,%eax\n\t"
                   "cmpq %rax,16(%rcx)\n\t"
                   "jne 2f\n\t"
                   "lock incl 8(%rcx)\n\t"
                   "incl 12(%rcx)\n\t"
                   "movl $1,%eax\n\t"
                   "ret\n"
                   "2:\txorl %eax,%eax\n\t"              /* another thread has it */
                   "ret\n"
                   "9:\tjmp \"#RtlTryEnterCriticalSection\"" )

__ASM_GLOBAL_FUNC( "EXP+#RtlLeaveCriticalSection",
                   "cmpl $0,madeira_sync_x64_off(%rip)\n\t"
                   "jne 9f\n\t"
                   "movl 12(%rcx),%eax\n\t"              /* RecursionCount */
                   "cmpl $1,%eax\n\t"
                   "jl 9f\n\t"                           /* not held: native reports it */
                   "jne 2f\n\t"
                   "cmpl $0,8(%rcx)\n\t"                 /* LockCount 0: nobody waits */
                   "jne 9f\n\t"                          /* else native leaves and wakes one */
                   "movq 16(%rcx),%rdx\n\t"
                   "movl $0,12(%rcx)\n\t"                /* RecursionCount, OwningThread = 0, */
                   "movq $0,16(%rcx)\n\t"
                   "xorl %eax,%eax\n\t"
                   "movl $-1,%r8d\n\t"
                   "lock cmpxchgl %r8d,8(%rcx)\n\t"      /* then LockCount 0 -> -1: free */
                   "jnz 3f\n\t"
                   "ret\n"
                   "2:\tdecl %eax\n\t"                   /* one level less */
                   "movl %eax,12(%rcx)\n\t"
                   "lock decl 8(%rcx)\n\t"
                   "xorl %eax,%eax\n\t"
                   "ret\n"
                   "3:\tmovq %rdx,16(%rcx)\n\t"          /* a waiter came: as it was, */
                   "movl $1,12(%rcx)\n"                  /* and native leaves and wakes it */
                   "9:\tjmp \"#RtlLeaveCriticalSection\"" )

#endif  /* __arm64ec_x64__ */
'''

# The bodies as x86_64 clang assembles them (checked by --verify). "rr" marks
# a rel32 byte the linker fills in: the flag's address and the jump targets.
X64_BODIES = {
    "RtlTryAcquireSRWLockExclusive":
        "833drrrrrrrr00 7520 8b01 a90000ffff 7514 89c2 81ca01000100 f00fb111 75eb b801000000 c3 31c0 c3"
        " e9rrrrrrrr",
    "RtlTryAcquireSRWLockShared":
        "833drrrrrrrr00 751e 8b01 a9ffff0000 7512 8d9000000100 f00fb111 75ed b801000000 c3 31c0 c3"
        " e9rrrrrrrr",
    "RtlAcquireSRWLockExclusive":
        "833drrrrrrrr00 750e 31c0 ba01000100 f00fb111 7501 c3 e9rrrrrrrr",
    "RtlAcquireSRWLockShared":
        "833drrrrrrrr00 7516 8b01 a9ffff0000 750d 8d9000000100 f00fb111 75ed c3 e9rrrrrrrr",
    "RtlReleaseSRWLockExclusive":
        "833drrrrrrrr00 753b 8b01 a901000000 7432 89c2 81e2fe7f0000 7408 89c2 81e2feff0000"
        " f00fb111 75e1 85d2 7508 a900800000 750a c3 4883c102 e9rrrrrrrr e9rrrrrrrr e9rrrrrrrr",
    "RtlReleaseSRWLockShared":
        "833drrrrrrrr00 7536 8b01 a901000000 752d 3d00000100 7226 8d900000ffff f00fb111 75e6"
        " 81fa00000100 7308 f7c2fe7f0000 7501 c3 4883c102 e9rrrrrrrr e9rrrrrrrr",
    "RtlEnterCriticalSection":
        "833drrrrrrrr00 753c b8ffffffff 31d2 f00fb15108 7516 658b042548000000 48894110 c7410c01000000"
        " 31c0 c3 658b042548000000 48394110 750a f0ff4108 ff410c 31c0 c3 e9rrrrrrrr",
    "RtlTryEnterCriticalSection":
        "833drrrrrrrr00 7545 b8ffffffff 31d2 f00fb15108 7519 658b042548000000 48894110 c7410c01000000"
        " b801000000 c3 658b042548000000 48394110 750d f0ff4108 ff410c b801000000 c3 31c0 c3 e9rrrrrrrr",
    "RtlLeaveCriticalSection":
        "833drrrrrrrr00 754b 8b410c 83f801 7c43 752a 83790800 753b 488b5110 c7410c00000000"
        " 48c7411000000000 31c0 41b8ffffffff f0440fb14108 750d c3 ffc8 89410c f0ff4908 31c0 c3"
        " 48895110 c7410c01000000 e9rrrrrrrr",
}


def body_pattern(name):
    """(bytes, mask) of an x64 body: mask 0 where the linker writes a rel32."""
    h = X64_BODIES[name].replace(" ", "")
    data, mask = bytearray(), bytearray()
    for i in range(0, len(h), 2):
        pair = h[i:i + 2]
        if pair == "rr":
            data.append(0); mask.append(0)
        else:
            data.append(int(pair, 16)); mask.append(1)
    return bytes(data), bytes(mask)


def edit(path, edits):
    text = open(path).read()
    changed = False
    for what, old, new in edits:
        if new in text:
            continue
        n = text.count(old)
        if n != 1:
            sys.exit("patch-wine-ntdll-sync: %s anchor found %d times in %s -- wine moved, review the patch"
                     % (what, n, path))
        text = text.replace(old, new)
        changed = True
    if changed:
        open(path, "w").write(text)
    return changed


def patch(wine):
    nt = os.path.join(wine, "dlls", "ntdll")
    sync_c = os.path.join(nt, "sync.c")
    spec = os.path.join(nt, "ntdll.spec")
    mk = os.path.join(nt, "Makefile.in")
    for p in (sync_c, spec, mk):
        if not os.path.isfile(p):
            sys.exit("patch-wine-ntdll-sync: no " + p)
    done = []
    if edit(sync_c, SYNC_EDITS):
        done.append("sync.c")
    if edit(spec, [("ntdll.spec export", SPEC_OLD, SPEC_NEW)]):
        done.append("ntdll.spec")
    src = os.path.join(nt, SOURCE)
    if not os.path.isfile(src) or open(src).read() != SYNC_X64_C:
        with open(src, "w") as f:
            f.write(SYNC_X64_C)
        done.append(SOURCE)
    text = open(mk).read()
    if "\t%s \\\n" % SOURCE not in text and "\t%s\n" % SOURCE not in text:
        lines = text.split("\n")
        try:
            start = lines.index("SOURCES = \\")
        except ValueError:
            sys.exit("patch-wine-ntdll-sync: SOURCES anchor not found in " + mk)
        end = start + 1
        while end < len(lines) and lines[end].endswith("\\"):
            end += 1
        if end >= len(lines) or not lines[end].startswith("\t"):
            sys.exit("patch-wine-ntdll-sync: SOURCES list not understood in " + mk)
        lines[end] = lines[end] + " \\"
        lines.insert(end + 1, "\t" + SOURCE)
        open(mk, "w").write("\n".join(lines))
        done.append("Makefile.in")
    print("patched " + ", ".join(done) if done else "already patched")


# ---- PE helpers -------------------------------------------------------------------

class Pe:
    def __init__(self, path):
        self.path = path
        self.d = d = open(path, "rb").read()
        if d[:2] != b"MZ":
            sys.exit("%s: not a PE image" % path)
        self.pe = pe = struct.unpack_from("<I", d, 0x3c)[0]
        if d[pe:pe + 4] != b"PE\0\0":
            sys.exit("%s: not a PE image" % path)
        self.machine, nsec = struct.unpack_from("<HH", d, pe + 4)
        optsz = struct.unpack_from("<H", d, pe + 20)[0]
        self.opt = opt = pe + 24
        if struct.unpack_from("<H", d, opt)[0] != 0x20b:
            sys.exit("%s: not a PE32+ image" % path)
        self.image_base = struct.unpack_from("<Q", d, opt + 24)[0]
        self.size_of_image = struct.unpack_from("<I", d, opt + 56)[0]
        ndirs = struct.unpack_from("<I", d, opt + 108)[0]
        self.dirs = [struct.unpack_from("<II", d, opt + 112 + 8 * i) for i in range(ndirs)]
        self.secs = []
        for i in range(nsec):
            s = opt + optsz + 40 * i
            name = d[s:s + 8].rstrip(b"\0").decode("latin-1")
            vsz, va, rsz, raw = struct.unpack_from("<IIII", d, s + 8)
            ch = struct.unpack_from("<I", d, s + 36)[0]
            self.secs.append((name, va, vsz, raw, rsz, ch))

    def off(self, rva):
        for name, va, vsz, raw, rsz, ch in self.secs:
            if va <= rva < va + max(vsz, rsz):
                return rva - va + raw
        raise ValueError("rva %#x outside the sections" % rva)

    def section_of(self, rva):
        for sec in self.secs:
            if sec[1] <= rva < sec[1] + max(sec[2], sec[4]):
                return sec
        return None

    def cstr(self, rva):
        o = self.off(rva)
        return self.d[o:self.d.index(b"\0", o)].decode("latin-1")

    def exports(self):
        rva, size = self.dirs[0]
        e = self.off(rva)
        nfun, nnames, afun, anames, aords = struct.unpack_from("<IIIII", self.d, e + 20)
        out = {}
        for i in range(nnames):
            name = self.cstr(struct.unpack_from("<I", self.d, self.off(anames) + 4 * i)[0])
            o = struct.unpack_from("<H", self.d, self.off(aords) + 2 * i)[0]
            out[name] = struct.unpack_from("<I", self.d, self.off(afun) + 4 * o)[0]
        return out

    def code_map(self):
        """CHPE code map: [(start, length, kind)], kind 0 ARM64, 1 ARM64EC, 2 X64."""
        cfg_rva, cfg_size = self.dirs[10]
        if cfg_size <= 0xC8:
            return []
        chpe = struct.unpack_from("<Q", self.d, self.off(cfg_rva) + 0xC8)[0] - self.image_base
        m = self.off(chpe)
        cm_rva, cm_n = struct.unpack_from("<II", self.d, m + 4)
        out = []
        for i in range(cm_n):
            start, length = struct.unpack_from("<II", self.d, self.off(cm_rva) + 8 * i)
            out.append((start & ~3, length, start & 3))
        return out

    def timestamp_offsets(self):
        """File offsets of the 4-byte time stamps: COFF header and each debug directory entry."""
        offs = [self.pe + 8]
        if len(self.dirs) > 6 and self.dirs[6][1]:
            rva, size = self.dirs[6]
            o = self.off(rva)
            for k in range(size // 28):
                offs.append(o + 28 * k + 4)
        return offs


def x18_role(insn):
    """build/ntdll-unix/virtual_ios.c ios_insn_x18_role: would the x18 patcher,
    which walks all of .text including its X64 ranges, rewrite this word?"""
    rn, rm, rt2 = (insn >> 5) & 0x1f, (insn >> 16) & 0x1f, (insn >> 10) & 0x1f
    if 18 not in (rn, rm, rt2):
        return 0
    top8, top11 = insn >> 24, insn >> 21
    if (top8 & 0x3F) in (0x39, 0x3D) and rn == 18:
        return 1
    if (top11 & 0x1F9) == 0x1C1 and ((insn >> 10) & 3) == 2:
        if rn == 18:
            return 1
        if rm == 18:
            return 2
    if (top11 & 0x3E7) == 0x1C0 and rn == 18:
        return 1
    if (top8 & 0x3E) in (0x28, 0x2C):
        if rn == 18:
            return 1
        if rt2 == 18:
            return 3
    if (top11 & 0x7FF) in (0x150, 0x550) and rm == 18:
        return 2
    if (top8 & 0x1F) == 0x11 and rn == 18:
        return 1
    if (top8 & 0x1F) == 0x0B:
        if rn == 18:
            return 1
        if rm == 18:
            return 2
    return 0


def verify(path, stock=None):
    img = Pe(path)
    bad = []
    exps = img.exports()
    ranges = img.code_map()
    # an ARM64EC image says AMD64 in its header and carries a CHPE code map
    if img.machine != 0x8664 or not any(k == 1 for s, l, k in ranges):
        bad.append("not an ARM64EC image (machine %#x, code map %s)" % (img.machine, ranges))
    text = max((s for s in img.secs if s[5] & 0x20000000), key=lambda s: s[2])
    for name in X64_BODIES:
        rva = exps.get(name)
        if rva is None:
            bad.append("%s: not exported" % name)
            continue
        kind = [k for s, l, k in ranges if s <= rva < s + l]
        if kind != [2]:
            bad.append("%s: rva %#x is not in an X64 code range (%s)" % (name, rva, kind))
            continue
        want, mask = body_pattern(name)
        got = img.d[img.off(rva):img.off(rva) + len(want)]
        if any(m and g != w for g, w, m in zip(got, want, mask)) or len(got) != len(want):
            bad.append("%s: body %s does not match %s" % (name, got.hex(), X64_BODIES[name]))
            continue
        # every 4-byte word the x18 patcher reads over the body (functions are
        # 16-byte aligned, .text is 64 KB aligned) up to the next 16-byte boundary
        if text[1] <= rva < text[1] + text[2]:
            end = (rva + len(want) + 15) & ~15
            for w in range(rva & ~3, end, 4):
                insn = struct.unpack_from("<I", img.d, img.off(w))[0]
                if x18_role(insn):
                    bad.append("%s: word %08x at rva %#x looks like an x18 access to the x18 patcher"
                               % (name, insn, w))
    flag = exps.get(FLAG)
    if flag is None:
        bad.append("%s: not exported" % FLAG)
    else:
        sec = img.section_of(flag)
        if not sec or not sec[5] & 0x80000000:
            bad.append("%s: rva %#x is not in a writable section" % (FLAG, flag))
    if MARKER not in img.d:
        bad.append("marker %r missing" % MARKER.decode())
    # ios_patch_rtl_pc_to_file_header (virtual_ios.c) puts a 48-byte trampoline
    # 16 bytes past the 16-aligned end of the section that holds the function,
    # inside that section's last 16 KB page
    pc2fh = exps.get("RtlPcToFileHeader")
    if pc2fh is not None:
        o = img.off(pc2fh)   # the export is the fast-forward sequence; follow its jmp
        if img.d[o:o + 10] == FFS:
            pc2fh = pc2fh + 14 + struct.unpack_from("<i", img.d, o + 10)[0]
        else:
            pc2fh = None
    sec = img.section_of(pc2fh) if pc2fh is not None else None
    if not sec:
        bad.append("RtlPcToFileHeader: not found")
    else:
        lo = ((sec[1] + sec[2] + 15) & ~15) + 16
        hi = (sec[1] + sec[2] + 0x3fff) & ~0x3fff
        nxt = min([s[1] for s in img.secs if s[1] > sec[1]] + [img.size_of_image])
        if hi > nxt:
            hi = nxt
        if lo + 48 > hi:
            bad.append("no room for the RtlPcToFileHeader trampoline after %s (%#x..%#x)" % (sec[0], lo, hi))
    if stock:
        ref = Pe(stock).exports()
        extra = sorted(set(exps) - set(ref))
        missing = sorted(set(ref) - set(exps))
        if missing or extra != [FLAG]:
            bad.append("exports differ from %s: missing %s, extra %s" % (stock, missing[:8], extra[:8]))
    if bad:
        sys.exit("verify %s:\n  %s" % (path, "\n  ".join(bad)))
    print("verify %s: the nine SRW and critical-section exports are the x64 bodies in X64 code ranges, no x18-looking word, "
          "RtlPcToFileHeader's trampoline fits, %s exported, marker present%s"
          % (path, FLAG, ", exports = the stock set + " + FLAG if stock else ""))


def same_as(a, b):
    pa, pb = Pe(a), Pe(b)
    da, db = bytearray(pa.d), bytearray(pb.d)
    if len(da) != len(db):
        sys.exit("same-as: %s is %d bytes, %s %d" % (a, len(da), b, len(db)))
    for img, buf in ((pa, da), (pb, db)):
        for o in img.timestamp_offsets():
            buf[o:o + 4] = b"\0\0\0\0"
    if da != db:
        n = sum(1 for x, y in zip(da, db) if x != y)
        first = next(i for i, (x, y) in enumerate(zip(da, db)) if x != y)
        sys.exit("same-as: %s and %s differ in %d bytes beyond their time stamps (first at %#x)" % (a, b, n, first))
    print("same-as: %s and %s are identical apart from their time stamps" % (a, b))


if __name__ == "__main__":
    if len(sys.argv) in (3, 4) and sys.argv[1] == "--verify":
        verify(sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else None)
    elif len(sys.argv) == 4 and sys.argv[1] == "--same-as":
        same_as(sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 2 and not sys.argv[1].startswith("-"):
        patch(sys.argv[1])
    else:
        sys.exit(__doc__)
