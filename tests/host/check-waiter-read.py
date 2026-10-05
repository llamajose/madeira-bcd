#!/usr/bin/env python3
"""Exercise the monitor's fault-safe reads against real protected/unmapped pages.

On this host a signal-guarded copy models Mach's fault-safe copy. The compiled
production helper must reject failed/short reads without changing its output.
The patch is applied twice to a temporary Wine source copy, leaving the pin
unchanged. Needs a Linux host, Python and a C compiler; no Wine or game binary.
"""
from pathlib import Path
import os
import subprocess
import tempfile


root = Path(__file__).resolve().parents[2]
workflow = (root / '.github/workflows/build-ipa.yml').read_text()
assert workflow.index('python3 tools/patch-wine-waiter-read.py') < workflow.index(
    'run: bash build/ntdll-unix/build.sh'), 'patch must run before ntdll compilation'

with tempfile.TemporaryDirectory(prefix='madeira-waiter-read-') as name:
    folder = Path(name)
    source = folder / 'sync.c'
    source.write_text((root / 'wine/dlls/ntdll/unix/sync.c').read_text())
    script = str(root / 'tools/patch-wine-waiter-read.py')
    for expected in ('patched', 'already patched'):
        result = subprocess.run(['python3', script, str(source)], check=True,
                                capture_output=True, text=True)
        assert result.stdout.strip() == expected, result.stdout
    patched = source.read_text()
    start = patched.index('static int ios_alert_waiter_read(')
    helper = patched[start:patched.index('\n}', start) + 2]
    start = patched.index('void ios_alert_waiter_dump(void)')
    dump = patched[start:patched.index('\n}', start) + 2]
    assert 'msync(' not in dump and '*(volatile unsigned int *)' not in dump
    assert 'ios_alert_waiter_read( w, (void *)row, sizeof(w) )' in dump
    assert 'ios_alert_waiter_read( &w0, (void *)al, sizeof(w0) )' in dump
    assert 'ios_alert_waiter_read( &w1, (void *)(al + 4), sizeof(w1) )' in dump

    # A moved anchor must fail without partially changing the target.
    broken = folder / 'broken.c'
    before = (root / 'wine/dlls/ntdll/unix/sync.c').read_text().replace(
        'void ios_alert_waiter_dump(void)', 'void changed_waiter_dump(void)')
    broken.write_text(before)
    result = subprocess.run(['python3', script, str(broken)], capture_output=True, text=True)
    assert result.returncode and broken.read_text() == before

    code = r'''
#define _GNU_SOURCE
#include <assert.h>
#include <stdint.h>
#include <stdio.h>
#include <signal.h>
#include <setjmp.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>
typedef uintptr_t ULONG_PTR;
typedef uintptr_t mach_vm_address_t;
typedef size_t mach_vm_size_t;
#define KERN_SUCCESS 0
static int mach_task_self(void) { return 0; }
static int forced_short, calls;
static sigjmp_buf copy_fault;
static void read_fault(int signal)
{
    (void)signal;
    siglongjmp(copy_fault, 1);
}
static int mach_vm_read_overwrite(int task, mach_vm_address_t from, mach_vm_size_t size,
                                  mach_vm_address_t to, mach_vm_size_t *got)
{
    volatile const unsigned char *source = (void *)from;
    unsigned char *dest = (void *)to;
    size_t count = forced_short ? size - 1 : size;
    (void)task;
    calls++;
    if (sigsetjmp(copy_fault, 1)) return 1;
    for (size_t i = 0; i < count; i++) dest[i] = source[i];
    *got = count;
    return KERN_SUCCESS;
}
''' + helper + r'''
int main(void)
{
    size_t page = (size_t)sysconf(_SC_PAGESIZE);
    unsigned char *memory = mmap(NULL, page * 2, PROT_READ | PROT_WRITE,
                                MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    unsigned char out[64];
    unsigned int word;
    struct sigaction action = { .sa_handler = read_fault };
    int before;
    sigemptyset(&action.sa_mask);
    assert(!sigaction(SIGSEGV, &action, NULL));
    assert(!sigaction(SIGBUS, &action, NULL));
    assert(memory != MAP_FAILED);
    memset(memory, 0x35, page * 2);
    memset(out, 0xa5, sizeof(out));
    assert(ios_alert_waiter_read(out, memory, sizeof(out)));
    for (size_t i = 0; i < sizeof(out); i++) assert(out[i] == 0x35);
    assert(ios_alert_waiter_read(&word, memory + 2, sizeof(word)) && word == 0x35353535);

    assert(!mprotect(memory + page, page, PROT_NONE));
    // Regression: msync succeeds even though a direct load would fault.
    assert(!msync(memory + page, page, MS_ASYNC));
    memset(out, 0xa5, sizeof(out));
    assert(!ios_alert_waiter_read(out, memory + page, sizeof(out)));
    for (size_t i = 0; i < sizeof(out); i++) assert(out[i] == 0xa5);
    assert(!ios_alert_waiter_read(out, memory + page - 32, sizeof(out)));
    for (size_t i = 0; i < sizeof(out); i++) assert(out[i] == 0xa5);

    forced_short = 1;
    assert(!ios_alert_waiter_read(out, memory, sizeof(out)));
    for (size_t i = 0; i < sizeof(out); i++) assert(out[i] == 0xa5);
    forced_short = 0;
    before = calls;
    assert(!ios_alert_waiter_read(out, memory, 0));
    assert(!ios_alert_waiter_read(out, memory, sizeof(out) + 1));
    assert(calls == before);

    assert(!munmap(memory, page * 2));
    assert(!ios_alert_waiter_read(out, memory, sizeof(out)));
    for (size_t i = 0; i < sizeof(out); i++) assert(out[i] == 0xa5);
    puts("PASS: readable, PROT_NONE, cross-page, short, invalid-size and unmapped reads");
    return 0;
}
'''
    driver = folder / 'test.c'
    driver.write_text(code)
    binary = folder / 'test'
    subprocess.run([os.environ.get('CC', 'cc'), '-std=c11', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=address,undefined', '-g', str(driver), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
    print('PASS: patch is idempotent, refuses moved anchors and protects both monitor read sites')
