#!/usr/bin/env python3
"""Make native monitor reads of guest waiter addresses fault-safe.

msync can succeed on PROT_NONE mappings and cannot prevent an unmap between
the probe and the load. Read through Mach into a local buffer instead, and
publish data only after a complete successful read. The Wine pin stays intact.
"""
from pathlib import Path
import sys


MARKER = "madeira-bcd: fault-safe waiter diagnostics"
INCLUDES = "# include <mach/mach_time.h>\n"
HOT = """                    char *page = (char *)(row & ~0x3fffULL);
                    if (msync( page, 0x4000, MS_ASYNC )) continue;
                    if ((((row + 0x38) & ~0x3fffULL) != (ULONG_PTR)page) &&
                        msync( (char *)((row + 0x38) & ~0x3fffULL), 0x4000, MS_ASYNC )) continue;
                    memcpy( w, (void *)row, 0x40 );
"""
WORDS = """            char *page = (char *)(al & ~0x3fffULL);
            if (!msync( page, 0x4000, MS_ASYNC ))
            {
                w0 = *(volatile unsigned int *)al;
                if (((al + 4) & ~0x3fffULL) == (ULONG_PTR)page)
                    w1 = *(volatile unsigned int *)(al + 4);
            }
"""
COMMENT = """        /* safe-probe the lock word: msync rejects unmapped pages (Darwin
         * returns ENOMEM).  ml442: read the CONTAINING aligned word — SRW and
         * FEX WritePriorityMutex read-waits pass lock+2 (2-aligned), which the
         * old 4-aligned-only guard refused (ml441's deaddead trio). */
"""
HELPER = """/* madeira-bcd: fault-safe waiter diagnostics. The native monitor may see
 * stale or PROT_NONE guest addresses. A successful msync is not a readable
 * mapping guarantee. Mach performs the copy without a fault on this thread;
 * the temporary buffer also keeps failed/short reads out of the output. */
static int ios_alert_waiter_read( void *out, const void *addr, size_t size )
{
    unsigned char data[64];
    mach_vm_size_t got = 0;

    if (!size || size > sizeof(data)) return 0;
    if (mach_vm_read_overwrite( mach_task_self(), (mach_vm_address_t)(ULONG_PTR)addr,
                               size, (mach_vm_address_t)(ULONG_PTR)data, &got ) != KERN_SUCCESS ||
        got != size) return 0;
    memcpy( out, data, size );
    return 1;
}

"""


def patch(source):
    if MARKER in source:
        return source
    replacements = (
        (INCLUDES, INCLUDES + "# include <mach/mach.h>\n# include <mach/mach_vm.h>\n"),
        ("void ios_alert_waiter_dump(void)\n", HELPER + "void ios_alert_waiter_dump(void)\n"),
        (HOT, "                    if (!ios_alert_waiter_read( w, (void *)row, sizeof(w) )) continue;\n"),
        (COMMENT, """        /* Read the containing aligned word (SRW/read waits can pass lock+2).
         * A dead process may already have released or protected this page. */
"""),
        (WORDS, """            ios_alert_waiter_read( &w0, (void *)al, sizeof(w0) );
            if (((al + 4) & ~0x3fffULL) == (al & ~0x3fffULL))
                ios_alert_waiter_read( &w1, (void *)(al + 4), sizeof(w1) );
"""),
    )
    # Check every anchor before returning any changes to the caller.
    for old, _ in replacements:
        if source.count(old) != 1:
            raise ValueError("waiter-read anchor missing or ambiguous: " + old.splitlines()[0])
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
