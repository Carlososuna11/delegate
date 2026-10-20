"""Scope globs: which paths a delegate run may change.

`*` and `?` stay inside one path segment, `**` spans segments, and a pattern
with no wildcard names a file or a directory and matches everything under it.
Unlike fnmatch, `src/*.ts` does not match `src/deep/x.ts`: a scope must not
be wider than it reads.

Bracket classes follow the shell and git's wildmatch: `[abc]`, ranges
`[a-z]`, negation with a leading `!` or `^`, a `]` right after the opening
(or after the negation) is literal, and a backslash makes the next character
literal. A class never matches `/`. A pattern that cannot be translated is an
error, raised before any run starts.
"""
from __future__ import annotations

import functools
import re


class ScopeError(ValueError):
    pass


def _class(pattern: str, i: int) -> tuple[str, int] | None:
    """Translate the class opening at pattern[i] == "[". None if unclosed."""
    n = len(pattern)
    j = i + 1
    negate = j < n and pattern[j] in "!^"
    if negate:
        j += 1
    body = []
    first = True
    while j < n:
        c = pattern[j]
        if c == "]" and not first:
            break
        first = False
        if c == "\\" and j + 1 < n:
            body.append(re.escape(pattern[j + 1]))
            j += 2
            continue
        # `-` keeps its range meaning; everything else is literal inside [].
        body.append(c if c == "-" else re.escape(c))
        j += 1
    else:
        return None
    return f"(?!/)[{'^' if negate else ''}{''.join(body)}]", j + 1


@functools.lru_cache(maxsize=256)
def compile_glob(pattern: str) -> re.Pattern:
    if not any(c in pattern for c in "*?["):
        p = pattern.rstrip("/")
        return re.compile(re.escape(p) + r"(?:/.*)?\Z", re.S)
    out, i, n = [], 0, len(pattern)
    while i < n:
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append(r"(?:[^/]+/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(r".*")
            i += 2
        elif c == "*":
            out.append(r"[^/]*")
            i += 1
        elif c == "?":
            out.append(r"[^/]")
            i += 1
        elif c == "[":
            translated = _class(pattern, i)
            if translated is None:
                out.append(re.escape(c))
                i += 1
            else:
                out.append(translated[0])
                i = translated[1]
        else:
            out.append(re.escape(c))
            i += 1
    try:
        return re.compile("".join(out) + r"\Z", re.S)
    except re.error as exc:
        raise ScopeError(f"patrón de scope inválido {pattern!r}: {exc}") from None


def in_scope(path: str, scope) -> bool:
    return any(compile_glob(p).match(path) for p in scope)
