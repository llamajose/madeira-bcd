#!/usr/bin/env python3
"""[file-trace]: every guest file open / attribute query, success or not.

GTA V Enhanced stops at its own "Failed to load due to an incomplete
installation" screen (build 329, log PlayGTAV.exe 2026-10-02 12:56:30) and
the session log cannot say which file it missed: [file-fail] (ml665) stops
after 64 failures, all spent on the loader's DLL search in the game folder
before the game even starts, and GetFileAttributes-style checks
(NtQueryAttributesFile / NtQueryFullAttributesFile) are never logged.

With MADEIRA_FILE_TRACE=1 (env, e.g. the game's file: env.MADEIRA_FILE_TRACE
= 1) NtCreateFile/NtOpenFile (create_file path), NtQueryAttributesFile and
NtQueryFullAttributesFile log
  [file-trace] #N open|attr status=0x... disp=... access=... name=...
for up to MADEIRA_FILE_TRACE_LIMIT lines (default 4000), skipping only the
loader's expected misses (STATUS_OBJECT_NAME_NOT_FOUND on *.dll) and
Madeira's own shader cache (\\Madeira\\ShaderCache). Guest
results are never changed. Off: nothing is logged and nothing else runs but
one getenv per process.

Applied to the wine checkout after tools/patch-wine-guest-log.py (same
file); the submodule is not committed to. Idempotent; exits non-zero when an
anchor is missing.
Usage: patch-wine-file-trace.py [path/to/wine/dlls/ntdll/unix/file.c]
"""
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "wine/dlls/ntdll/unix/file.c"
src = open(path).read()
marker = "madeira-bcd: [file-trace]"
if marker in src:
    print("patch-wine-file-trace: already patched")
    sys.exit(0)

helper = r'''#ifdef WINE_IOS
/* madeira-bcd: [file-trace] (tools/patch-wine-file-trace.py). */
static void madeira_file_trace( const char *what, unsigned int status, const UNICODE_STRING *nt,
                                const OBJECT_ATTRIBUTES *attr, unsigned int disp, unsigned int access )
{
    static int on = -1, limit;
    static volatile int n;
    const WCHAR *w = NULL;
    unsigned int wl = 0, k = 0;
    char nb[300];
    int i;

    if (on < 0)
    {
        const char *e = getenv( "MADEIRA_FILE_TRACE" ), *l = getenv( "MADEIRA_FILE_TRACE_LIMIT" );
        limit = l ? atoi( l ) : 4000;
        if (limit <= 0 || limit > 100000) limit = 4000;
        on = e && e[0] == '1';
        if (on) dprintf( 2, "[file-trace] madeira-bcd on: every guest open / attribute query, %d lines (MADEIRA_FILE_TRACE)\n", limit );
    }
    if (!on || n >= limit) return;
    if (nt && nt->Buffer) { w = nt->Buffer; wl = nt->Length / sizeof(WCHAR); }
    else if (attr && attr->ObjectName && attr->ObjectName->Buffer)
    { w = attr->ObjectName->Buffer; wl = attr->ObjectName->Length / sizeof(WCHAR); }
    if (w) while (k < wl && k < sizeof(nb) - 1) { nb[k] = (w[k] < 32 || w[k] > 126) ? '?' : (char)w[k]; k++; }
    nb[k] = 0;
    /* Madeira's own shader cache (madeira_d3d12's .msc files) is not the game's I/O and
     * used most of the budget in the first GTA trace (log 2026-10-02 13:21:28). */
    if (strstr( nb, "\\Madeira\\ShaderCache" )) return;
    if (status == 0xc0000034 && k > 4)
    {
        const char *x = nb + k - 4;
        if ((x[0] == '.') && (x[1] | 0x20) == 'd' && (x[2] | 0x20) == 'l' && (x[3] | 0x20) == 'l') return;
    }
    i = __atomic_add_fetch( &n, 1, __ATOMIC_RELAXED );
    if (i > limit) return;
    dprintf( 2, "[file-trace] #%d %s status=0x%08x disp=%u access=0x%08x name=%s\n",
             i, what, status, disp, access, nb );
}

/* madeira-bcd: [pipe-trace] (MADEIRA_PIPE_TRACE=1). Social Club's game <-> helper
 * channel (\\.\pipe\chrome.rgsc_ipc_<pid>_channel_0, Chromium IPC) connects
 * (OnChannelConnected) but the game never asks the helper for a UI (GTA logs of
 * 2026-10-02/03). Log every NtWriteFile on a handle whose object name contains
 * "rgsc_": who writes, how much, and the first bytes, so both directions of the
 * exchange show up. The name is looked up once per (process, handle). */
static void madeira_pipe_trace( HANDLE handle, const void *buffer, ULONG length )
{
    static int on = -1, limit;
    static volatile int n;
    static struct { void *peb; HANDLE h; int rgsc; } cache[64];
    static int cache_n;
    void *peb = NtCurrentTeb()->Peb;
    int i, k, rgsc = -1, line;
    char hex[3 * 48 + 1], asc[97];
    const unsigned char *b = buffer;

    if (on < 0)
    {
        const char *e = getenv( "MADEIRA_PIPE_TRACE" ), *l = getenv( "MADEIRA_PIPE_TRACE_LIMIT" );
        limit = l ? atoi( l ) : 2000;
        if (limit <= 0 || limit > 100000) limit = 2000;
        on = e && e[0] == '1';
        if (on) dprintf( 2, "[pipe-trace] madeira-bcd on: writes to rgsc_ pipes, %d lines (MADEIRA_PIPE_TRACE)\n", limit );
    }
    if (!on || n >= limit) return;
    for (i = 0; i < cache_n; i++)
        if (cache[i].peb == peb && cache[i].h == handle) { rgsc = cache[i].rgsc; break; }
    if (rgsc < 0)
    {
        char buf[sizeof(OBJECT_NAME_INFORMATION) + 512];
        OBJECT_NAME_INFORMATION *oni = (OBJECT_NAME_INFORMATION *)buf;
        ULONG used = 0;
        rgsc = 0;
        if (!NtQueryObject( handle, ObjectNameInformation, oni, sizeof(buf), &used ) && oni->Name.Buffer)
        {
            const WCHAR *w = oni->Name.Buffer;
            unsigned int wl = oni->Name.Length / sizeof(WCHAR), j;
            for (j = 0; j + 5 <= wl && !rgsc; j++)
                rgsc = w[j] == 'r' && w[j + 1] == 'g' && w[j + 2] == 's' && w[j + 3] == 'c' && w[j + 4] == '_';
        }
        if (cache_n < 64) { cache[cache_n].peb = peb; cache[cache_n].h = handle; cache[cache_n].rgsc = rgsc; cache_n++; }
    }
    if (!rgsc || !b) return;
    for (k = 0; k < 48 && k < (int)length; k++) sprintf( hex + 3 * k, "%02x ", b[k] );
    hex[3 * k] = 0;
    for (k = 0; k < 96 && k < (int)length; k++) asc[k] = (b[k] < 32 || b[k] > 126) ? '.' : (char)b[k];
    asc[k] = 0;
    line = __atomic_add_fetch( &n, 1, __ATOMIC_RELAXED );
    if (line > limit) return;
    dprintf( 2, "[pipe-trace] #%d write peb=%p tid=%04x h=%p len=%u | %s| %s\n", line, peb,
             (unsigned int)HandleToULong( NtCurrentTeb()->ClientId.UniqueThread ), handle,
             (unsigned int)length, hex, asc );
}
#endif

'''

