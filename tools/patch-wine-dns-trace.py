#!/usr/bin/env python3
"""Name every getaddrinfo() a guest makes, and optionally bound how long it may take.

GTA V Enhanced build 378 (2026-10-04 11:07, pool-low = 1): the Rockstar Games
Launcher's SocialClubHelper.exe (Chromium, --single-process) connected to the
launcher and painted its first frames, then the launcher gave up after 80 s
("Social club UI in fail state", SC_INIT_ERR_WEBSITE_FAILED_LOAD). Stack
snapshots of that minute show three of Chromium's ThreadPoolForegroundWorker
threads parked in libsystem_info's getaddrinfo (via ws2_32's unix_getaddrinfo)
the whole time, while the launcher's own downloads in the same session
resolved fine. Nothing says which names they asked for.

This patch wraps the iOS getaddrinfo call in wine/dlls/ws2_32/unixlib.c:
  - `[dns] madeira-bcd begin #N "node" "service" family=.. flags=.. type=..`
    before the call (a call that never returns still leaves its name) and
    `[dns] madeira-bcd end #N ... = ret after T ms (K results)` after it; the
    first 400 calls of the session, then only calls slower than 1 s;
  - with MADEIRA_DNS_TIMEOUT_MS=<ms> (100..120000) the call runs on its own
    thread and, if it has not returned in time, the caller gets EAI_AGAIN
    (WSATRY_AGAIN) and `[dns] ... TIMED OUT`; the abandoned thread frees its
    result when getaddrinfo finally returns. Off by default.
The objc autorelease pool bracket stays around the real call, on whichever
thread makes it.

Usage: patch-wine-dns-trace.py wine/dlls/ws2_32/unixlib.c   (idempotent)
"""
import sys

path = sys.argv[1]
src = open(path).read()
if "madeira_dns_getaddrinfo" in src:
    print("already patched"); sys.exit(0)

old = """    extern void *objc_autoreleasePoolPush(void);
    extern void  objc_autoreleasePoolPop(void *);
    void *ios_arpool = objc_autoreleasePoolPush();
    ret = getaddrinfo( params->node, service, hints ? &unix_hints : NULL, &unix_info );
    objc_autoreleasePoolPop( ios_arpool );
#else"""
new = """    ret = madeira_dns_getaddrinfo( params->node, service, hints ? &unix_hints : NULL, &unix_info );
#else"""

