#!/usr/bin/env python3
"""Use coherent 8 MiB rpmalloc spans in the fork's ARM64EC build copy.

The span mask, large page size and size-class ceiling must change together.
An 8 MiB block cannot fit beside the header in an 8 MiB large page; route
sizes above 7 MiB through the existing exact-size huge path. Leave the
non-FEX_IOS_HOST configuration, ownership, locks and band limits unchanged.
This is a Madeira build overlay, not a contribution to the pinned submodule.
"""
from pathlib import Path
import sys


MARKER = "madeira-bcd: coherent 8 MiB spans"
EDITS = (
    (
        "#define LARGE_BLOCK_SIZE_LIMIT (8 * 1024 * 1024)",
        "#ifdef FEX_IOS_HOST\n"
        "/* madeira-bcd: the largest class that fits beside an 8 MiB page's header. */\n"
        "#define LARGE_BLOCK_SIZE_LIMIT (7 * 1024 * 1024)\n"
        "#else\n#define LARGE_BLOCK_SIZE_LIMIT (8 * 1024 * 1024)\n#endif",
    ),
    (
        "#define MEDIUM_SIZE_CLASS_COUNT 24\n#define LARGE_SIZE_CLASS_COUNT 20",
        "#define MEDIUM_SIZE_CLASS_COUNT 24\n"
        "#ifdef FEX_IOS_HOST\n#define LARGE_SIZE_CLASS_COUNT 19\n"
        "#else\n#define LARGE_SIZE_CLASS_COUNT 20\n#endif",
    ),
    (
        "#define LARGE_PAGE_SIZE_SHIFT 24\n#else",
        "/* madeira-bcd: paired with the 8 MiB span and its 7 MiB class ceiling. */\n"
        "#define LARGE_PAGE_SIZE_SHIFT 23\n#else",
    ),
    (
        'static const char ios_span16_marker[] __attribute__((used)) = "rpmalloc-span16 rev=ml436";\n'
        "#define SPAN_SIZE (16 * 1024 * 1024)",
        "/* " + MARKER + ": halve both reservation size and alignment.\n"
        " * Keeping a 16 MiB mask/alignment with smaller reservations still\n"
        " * exhausts aligned slots. All page/header lookup masks derive below;\n"
        " * live-span ownership and the protected FEX band are unchanged. */\n"
        'static const char ios_span8_marker[] __attribute__((used)) = "rpmalloc-span8 madeira-bcd";\n'
        "#define SPAN_SIZE (8 * 1024 * 1024)",
    ),
    (
        "LCLASS(262144), LCLASS(327680), LCLASS(393216), LCLASS(458752), LCLASS(524288)};",
        "LCLASS(262144), LCLASS(327680), LCLASS(393216), LCLASS(458752)\n"
        "#ifndef FEX_IOS_HOST\n    , LCLASS(524288)\n#endif\n};",
    ),
)


def patch(source):
    if MARKER in source:
        if all(source.count(after) == 1 for _, after in EDITS):
            return source
        raise ValueError("partial or changed span8 overlay")
    for before, _ in EDITS:
        if source.count(before) != 1:
            raise ValueError("rpmalloc anchor changed: " + before[:100])
    for before, after in EDITS:
        source = source.replace(before, after, 1)
    return source


if __name__ == "__main__":
    path = Path(sys.argv[1])
    source = path.read_text()
    try:
        result = patch(source)
    except ValueError as error:
        sys.exit("patch-fex-ios-rpmalloc-span8: " + str(error))
    if result != source:
        path.write_text(result)
    print("rpmalloc iOS: span/alignment/large page=8MiB, classes<=7MiB; larger requests use the huge path")
