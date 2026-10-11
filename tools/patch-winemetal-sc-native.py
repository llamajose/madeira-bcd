#!/usr/bin/env python3
"""Native file access for madeira-d3d12's shader cache (MadeiraCtl ops 20-22).

madeira-d3d12 keeps every converted shader stage in its own cache,
%LOCALAPPDATA%\\Madeira\\ShaderCache\\<build>\\<bb>\\<key>.msc, and read and
wrote those files through Wine's file API. Each read cost a create_file, a
get_handle_fd and a close_handle request to the wineserver and a realpath()
there. Horizon Zero Dawn's menu (build 493, 2026-10-11) read ~1,000 entries a
second, two per pipeline, at ~3.5 ms each; the three requests ran at ~990/s
each and kept the server thread at 0.5 of a performance core at ~3.9 GHz.

These ops reach the SAME files natively, relative to a descriptor of the build
directory that stays open: a read is openat, fstat, pread and close; a write
is the same temporary file renamed over the entry that Wine's MoveFileExW
made, with the bucket directory created when it is missing. Names, contents
and the directory layout are unchanged, so either way of access reads what
the other wrote, and madeira-d3d12's removal of other builds' directories
removes these files as before.

  op 20  open: ptr = the build directory's unix path; ret = a handle (1-8),
         0 when it cannot be opened. One directory gives one handle in every
         Wine process of the app.
  op 21  get: ptr = struct madeira_scn_io. cap > 0: ret 1 = the entry was
         read into data (len = its length), 2 = it is longer than cap (len =
         its length, nothing read). cap = 0: ret 2 = the entry is there (len).
         ret 0 = there is no such entry, 3 = it could not be read here (the
         caller then reads it through Wine).
  op 22  put: ptr = struct madeira_scn_io (data, len); ret 1 = the entry is
         in place, 0 = not written (the caller then writes it through Wine).

madeira-d3d12 uses them unless madeira.cfg / a game's config says
shader-cache-native = 0. Local in both backends; native only (the i386 entry
forwards op 7 alone). Idempotent; fails by name if an anchor moved or one of
the op numbers is taken. Run from the repository root.
"""
import pathlib
import re
import sys

PATH = pathlib.Path("dxmt/src/winemetal/unix/winemetal_unix.c")
MARKER = "madeira-bcd: native file access for the D3D12 shader cache"
OPS = (20, 21, 22)

ANCHOR_FN = "static NTSTATUS _madeira_ctl(void *args) {\n"
ANCHOR_DEFAULT = "  default: break;\n  }\n  return STATUS_SUCCESS;\n}\n"

