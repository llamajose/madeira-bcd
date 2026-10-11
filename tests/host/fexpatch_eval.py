"""Helpers for the FEX overlay checks: rebuild the ARM64EC overlay chain of
tools/build-xtajit64.sh in a temporary tree, and partially evaluate C++ sources
(preprocessor conditionals with a fixed macro set, comments removed) so two
versions of an emitter can be compared token for token.
"""
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

# Source trees the ARM64EC overlays read or write.
OVERLAY_TREES = ('FEXCore/Source', 'FEXCore/include', 'Source/Windows', 'External/rpmalloc/rpmalloc')


def overlay_chain(root):
    """The python3 overlay invocations of tools/build-xtajit64.sh, in order."""
    script = (root / 'tools/build-xtajit64.sh').read_text()
    cpuf = re.search(r'^CPUF="([^"]+)"', script, re.M).group(1)
    chain = []
    for line in script.splitlines():
        if line.startswith('python3 "$R/tools/patch-fex-ios-'):
            chain.append(line.replace('$CPUF', cpuf))
    return chain


def chain_tree(root, tmp, stop_before=None):
    """Copy the overlay inputs of root/FEX into tmp/FEX and apply the build's
    overlays in order, up to (not including) the script named stop_before."""
    fex = Path(tmp) / 'FEX'
    for rel in OVERLAY_TREES:
        shutil.copytree(root / 'FEX' / rel, fex / rel)
    applied = []
    for line in overlay_chain(root):
        args = shlex.split(line.replace('$R/FEX', str(fex)).replace('$R', str(root)))
        name = Path(args[1]).name
        if name == stop_before:
            break
        subprocess.run([sys.executable] + args[1:], check=True, capture_output=True)
        applied.append(name)
    return fex, applied


def strip_comments(text):
    """Remove // and /* */ comments, keeping string and character literals."""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '/' and i + 1 < n and text[i + 1] == '/':
            j = text.find('\n', i)
            i = n if j < 0 else j
        elif c == '/' and i + 1 < n and text[i + 1] == '*':
            j = text.find('*/', i + 2)
            seg = text[i:j + 2]
            out.append('\n' * seg.count('\n'))
            i = j + 2
        elif c in '"\'':
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == '\\' else 1
            out.append(text[i:j + 1])
            i = j + 1
        else:
            out.append(c)
            i += 1
    return ''.join(out)


def _cond(expr, macros):
    expr = re.sub(r'defined\s*\(\s*(\w+)\s*\)', lambda m: ' 1 ' if m.group(1) in macros else ' 0 ', expr)
    expr = re.sub(r'defined\s+(\w+)', lambda m: ' 1 ' if m.group(1) in macros else ' 0 ', expr)
    expr = expr.replace('&&', ' and ').replace('||', ' or ')
    expr = re.sub(r'!(?!=)', ' not ', expr)
    expr = re.sub(r'\b[A-Za-z_]\w*\b', lambda m: m.group(0) if m.group(0) in ('and', 'or', 'not')
                  else str(int(macros.get(m.group(0), 0))), expr)
    return bool(eval(expr, {}))


def preprocess(text, macros):
    """Keep only the lines active under `macros` (name -> value); directives dropped."""
    out = []
    stack = []  # (parent_active, this_active, any_taken)
    active = True
    lines = text.split('\n')
    k = 0
    while k < len(lines):
        line = lines[k]
        while line.rstrip().endswith('\\') and k + 1 < len(lines):
            k += 1
            line = line.rstrip()[:-1] + ' ' + lines[k]
        s = line.strip()
        m = re.match(r'#\s*(ifdef|ifndef|if|elif|else|endif)\b(.*)', s)
        if m:
            d, rest = m.group(1), m.group(2).strip()
            if d in ('ifdef', 'ifndef', 'if'):
                if d == 'ifdef':
                    c = rest.split()[0] in macros
                elif d == 'ifndef':
                    c = rest.split()[0] not in macros
                else:
                    c = _cond(rest, macros)
                stack.append((active, active and c, c))
                active = active and c
            elif d == 'elif':
                parent, _, taken = stack[-1]
                c = (not taken) and _cond(rest, macros)
                stack[-1] = (parent, parent and c, taken or c)
                active = parent and c
            elif d == 'else':
                parent, _, taken = stack[-1]
                stack[-1] = (parent, parent and not taken, True)
                active = parent and not taken
            else:
                parent, _, _ = stack.pop()
                active = parent
            out.append('')
        elif s.startswith('#'):
            out.append('')
        else:
            out.append(line if active else '')
        k += 1
    return '\n'.join(out)


TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[A-Za-z_]\w*|0[xX][0-9A-Fa-f\']+|\d[\d\']*|->|::|&&|\|\||==|!=|<=|>=|<<|>>|\S')


def tokens(text):
    return TOKEN.findall(text)


def match_brace(text, open_index):
    """Index of the brace closing the one at open_index (comment-free text)."""
    depth = 0
    i = open_index
    while i < len(text):
        c = text[i]
        if c in '"\'':
            j = i + 1
            while text[j] != c:
                j += 2 if text[j] == '\\' else 1
            i = j + 1
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError('unbalanced braces')


def body(text, header):
    """The text of the function or block whose opening line contains `header`."""
    start = text.index(header)
    brace = text.index('{', start)
    return text[start:match_brace(text, brace) + 1]