anchor_fn = "NTSTATUS WINAPI NtCreateFile( HANDLE *handle, ACCESS_MASK access, OBJECT_ATTRIBUTES *attr,"
if src.count(anchor_fn) != 1:
    sys.exit("patch-wine-file-trace: NtCreateFile anchor not found")
# The helper must precede create_file too; put it before the first of them.
first = min(i for i in (src.find("static NTSTATUS create_file("), src.find(anchor_fn)) if i >= 0)
line_start = src.rfind("\n/***", 0, first)
ins = line_start + 1 if line_start >= 0 else first
src = src[:ins] + helper + src[ins:]

open_anchor = "    if (status && ios_file_fail_logged < 64)\n"
if src.count(open_anchor) != 1:
    sys.exit("patch-wine-file-trace: [file-fail] anchor not found")
src = src.replace(open_anchor, "    madeira_file_trace( \"open\", status, &nt_name, attr, disposition, access );\n" + open_anchor)

attr_old = '''    else WARN( "%s not found (%x)\\n", debugstr_us(attr->ObjectName), status );
    free( unix_name );
    free( nt_name.Buffer );
    return status;'''
attr_new = '''    else WARN( "%s not found (%x)\\n", debugstr_us(attr->ObjectName), status );
#ifdef WINE_IOS
    madeira_file_trace( "attr", status, nt_name.Buffer ? &nt_name : NULL, attr, 0, 0 );
#endif
    free( unix_name );
    free( nt_name.Buffer );
    return status;'''
c = src.count(attr_old)
if c != 2:
    sys.exit("patch-wine-file-trace: expected the two attribute queries, found %d" % c)
src = src.replace(attr_old, attr_new)

write_old = '''    TRACE( "(%p,%p,%p,%p,%p,%p,0x%08x,%p,%p)\\n",
           handle, event, apc, apc_user, io, buffer, length, offset, key );

    if (!io) return STATUS_ACCESS_VIOLATION;'''
write_new = '''    TRACE( "(%p,%p,%p,%p,%p,%p,0x%08x,%p,%p)\\n",
           handle, event, apc, apc_user, io, buffer, length, offset, key );
#ifdef WINE_IOS
    madeira_pipe_trace( handle, buffer, length );
#endif

    if (!io) return STATUS_ACCESS_VIOLATION;'''
c = src.count(write_old)
if c < 1:
    sys.exit("patch-wine-file-trace: NtWriteFile anchor not found")
# NtWriteFile is the first function with this TRACE + io check
idx = src.index("NTSTATUS WINAPI NtWriteFile( HANDLE handle")
j = src.index(write_old, idx)
src = src[:j] + write_new + src[j + len(write_old):]

open(path, "w").write(src)
print("patch-wine-file-trace: [file-trace] in NtCreateFile and both attribute queries (MADEIRA_FILE_TRACE=1), "
      "[pipe-trace] in NtWriteFile (MADEIRA_PIPE_TRACE=1)")