HELPERS = r'''/* madeira-bcd: native file access for the D3D12 shader cache (tools/patch-winemetal-sc-native.py).
 * madeira-d3d12's cache entries, %LOCALAPPDATA%\Madeira\ShaderCache\<build>\<bb>\<key>.msc,
 * read and written here relative to an open descriptor of the build directory
 * instead of through Wine's file API (three wineserver requests and a server
 * realpath() per entry). The same files, names and contents. MadeiraCtl ops
 * 20 (open), 21 (get) and 22 (put); see the script. */
#include <pthread.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>
#include <time.h>

/* One get or put; madeira_d3d12.c (struct mad_scn_io) has the same layout. */
struct madeira_scn_io {
  uint64_t key[2];   /* the entry's key: file <key[0] >> 56 as %02x>/<key[0]><key[1] as %016llx>.msc */
  uint64_t data;     /* get: where the entry goes; put: the entry */
  uint64_t cap;      /* get: room at data (0: only whether the entry is there) */
  uint64_t len;      /* get: the entry's length (out); put: its length */
  uint32_t handle;   /* from op 20 */
  uint32_t flags;    /* 0 */
};
#define MADEIRA_SCN_MAX 8
#define MADEIRA_SCN_MAX_LEN (256ull << 20)   /* madeira_d3d12.c mad_sc_read_file's limit */

static struct { char dir[1024]; int fd; } madeira_scn_tab[MADEIRA_SCN_MAX];
static unsigned madeira_scn_n;
static pthread_mutex_t madeira_scn_mu = PTHREAD_MUTEX_INITIALIZER;
static uint32_t madeira_scn_nonce;   /* set at the first open, under the lock: this run's temporary names */
static _Atomic uint64_t madeira_scn_seq, madeira_scn_again;
static _Atomic uint32_t madeira_scn_errors;

static uint32_t madeira_scn_open(const char *dir) {
  uint32_t h = 0;
  unsigned i;
  if (!dir || !*dir || strlen(dir) >= sizeof madeira_scn_tab[0].dir) return 0;
  pthread_mutex_lock(&madeira_scn_mu);
  if (!madeira_scn_nonce) madeira_scn_nonce = (uint32_t)time(NULL) | 1u;   /* differs from an earlier run's leftovers */
  for (i = 0; i < madeira_scn_n && !h; i++)
    if (!strcmp(madeira_scn_tab[i].dir, dir)) h = i + 1;
  if (!h && madeira_scn_n < MADEIRA_SCN_MAX) {
    int fd = open(dir, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (fd >= 0) {
      snprintf(madeira_scn_tab[madeira_scn_n].dir, sizeof madeira_scn_tab[0].dir, "%s", dir);
      madeira_scn_tab[madeira_scn_n].fd = fd;
      h = ++madeira_scn_n;
    } else
      fprintf(stderr, "[sc-native] madeira-bcd cannot open the shader cache directory (errno %d); "
                      "madeira-d3d12 keeps Wine's file calls\n", errno);
  }
  pthread_mutex_unlock(&madeira_scn_mu);
  return h;
}

/* A handle's directory descriptor, -1 for none; entries never change once made. */
static int madeira_scn_dirfd(uint32_t h) {
  int fd = -1;
  pthread_mutex_lock(&madeira_scn_mu);
  if (h >= 1 && h <= madeira_scn_n) fd = madeira_scn_tab[h - 1].fd;
  pthread_mutex_unlock(&madeira_scn_mu);
  return fd;
}

/* "<bb>/<key[0]><key[1]>.msc", as madeira_d3d12.c mad_sc_path names it. */
static void madeira_scn_name(const uint64_t key[2], char out[64]) {
  snprintf(out, 64, "%02x/%016llx%016llx.msc", (unsigned)(key[0] >> 56),
           (unsigned long long)key[0], (unsigned long long)key[1]);
}

static void madeira_scn_error(const char *what, int e) {
  uint32_t n = atomic_fetch_add_explicit(&madeira_scn_errors, 1, memory_order_relaxed) + 1;
  if (n <= 8)
    fprintf(stderr, "[sc-native] madeira-bcd shader cache %s failed natively (errno %d); "
                    "that entry goes through Wine's file calls\n", what, e);
}

static uint32_t madeira_scn_get(struct madeira_scn_io *io) {
  char name[64];
  struct stat st;
  unsigned char *b = (unsigned char *)(uintptr_t)io->data;
  uint64_t off = 0;
  int dfd = madeira_scn_dirfd(io->handle), fd;
  if (dfd < 0) return 3;
  madeira_scn_name(io->key, name);
  if (!io->cap) {   /* is it there? (as GetFileAttributesW answers: anything by that name) */
    if (fstatat(dfd, name, &st, 0) != 0) {
      if (errno == ENOENT || errno == ENOTDIR) return 0;
      madeira_scn_error("lookup", errno);
      return 3;
    }
    io->len = (uint64_t)st.st_size;
    return 2;
  }
  fd = openat(dfd, name, O_RDONLY | O_CLOEXEC);
  if (fd < 0) {
    if (errno == ENOENT || errno == ENOTDIR) return 0;
    madeira_scn_error("read", errno);
    return 3;
  }
  /* Not a regular file, or past the reader's limit: the caller's Wine path decides, as before. */
  if (fstat(fd, &st) != 0 || !S_ISREG(st.st_mode) || st.st_size < 0 || (uint64_t)st.st_size > MADEIRA_SCN_MAX_LEN) {
    close(fd);
    return 3;
  }
  io->len = (uint64_t)st.st_size;
  if (io->len > io->cap || !b) { close(fd); return 2; }
  while (off < io->len) {
    ssize_t r = pread(fd, b + off, (size_t)(io->len - off), (off_t)off);
    if (r < 0 && errno == EINTR) continue;
    if (r <= 0) break;
    off += (uint64_t)r;
  }
  close(fd);
  return off == io->len ? 1u : 3u;
}

static uint32_t madeira_scn_put(const struct madeira_scn_io *io) {
  char name[64], tmp[96], bucket[4];
  const unsigned char *b = (const unsigned char *)(uintptr_t)io->data;
  uint64_t off = 0;
  int dfd = madeira_scn_dirfd(io->handle), fd = -1, tries, ok = 1;
  if (dfd < 0 || !b || !io->len || io->len > MADEIRA_SCN_MAX_LEN) return 0;
  madeira_scn_name(io->key, name);
  for (tries = 0; fd < 0 && tries < 4; tries++) {
    snprintf(tmp, sizeof tmp, "%s.%08x%llx.tmp", name, madeira_scn_nonce,
             (unsigned long long)atomic_fetch_add_explicit(&madeira_scn_seq, 1, memory_order_relaxed));
    fd = openat(dfd, tmp, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0666);
    if (fd < 0 && errno == ENOENT) {   /* no bucket directory yet: made as Wine's CreateDirectoryW makes it */
      snprintf(bucket, sizeof bucket, "%02x", (unsigned)(io->key[0] >> 56));
      if (mkdirat(dfd, bucket, 0777) == 0 || errno == EEXIST)
        fd = openat(dfd, tmp, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0666);
    }
    if (fd < 0 && errno != EEXIST) break;
  }
  if (fd < 0) { madeira_scn_error("write", errno); return 0; }
  while (off < io->len) {
    ssize_t w = pwrite(fd, b + off, (size_t)(io->len - off), (off_t)off);
    if (w < 0 && errno == EINTR) continue;
    if (w <= 0) { ok = 0; break; }
    off += (uint64_t)w;
  }
  if (close(fd) != 0) ok = 0;
  if (ok) {
    /* A count only: an entry already there was converted twice, by two threads
     * that missed the same key at once (or an earlier run's file is replaced). */
    struct stat st;
    int again = fstatat(dfd, name, &st, 0) == 0;
    if (renameat(dfd, tmp, dfd, name) == 0) {
      if (again) {
        uint64_t n = atomic_fetch_add_explicit(&madeira_scn_again, 1, memory_order_relaxed) + 1;
        if (n == 1 || n == 10 || n == 100 || n % 1000 == 0)
          fprintf(stderr, "[sc-native] madeira-bcd %llu shader-cache entries were already there when stored "
                          "(converted twice, e.g. by two threads at once)\n", (unsigned long long)n);
      }
      return 1;
    }
  }
  madeira_scn_error("write", errno);
  unlinkat(dfd, tmp, 0);
  return 0;
}

static uint32_t madeira_scn_ctl(struct madeira_ctl_args *a) {
  struct madeira_scn_io *io = (struct madeira_scn_io *)(uintptr_t)a->ptr;
  switch (a->op) {
  case 20: return madeira_scn_open((const char *)(uintptr_t)a->ptr);
  case 21: return io ? madeira_scn_get(io) : 3;
  case 22: return io ? madeira_scn_put(io) : 0;
  default: return 0;
  }
}

'''

