#!/usr/bin/env python3
"""Exercise real pinned rpmalloc; contrast the old and new arena geometry."""
from pathlib import Path
import importlib.util
import os
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]
source = root / "FEX/External/rpmalloc/rpmalloc/rpmalloc.c"
assert source.is_file(), "initialize FEX/External/rpmalloc at its pinned revision first"
spec = importlib.util.spec_from_file_location("span8", root / "tools/patch-fex-ios-rpmalloc-span8.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
original = source.read_text()
patched = module.patch(original)
assert module.patch(patched) == patched
for corrupt in (
    original.replace("#define LARGE_PAGE_SIZE_SHIFT 24", "#define LARGE_PAGE_SIZE_SHIFT 25"),
    patched.replace("#define LARGE_SIZE_CLASS_COUNT 19", "#define LARGE_SIZE_CLASS_COUNT 20"),
    patched.replace("#define SPAN_SIZE (8 * 1024 * 1024)", "#define SPAN_SIZE (4 * 1024 * 1024)"),
):
    try:
        module.patch(corrupt)
    except ValueError:
        pass
    else:
        raise AssertionError("changed or partial overlay must fail before writing")

build = (root / "tools/build-xtajit64.sh").read_text()
apply = build.index('python3 "$R/tools/patch-fex-ios-rpmalloc-span8.py"')
rebuild = build.index("\nbuild\n", apply)
restore = build.index("git -C FEX/External/rpmalloc checkout -- rpmalloc/rpmalloc.c", rebuild)
assert build.index("=== unpatched rebuild") < apply < rebuild < restore < build.index('cp "$B/Bin/libarm64ecfex.dll"')

with tempfile.TemporaryDirectory(prefix="rpmalloc-span8-") as directory:
    temp = Path(directory)
    for name in ("rpmalloc.h", "malloc.c"):
        (temp / name).write_bytes(source.with_name(name).read_bytes())
    env = dict(os.environ, ASAN_OPTIONS="detect_leaks=1", UBSAN_OPTIONS="halt_on_error=1")
    cc = os.environ.get("CC", "cc")
    for name, text in (("original", original), ("span8", patched)):
        unit = temp / "rpmalloc.c"
        unit.write_text(text)
        # The ordinary platform's spans, large pages and class ceiling remain
        # identical. Preprocessing also validates both sides of the new guards.
        macros = subprocess.check_output([cc, "-E", "-dM", "-I", str(temp), str(unit)], text=True)
        for definition in (
            "#define SPAN_SIZE (256 * 1024 * 1024)",
            "#define LARGE_PAGE_SIZE_SHIFT 26",
            "#define LARGE_SIZE_CLASS_COUNT 20",
            "#define LARGE_BLOCK_SIZE_LIMIT (8 * 1024 * 1024)",
        ):
            assert definition in macros, definition
        binary = temp / name
        subprocess.run([
            cc, "-std=gnu11", "-O1", "-g", "-DFEX_IOS_HOST",
            "-DRPMALLOC_FIRST_CLASS_HEAPS=1", "-DENABLE_DECOMMIT=1", "-DENABLE_OVERRIDE=0",
            "-DENABLE_ASSERTS=1", "-fsanitize=address,undefined", "-fno-sanitize-recover=all",
            "-Wno-unused-function", "-I", str(temp),
            str(root / "tests/host/rpmalloc-span8-fixture.c"), "-pthread", "-o", str(binary),
        ], check=True)
        mode = str(int(name == "span8"))
        subprocess.run([str(binary), "12", mode, "geometry"], env=env, check=True)
        subprocess.run([str(binary), "12", mode, "pressure"], env=env, check=True)
        if name == "span8":
            for gib in ("4", "8", "16"):
                subprocess.run([str(binary), gib, mode, "geometry"], env=env, check=True)
    assert source.read_text() == original, "host tests must not edit the pinned submodule"

print("PASS: strict/idempotent overlay, actual allocator boundaries and arena pressure, non-iOS geometry preserved")
