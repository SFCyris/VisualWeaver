# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run a JavaScript snippet under node via a temp FILE, not `node -e`.

`node -e <src>` passes the whole program as one argv element. Linux caps a single
argument at MAX_ARG_STRLEN (128 KB) regardless of ARG_MAX, so the larger snippets
here (the whole renderMarkdown pipeline, or a ReDoS input) raise
`OSError: [Errno 7] Argument list too long` on CI while passing on macOS, whose
limit is far higher. Writing the program to a file and running `node <file>`
removes the limit and behaves identically everywhere. Fixtures are required by
absolute path, so the temp file's location does not matter.
"""
import os
import subprocess
import tempfile


def run_node(js: str, timeout: int = 60) -> subprocess.CompletedProcess:
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                     encoding="utf-8") as tf:
        tf.write(js)
        path = tf.name
    try:
        return subprocess.run(["node", path], capture_output=True,
                              text=True, timeout=timeout)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def extract_js_function(source: str, name: str) -> str:
    """Return exactly one function's source, matched by brace balance.

    Slicing from `function name(` to the next line-initial `}` is wrong for a one-liner:
    A one-liner such as `_ragKey` slices on to the next function's closing brace
    and swallowed the whole of `_tokenUsageCardHTML` — which the test payload then defined
    twice. `escHtml` likewise dragged in five unrelated helpers. Neither broke a test, but
    the extraction was not doing what it read as, and a genuine second definition would
    silently shadow the first.

    Braces inside strings, template literals, regex literals and comments are skipped, so
    the count reflects real block structure.
    """
    start = source.index(f"function {name}(")
    # Keep a leading `async`: slicing from `function` alone drops it, and the extracted
    # body's `await` then fails to parse as "await is only valid in async functions".
    if source[max(0, start - 6):start] == "async ":
        start -= 6
    i = source.index("{", start)
    depth, j, n = 0, i, len(source)
    while j < n:
        # The SHARED scanner, not a second copy. The copy that used to live here
        # kept every defect _skip_token was fixed for — a regex after a
        # line-broken operator, a `${}` in a template, a `/` inside a character
        # class, and an unterminated block comment whose find(...)+2 jumped
        # BACKWARDS to index 1 and looped forever.
        nxt = _skip_token(source, j, n)
        if nxt is not None:
            j = nxt
            continue
        c = source[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return source[start:j + 1]
        j += 1
    raise ValueError(f"unbalanced braces extracting {name}()")


def _skip_token(source, j, n):
    """Advance past a string, template literal, comment or regex literal.

    Returns the new index, or None when `source[j]` starts none of those.

    Three cases the first version got wrong, each of which silently truncated a
    lane list rather than failing:

    * the "is this `/` a regex?" back-scan skipped only spaces and tabs, so a
      regex whose preceding operator ended the previous LINE was read as
      division and its `}` closed the object;
    * template literals did not understand `${...}`, so a `}` inside an
      interpolation ended the scan;
    * regex literals did not track `[...]`, and `/[/]}/` is legal.
    """
    c = source[j]
    if c in "\"'":
        quote, j = c, j + 1
        while j < n and source[j] != quote:
            j += 2 if source[j] == "\\" else 1
        return j + 1
    if c == "`":
        j += 1
        while j < n:
            ch = source[j]
            if ch == "\\":
                j += 2
                continue
            if ch == "`":
                return j + 1
            if ch == "$" and j + 1 < n and source[j + 1] == "{":
                depth, j = 1, j + 2          # walk the interpolation out
                while j < n and depth:
                    nxt = _skip_token(source, j, n)
                    if nxt is not None:
                        j = nxt
                        continue
                    if source[j] == "{":
                        depth += 1
                    elif source[j] == "}":
                        depth -= 1
                    j += 1
                continue
            j += 1
        return n
    if c == "/" and j + 1 < n:
        if source[j + 1] == "/":
            k = source.find("\n", j)
            return n if k < 0 else k
        if source[j + 1] == "*":
            k = source.find("*/", j)
            # find() returning -1 made this jump BACKWARDS to index 1 on an
            # unterminated comment — an infinite loop rather than an error.
            return n if k < 0 else k + 2
        k = j - 1
        while k >= 0 and source[k] in " \t\r\n":
            k -= 1
        # A regex can also follow a word operator. `return /,\d{3}$/.test(t)` is
        # already in this codebase, and reading its `/` as division let the braces
        # and quotes inside the pattern corrupt the depth count — five lanes
        # vanished from the extraction, silently.
        word = ""
        w = k
        while w >= 0 and (source[w].isalpha() or source[w] == "_"):
            w -= 1
        if w < k:
            word = source[w + 1:k + 1]
        if (k >= 0 and source[k] in "=(,:[!&|?{};+-*%<>~^") or word in (
                "return", "typeof", "case", "in", "of", "new", "delete", "void",
                "do", "else", "yield", "await", "throw"):
            j += 1
            in_class = False
            while j < n:
                ch = source[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    return j + 1
                elif ch == "\n":
                    break                     # not a regex after all
                j += 1
            return j
    return None


def rich_lane_names(index_html: "os.PathLike | str") -> list:
    """Top-level keys of the RICH_LANES object literal.

    Counting raw braces looked fine and was luck: the literal holds 139 braces
    inside strings, regex literals, template literals and comments
    ('https://{s}.tile…/{z}/{x}/{y}.png', `body > #dmmd-${uid}`, `// map {"Jan":10}`).
    They balance today. One unbalanced brace inside a string — `_sep:'}'` — would
    silently truncate the list, and a truncated list makes every "is every lane
    covered?" assertion pass while covering less.

    This walks the same token rules extract_js_function uses and records keys at
    depth 1, so a brace inside a literal cannot end the object early. Quoted keys
    ('molecule3d':{…}) and any indentation are handled; the previous version
    anchored on exactly two spaces.
    """
    import pathlib
    import re

    src = pathlib.Path(index_html).read_text(encoding="utf-8")
    i = src.index("const RICH_LANES={")
    i = src.index("{", i)
    depth, j, n = 0, i, len(src)
    keys, expect_key = [], False
    # The value must be an object: every lane is `name:{…}`, and a scalar entry
    # (`_n: 6/2`) is not a lane. Requiring the brace also means a stray key can
    # never be reported as one.
    # The colon only. Whether an object follows is checked separately, because
    # `\s` does not match a COMMENT: `truth: /* math.js */ {` silently dropped
    # that lane from the result.
    key_re = re.compile(
        r"""\s*(?:(['"])([A-Za-z_$][\w$]*)\1|([A-Za-z_$][\w$]*))\s*:""")

    def _object_follows(pos):
        while pos < len(src):
            nxt = _skip_token(src, pos, len(src))
            if nxt is not None and src[pos] == "/":       # a comment, not a regex
                pos = nxt
                continue
            if src[pos].isspace():
                pos += 1
                continue
            return src[pos] == "{"
        return False
    while j < n:
        # The key matcher runs BEFORE the token skipper: a QUOTED key
        # ('molecule3d':{…}) starts with a quote, and skipping it as a string
        # swallowed the key and dropped that lane from the result.
        if expect_key and not src[j].isspace():
            m = key_re.match(src, j)
            if m and _object_follows(m.end()):
                keys.append(m.group(2) or m.group(3))
                j = m.end()
                expect_key = False
                continue
        nxt = _skip_token(src, j, n)
        if nxt is not None:
            # expect_key survives a skip: lanes are separated by comments, and
            # clearing the flag on a comment lost the key that followed it.
            j = nxt
            continue
        c = src[j]
        if c == "{":
            depth += 1
            expect_key = depth == 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
            expect_key = depth == 1
        elif c == "," and depth == 1:
            expect_key = True
        elif expect_key and not c.isspace():
            expect_key = False
        j += 1
    return keys