helper_anchor = "static NTSTATUS unix_getaddrinfo( void *args )\n"
helper = r'''#ifdef WINE_IOS
/* madeira-bcd: see tools/patch-wine-dns-trace.py. */
#include <pthread.h>
#include <time.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

extern void *objc_autoreleasePoolPush(void);
extern void  objc_autoreleasePoolPop(void *);

struct madeira_dns_call
{
    pthread_mutex_t lock;
    pthread_cond_t done_cv;
    int refs, done, ret;
    char *node, *service;
    struct addrinfo hints, *result;
    int has_hints;
};

static int madeira_dns_real( const char *node, const char *service, const struct addrinfo *hints,
                             struct addrinfo **res )
{
    void *pool = objc_autoreleasePoolPush();
    int ret = getaddrinfo( node, service, hints, res );
    objc_autoreleasePoolPop( pool );
    return ret;
}

static void madeira_dns_release( struct madeira_dns_call *c )
{
    int last;
    pthread_mutex_lock( &c->lock );
    last = !--c->refs;
    pthread_mutex_unlock( &c->lock );
    if (!last) return;
    if (c->result) freeaddrinfo( c->result );   /* only when the caller gave up */
    free( c->node );
    free( c->service );
    pthread_cond_destroy( &c->done_cv );
    pthread_mutex_destroy( &c->lock );
    free( c );
}

static void *madeira_dns_thread( void *arg )
{
    struct madeira_dns_call *c = arg;
    struct addrinfo *res = NULL;
    int ret = madeira_dns_real( c->node, c->service, c->has_hints ? &c->hints : NULL, &res );

    pthread_mutex_lock( &c->lock );
    c->ret = ret;
    c->result = ret ? NULL : res;
    c->done = 1;
    pthread_cond_signal( &c->done_cv );
    pthread_mutex_unlock( &c->lock );
    madeira_dns_release( c );
    return NULL;
}

static long madeira_dns_ms_since( const struct timespec *t0 )
{
    struct timespec t1;
    clock_gettime( CLOCK_MONOTONIC, &t1 );
    return (long)(t1.tv_sec - t0->tv_sec) * 1000 + (t1.tv_nsec - t0->tv_nsec) / 1000000;
}

static int madeira_dns_getaddrinfo( const char *node, const char *service, const struct addrinfo *hints,
                                    struct addrinfo **res )
{
    static int calls, timeout_ms = -1;
    struct timespec t0;
    struct addrinfo *ai;
    int n = __sync_add_and_fetch( &calls, 1 ), ret, count = 0, timed_out = 0;
    long ms;

    if (timeout_ms < 0)
    {
        const char *env = getenv( "MADEIRA_DNS_TIMEOUT_MS" );
        long v = env ? strtol( env, NULL, 10 ) : 0;
        timeout_ms = (v >= 100 && v <= 120000) ? (int)v : 0;
    }
    if (n <= 400)
        fprintf( stderr, "[dns] madeira-bcd begin #%d \"%s\" \"%s\" family=%d flags=0x%x type=%d%s\n", n,
                 node ? node : "(null)", service ? service : "(null)", hints ? hints->ai_family : -1,
                 hints ? hints->ai_flags : 0, hints ? hints->ai_socktype : 0,
                 timeout_ms ? " (bounded)" : "" );
    clock_gettime( CLOCK_MONOTONIC, &t0 );

    *res = NULL;
    if (!timeout_ms)
        ret = madeira_dns_real( node, service, hints, res );
    else
    {
        struct madeira_dns_call *c = calloc( 1, sizeof(*c) );
        pthread_t thread;
        pthread_attr_t attr;
        struct timespec deadline;

        if (!c) return EAI_MEMORY;
        pthread_mutex_init( &c->lock, NULL );
        pthread_cond_init( &c->done_cv, NULL );
        c->refs = 2;
        c->node = node ? strdup( node ) : NULL;
        c->service = service ? strdup( service ) : NULL;
        if (hints) { c->hints = *hints; c->has_hints = 1; }
        pthread_attr_init( &attr );
        pthread_attr_setdetachstate( &attr, PTHREAD_CREATE_DETACHED );
        if (pthread_create( &thread, &attr, madeira_dns_thread, c ))
        {
            pthread_attr_destroy( &attr );
            c->refs = 1;
            madeira_dns_release( c );
            ret = madeira_dns_real( node, service, hints, res );
        }
        else
        {
            pthread_attr_destroy( &attr );
            clock_gettime( CLOCK_REALTIME, &deadline );
            deadline.tv_sec += timeout_ms / 1000;
            deadline.tv_nsec += (long)(timeout_ms % 1000) * 1000000;
            if (deadline.tv_nsec >= 1000000000) { deadline.tv_sec++; deadline.tv_nsec -= 1000000000; }
            pthread_mutex_lock( &c->lock );
            while (!c->done)
                if (pthread_cond_timedwait( &c->done_cv, &c->lock, &deadline ) == ETIMEDOUT) break;
            if (c->done)
            {
                ret = c->ret;
                *res = c->result;
                c->result = NULL;            /* ours now */
            }
            else
            {
                ret = EAI_AGAIN;
                timed_out = 1;
            }
            pthread_mutex_unlock( &c->lock );
            madeira_dns_release( c );
        }
    }

    ms = madeira_dns_ms_since( &t0 );
    for (ai = ret ? NULL : *res; ai; ai = ai->ai_next) count++;
    if (n <= 400 || ms >= 1000 || timed_out)
        fprintf( stderr, "[dns] madeira-bcd end #%d \"%s\" = %d after %ld ms (%d result(s))%s\n", n,
                 node ? node : "(null)", ret, ms, count,
                 timed_out ? " -- TIMED OUT, EAI_AGAIN to the caller (MADEIRA_DNS_TIMEOUT_MS)" : "" );
    return ret;
}
#endif

'''

for name, a in (("objc block", old), ("unix_getaddrinfo", helper_anchor)):
    if src.count(a) != 1:
        print(f"::error::patch-wine-dns-trace: {name} anchor not found exactly once -- ws2_32/unixlib.c changed")
        sys.exit(1)
src = src.replace(old, new, 1).replace(helper_anchor, helper + helper_anchor, 1)
open(path, "w").write(src)
print("patched")