CASES = """  case 20: case 21: case 22:   /* madeira-bcd: native file access for the D3D12 shader cache
                                * (tools/patch-winemetal-sc-native.py): open, get, put */
    a->ret = madeira_scn_ctl(a);
    break;
"""


def main():
    src = PATH.read_text()
    if MARKER in src:
        print("patch-winemetal-sc-native: already applied")
        return 0
    if src.count(ANCHOR_FN) != 1:
        sys.exit("patch-winemetal-sc-native: _madeira_ctl anchor not found once")
    fn_at = src.index(ANCHOR_FN)
    fn_end = src.find(ANCHOR_DEFAULT, fn_at)
    if fn_end < 0 or "\n}\n" in src[fn_at:fn_end]:
        sys.exit("patch-winemetal-sc-native: the end of _madeira_ctl (default: break; ... return) moved")
    body = src[fn_at:fn_end]
    for op in OPS:
        if re.search(r"^\s*case %d:" % op, body, re.M):
            sys.exit("patch-winemetal-sc-native: _madeira_ctl already has a case %d (pick free ops)" % op)
    if "madeira_scn_" in src:
        sys.exit("patch-winemetal-sc-native: madeira_scn_ names are already taken")
    src = src[:fn_end] + CASES + src[fn_end:]
    src = src.replace(ANCHOR_FN, HELPERS + ANCHOR_FN)
    PATH.write_text(src)
    print("patch-winemetal-sc-native: MadeiraCtl ops 20-22 read and write madeira-d3d12's shader cache files natively "
          "(madeira.cfg shader-cache-native = 0 keeps Wine's file calls)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
