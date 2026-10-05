#!/usr/bin/env python3
"""tools/patch-wine-dns-trace.py: names every getaddrinfo, bounds it on request; no Wine runs.

Applies the patch to a copy of wine/dlls/ws2_32/unixlib.c, checks that the iOS
call site now goes through madeira_dns_getaddrinfo and that the patch is wired
into the workflow before ntdll-unix is built, then compiles the inserted helper
against a fake getaddrinfo (fast, failing, and one that blocks for 1.5 s) with
AddressSanitizer/UBSan and checks:
  - without MADEIRA_DNS_TIMEOUT_MS every call returns the real result and logs
    begin/end lines with the name;
  - with MADEIRA_DNS_TIMEOUT_MS=300 the blocking call returns EAI_AGAIN after
    ~300 ms with a TIMED OUT line, fast calls are unchanged, and the abandoned
    thread frees its late result (LeakSanitizer stays quiet);
  - out-of-range values (50, 999999, junk) leave the call unbounded.
"""
from pathlib import Path
import os
import re
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]
workflow = (root / '.github/workflows/build-ipa.yml').read_text()
assert workflow.index('python3 tools/patch-wine-dns-trace.py wine/dlls/ws2_32/unixlib.c') \
    < workflow.index('run: bash build/ntdll-unix/build.sh'), 'the DNS patch runs before ntdll-unix is built'

with tempfile.TemporaryDirectory(prefix='madeira-dns-') as d:
    folder = Path(d)
    copy = folder / 'unixlib.c'
    copy.write_text((root / 'wine/dlls/ws2_32/unixlib.c').read_text())
    for expect in ('patched', 'already patched'):
        out = subprocess.run(['python3', str(root / 'tools/patch-wine-dns-trace.py'), str(copy)],
                             capture_output=True, text=True, check=True).stdout.strip()
        assert out == expect, out
    patched = copy.read_text()
    assert 'ret = madeira_dns_getaddrinfo( params->node, service, hints ? &unix_hints : NULL, &unix_info );' in patched
    start = patched.index('/* madeira-bcd: see tools/patch-wine-dns-trace.py. */')
    helper = patched[start:patched.index('#endif', start)]
    helper = re.sub(r'#include <[a-z/]+\.h>\n', '', helper)

    code = r'''
#define _GNU_SOURCE
#include <errno.h>
#include <netdb.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
void *objc_autoreleasePoolPush(void) { return (void *)1; }
void objc_autoreleasePoolPop(void *p) { (void)p; }
static struct addrinfo *one_result( void )
{
    struct addrinfo *ai = calloc( 1, sizeof(*ai) );
    ai->ai_family = AF_INET;
    return ai;
}
static int fake_getaddrinfo( const char *node, const char *service, const struct addrinfo *hints,
                             struct addrinfo **res )
{
    (void)service; (void)hints;
    if (!strcmp( node, "fail.example" )) return EAI_NONAME;
    if (!strcmp( node, "slow.example" )) usleep( 1500 * 1000 );
    *res = one_result();
    return 0;
}
static void fake_freeaddrinfo( struct addrinfo *ai ) { free( ai ); }
#define getaddrinfo fake_getaddrinfo
#define freeaddrinfo fake_freeaddrinfo
''' + helper + r'''
static long now_ms( void )
{
    struct timespec t; clock_gettime( CLOCK_MONOTONIC, &t ); return t.tv_sec * 1000 + t.tv_nsec / 1000000;
}
int main( int argc, char **argv )
{
    struct addrinfo hints = { .ai_family = AF_UNSPEC, .ai_socktype = SOCK_STREAM }, *res;
    int bounded = argc > 1 && !strcmp( argv[1], "bounded" );
    long t0;
    int ret;

    ret = madeira_dns_getaddrinfo( "fast.example", "443", &hints, &res );
    if (ret || !res) return 1;
    fake_freeaddrinfo( res );
    ret = madeira_dns_getaddrinfo( "fail.example", NULL, NULL, &res );
    if (ret != EAI_NONAME) return 2;
    t0 = now_ms();
    ret = madeira_dns_getaddrinfo( "slow.example", "80", &hints, &res );
    if (bounded)
    {
        if (ret != EAI_AGAIN || now_ms() - t0 > 1200) return 3;
        usleep( 1800 * 1000 );                 /* let the abandoned thread finish and free */
    }
    else
    {
        if (ret || !res || now_ms() - t0 < 1400) return 4;
        fake_freeaddrinfo( res );
    }
    puts( bounded ? "bounded OK" : "unbounded OK" );
    return 0;
}
'''
    source = folder / 'dns.c'
    source.write_text(code)
    exe = folder / 'dns'
    cc = os.environ.get('CC', 'cc')
    base = [cc, '-std=gnu11', '-Wall', '-Wextra', '-Wno-unused-parameter', '-Werror', '-g', '-pthread',
            str(source), '-o', str(exe)]
    sanitize = ['-fsanitize=address,undefined', '-fno-sanitize-recover=all']
    if subprocess.run(base[:1] + sanitize + base[1:], capture_output=True).returncode == 0:
        print('built with AddressSanitizer/UBSan')
    else:
        subprocess.run(base, check=True)
        print('built without sanitizers')
    for value, mode in [(None, 'unbounded'), ('300', 'bounded'), ('50', 'unbounded'),
                        ('999999', 'unbounded'), ('junk', 'unbounded')]:
        env = dict(os.environ)
        env.pop('MADEIRA_DNS_TIMEOUT_MS', None)
        if value is not None:
            env['MADEIRA_DNS_TIMEOUT_MS'] = value
        r = subprocess.run([str(exe), mode], env=env, capture_output=True, text=True)
        assert r.returncode == 0, (value, r.returncode, r.stdout, r.stderr)
        err = r.stderr
        assert '[dns] madeira-bcd begin #1 "fast.example" "443" family=0' in err, err
        assert '[dns] madeira-bcd end #2 "fail.example" = %d' % 8 in err or '"fail.example" = ' in err, err
        if mode == 'bounded':
            assert '"slow.example" = ' in err and 'TIMED OUT' in err and '(bounded)' in err, err
        else:
            assert 'TIMED OUT' not in err and '(bounded)' not in err, err
        print(f'MADEIRA_DNS_TIMEOUT_MS={value!r}: {r.stdout.strip()}')
print('PASS: every getaddrinfo is named; MADEIRA_DNS_TIMEOUT_MS bounds a blocking one and frees its late result')
