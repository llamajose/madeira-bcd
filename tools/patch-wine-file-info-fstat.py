#!/usr/bin/env python3
"""File size, index and end-of-file queries with one fstat (NtQueryInformationFile).

NtQueryInformationFile answers FileStandardInformation (GetFileSizeEx,
GetFileInformationByHandleEx), FileInternalInformation and FileEndOfFileInformation
through fd_get_file_info, which after its fstat reads two extended attributes (the
reparse tag and the DOS attributes) to compute the file's attributes. On Apple each of
those reads starts with flistxattr (xattr_exists), so a size query is one fstat and two
or more xattr calls, each checked by the sandbox. fill_file_info never looks at the
attributes for these three classes (only st: st_mode, st_size, st_blocks, st_nlink,
st_ino), and fd_get_file_info fails only when its fstat fails, with fstat's errno; its
other work (the attribute reads, the mount-point check for FILE_OPEN_REPARSE_POINT)
only adds to the attributes. So answering these three classes from fstat alone gives
the same result, status and errno, without the xattr calls. madeira-d3d12's shader
cache asks for the size of every file it reads (~940 a second in Horizon Zero Dawn's
menu, build 493). env.MADEIRA_FILE_INFO_FULL = 1 (read once) goes through
fd_get_file_info as before.

Applied to the wine checkout (wine/dlls/ntdll/unix/file.c) by CI before ntdll-unix is
built; the submodule is not committed to. Idempotent; exits non-zero when an anchor is
missing. Usage: patch-wine-file-info-fstat.py [path/to/wine/dlls/ntdll/unix/file.c]
"""
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "wine/dlls/ntdll/unix/file.c"
src = open(path).read()
marker = "madeira-bcd: madeira_fd_stat_only"
if marker in src:
    print("patch-wine-file-info-fstat: already patched")
    sys.exit(0)

helper = r'''#ifdef WINE_IOS
/* madeira-bcd: madeira_fd_stat_only (tools/patch-wine-file-info-fstat.py). The size,
 * index and end-of-file classes never use the attributes fd_get_file_info reads from
 * extended attributes, and it fails only when its fstat fails: fstat alone answers them
 * the same, without two xattr reads per query. MADEIRA_FILE_INFO_FULL=1 restores the call. */
static int madeira_fd_stat_only( HANDLE handle, int fd, unsigned int options, struct stat *st, ULONG *attr )
{
    static int full = -1;

    if (full < 0)
    {
        const char *e = getenv( "MADEIRA_FILE_INFO_FULL" );
        full = e && e[0] == '1';
    }
    if (full) return fd_get_file_info( handle, fd, options, st, attr, NULL );
    *attr = 0;
    return fstat( fd, st );
}
#endif

'''

anchor_fn = "NTSTATUS WINAPI NtQueryInformationFile( HANDLE handle, IO_STATUS_BLOCK *io,"
if src.count(anchor_fn) != 1:
    sys.exit("patch-wine-file-info-fstat: NtQueryInformationFile anchor not found")
fn = src.index(anchor_fn)
head = src.rfind("\n/***", 0, fn)
if head < 0 or src.count("static int fd_get_file_info(", 0, head) != 1:
    sys.exit("patch-wine-file-info-fstat: fd_get_file_info must come before NtQueryInformationFile")
src = src[:head + 1] + helper + src[head + 1:]
fn = src.index(anchor_fn)
end = src.index("\n}\n", fn)
body = src[fn:end]

old_if = "if (fd_get_file_info( handle, fd, options, &st, &attr, NULL ) == -1)\n"
new_if = ("\n#ifdef WINE_IOS\n%sif (madeira_fd_stat_only( handle, fd, options, &st, &attr ) == -1)\n"
          "#else\n%sif (fd_get_file_info( handle, fd, options, &st, &attr, NULL ) == -1)\n#endif\n")
cases = (
    ("    case FileStandardInformation:\n        {\n            FILE_STANDARD_INFORMATION *info = ptr;\n\n",
     "            "),
    ("    case FileInternalInformation:\n", "        "),
    ("    case FileEndOfFileInformation:\n", "        "),
)
for label, indent in cases:
    case = label + indent + old_if
    if body.count(case) != 1:
        sys.exit("patch-wine-file-info-fstat: anchor not found in NtQueryInformationFile: %r" % label.splitlines()[0].strip())
    body = body.replace(case, label[:-1] + new_if % (indent, indent))
src = src[:fn] + body + src[end:]

# fill_file_info must not use the attributes for these classes (the reason this is equivalent)
ff = src.index("static NTSTATUS fill_file_info( const struct stat *st, ULONG attr, void *ptr,")
ff_end = src.index("\n}\n", ff)
ffb = src[ff:ff_end]
for cls in ("FileStandardInformation", "FileInternalInformation", "FileEndOfFileInformation"):
    i = ffb.find("    case %s:" % cls)
    j = ffb.find("break;", i)
    if i < 0 or j < 0 or "attr" in ffb[i:j]:
        sys.exit("patch-wine-file-info-fstat: fill_file_info's %s case uses the attributes now" % cls)

open(path, "w").write(src)
print("patch-wine-file-info-fstat: FileStandardInformation, FileInternalInformation and FileEndOfFileInformation "
      "from fstat alone (MADEIRA_FILE_INFO_FULL=1 restores fd_get_file_info)")
