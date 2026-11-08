# Regression tests

Run them:

```bash
venv/bin/python3 -m pytest tests/ -q
```

Tests that need Redis get their **own** server: the suite starts a throwaway
`redis-server` on a free port, bound to the loopback, and stops it afterwards, so
a run never reaches the instance the application uses. If no server with
RediSearch can be started — a bare checkout with nothing installed — those tests
**skip**, and `pytest` prints why in its header. Set `VISUALWEAVER_TEST_REDIS_PORT` to use a server of your own instead — that is
the switch, and CI sets it because it supplies its own Redis service.
`VISUALWEAVER_TEST_REDIS_HOST` only changes where the client connects; on its own
it does not stop the private server from starting.
`VISUALWEAVER_TEST_REDIS_REQUIRED=1` turns "no server could be started" into a
failure rather than a skip.

Two tests reach outside the process:

* frontend tests under `test_lanes_js.py` shell out to `node` and **skip** without
  it. They need no browser, no network and no dev server.
* `test_manifest_urls_are_live` HEADs every CDN asset and only runs with
  `VISUALWEAVER_TEST_NETWORK=1`, because the mutation sweep runs tests once per
  catalogue entry and would otherwise issue thousands of requests to cdnjs per sweep.

## Safety

RediSearch refuses `FT.CREATE` on any database but 0, so tests cannot be isolated
onto their own logical database — only onto their own **server**, which is what
the throwaway instance above is for. The default used to be `6390`, the port the
application listens on; on 2026-08-24 a `FLUSHDB` typed against that instance
removed a 57,805-chunk corpus.

The namespacing still applies, because an explicit override can still point the
suite at a shared server: every key goes under `__vwtest_<pid>__` and only that
prefix is deleted. **No test may call `FLUSHDB` or `FLUSHALL`**, on any database
number — against an override it would destroy the corpus of whoever runs the suite;
`test_no_test_file_flushes_a_redis_database` fails if one appears in `tests/`. Use the
`clean_redis` fixture and build keys with `rc.key("...")` (or from `KEY_PREFIX`),
never by writing the prefix out.

Tests that touch config or sessions get an isolated `DATA_DIR`; `conftest.py`
sets `VISUALWEAVER_DATA_DIR` before `visualweaver.main` is imported, because the
module resolves that path at import time.

## The gate: `tests/mutation_sweep.py`

```bash
venv/bin/python3 tests/mutation_sweep.py --since        # entries touched since the last tag
venv/bin/python3 tests/mutation_sweep.py --changed      # entries touched in the working tree
venv/bin/python3 tests/mutation_sweep.py -k M16         # ids matching a regex
venv/bin/python3 tests/mutation_sweep.py                # whole catalogue, fast mode
venv/bin/python3 tests/mutation_sweep.py --full         # whole suite per entry (+ never-red list)
venv/bin/python3 tests/mutation_sweep.py --check        # catalogue freshness, ~1 s
```

`tests/mutations.json` is a catalogue of known-bad edits — each one reintroduces a
defect that has actually shipped. The sweep copies the repo to a throwaway lab
under `$TMPDIR` (venv symlinked, `.git`/caches skipped, **the working tree is
never written**), applies exactly one edit, runs tests, and reports:

| verdict | meaning |
|---|---|
| `KILLED` | a test named in the entry's `expect` failed — that defect is guarded |
| `KILLED-ELSEWHERE` | `expect` stayed green and only other tests failed — repoint `expect`, or the expect test does not test what it claims |
| `SURVIVED` | `expect` stayed green, and so did the whole suite — **the defect can ship again** |
| `EXPECT-NOT-RUN` | no `expect` test ran: the name matches no test, it skipped (no browser, no MongoDB) or was not collected |
| `RED-NO-KILLER` | pytest exited red without naming a failing test (internal or usage error) |
| `TIMEOUT` | the `expect` tests (or, with `--full` or an escalation, the whole suite) ran past `--timeout` — **not** a kill, and it fails the run like `SURVIVED`: make the test fail rather than hang on that defect |
| `STALE` | the `find` text is gone, or matches a different number of `sites` — the catalogue lies; fix it |

**Only the `expect` test counts.** Any red test used to score `KILLED`, so a flaky
failure anywhere hid a survivor. `expect` is free text; every `test_…` name in it
counts, and a kill by any one of them is a kill. `test_x` covers every
parametrization (`test_x[firefox]`, `test_x[chromium-375]`); `test_x[firefox]`
covers only that one. A collection error in the expect test's own file counts (the
mutated code cannot even be imported there); one in another file does not. Entries
whose `expect` names no test at all (`"NOTHING"`) keep the old any-red-test rule.

Exit code is 1 for any `SURVIVED`, `KILLED-ELSEWHERE`, `EXPECT-NOT-RUN`,
`RED-NO-KILLER`, `TIMEOUT` or `STALE`, so CI can gate on it.

### Modes

* **Fast (default).** The lab runs only the `expect` tests' node ids (built from
  `def test_…` in `tests/test_*.py`). When they stay green it escalates to the whole
  suite, which is what tells `SURVIVED` from `KILLED-ELSEWHERE`; `--no-escalate`
  stops at the expect tests (then `SURVIVED` means "the expect test stayed green").
  The baseline is the union of the selected entries' expect tests, run once on the
  real tree; it must be green.
* **`--full`.** The whole suite in every lab, as the sweep always used to run, with
  the whole suite as the baseline. Only a `--full` run of the whole catalogue (no
  `-k`, `--since` or `--changed`) lists the tests that never went red anywhere — a
  test in that list has not been shown capable of failing.

### Choosing entries

* `-k REGEX` — ids matching the regex.
* `--since [REF]` — entries whose `file` changed since `REF` (commits since it plus
  staged, unstaged and untracked changes); without `REF`, the last tag
  (`git describe --tags --abbrev=0`).
* `--changed` — the same against `HEAD`: working-tree changes only.

Both also select the entries whose `expect` test lives in a changed test file, or in
a test file that imports or names a changed helper under `tests/` (a probe such as
`ux_fixes_probe.py`, or `conftest.py` through `from conftest import …`) — editing a
test can stop it catching its defect. `--list` prints the selection and stops.

### MongoDB and memory

One built-in MongoDB stack (`visualweaver.local_mongo`, set up exactly as
`conftest.py`'s session fixture does) serves every lab of a run. The sweep starts it
on first need — the baseline or a lab whose tests use `mongo_uri` — and passes it
to the labs as `VISUALWEAVER_TEST_MONGO_URI` plus `VISUALWEAVER_TEST_MONGO_SHARED=1`.
Labs never see each other's data: every MongoDB fixture names its database
`vwtest_[<tag>_]<pid>_<ms>`, and with `VISUALWEAVER_TEST_MONGO_SHARED=1` conftest
drops the databases carrying its own pid before and after the run and warns about
any database on the server that carries no pid. The stack (compose project
`vwtest-mongo-<sweep pid>`) is removed when the sweep ends, on Ctrl-C and on
SIGTERM; a removal that fails prints the exact `docker compose … down -v` to run.
A plain `pytest` run, with neither variable set, still starts and removes its own
stack as before. Each lab keeps its own private Redis (a host process of a few MB
that starts in well under a second), so Redis is not shared.

Before starting a stack the sweep checks the container VM: `docker info` MemTotal
minus what `docker stats` says running containers use, minus a 1 GiB reserve, must
hold one stack (planned at 640 MB; one idles at ~520 MB). If it cannot, the entries
that need MongoDB report `EXPECT-NOT-RUN` with the reason. `--no-shared-mongo`
returns to one stack per lab and caps `-j` to the number of stacks that fit, or
refuses to start when not even one does.

### Other flags

* `-j N` — labs in parallel (default 1).
* `--timeout S` — seconds per pytest run (default 1800).
* `--no-baseline` — skip the baseline run (only right after one was green).
* `--json PATH` — write every result, with killers and the other red tests.
* `--catalogue PATH` — sweep another catalogue file.

`--check` also fails an entry whose `expect` names no existing test, and notes one
that names some missing ones. It is fast enough for a pre-commit hook, which is what
keeps a `find` string from drifting out of the tree and turning a row silently
`STALE`.

### How long it takes

Measured on the development Mac (12 cores, 8 GiB container VM), 2026-11-08,
741-entry catalogue, `-j 1`, other test runs sharing the machine:

| what | time |
|---|---|
| `--check` | ~1 s |
| one fast entry, backend expect test | 0.4–6.4 s (3 measured) |
| one fast entry, `node` JS expect test | 0.6–1.0 s (3 measured) |
| one fast entry, MongoDB expect test (shared stack already up) | 6.2–7.8 s (2 measured) |
| one fast entry, browser expect test | 1.7–115 s — the cost of that file's probe: most probes 2–11 s, `test_ux_browser` 35 s, `test_storage_choice_ui_browser` 44 s and `test_ux_fixes_browser` 115 s for Chromium + WebKit (Firefox adds about half again) |
| shared MongoDB start | 6–7 s, once per run |
| one `--full` entry (whole suite) | 1,629 s (1 measured, under load; includes ~9 min of Firefox launch time-outs on that machine) |

Whole catalogue, estimated from those numbers per entry by the file its expect test is
in (297 backend, 158 JS, 73 MongoDB, 210 browser, 3 without an expect test that run the
whole suite):

* **fast (default), `-j 1`: about 5 h**, of which the three whole-suite entries are
  ~1.4 h and `test_ux_fixes_browser`'s 34 entries ~1.6 h; plus one baseline pass over
  the expect tests and one whole-suite run per entry whose expect test stays green.
* `--full`, `-j 1`: 741 × ~27 min (the one measured entry) ≈ 330 h under that load;
  ~60 h on an idle machine. This was the old default.

An `--since` run selects far less: the last four release ranges selected 13, 22, 414
and 711 entries — anything that touches `index.html` selects its 405 entries, a
backend-only change typically 10–30.

## Adding a test when you fix a bug

This file exists so a fixed defect stays fixed. For every non-trivial fix:

1. Write the test **first** and watch it fail against the unfixed code. A test
   that has never failed proves nothing.
2. Name it `test_issue<N>_<what_must_hold>` when it maps to a numbered issue in
   `internal/OPEN-ISSUES.md`, otherwise `test_<what_must_hold>`.
3. State the **old wrong behaviour** in the docstring, not the new correct one.
   Six months from now the useful information is what went wrong.
4. **Assert the behaviour.** Call the function, drive the handler, execute the
   JavaScript. A source-level assertion (`inspect.getsource`, reading
   `index.html`) is admissible only when it is accompanied by an entry in
   `tests/mutations.json` that has been **shown to KILL it**, and the sweep must
   be green before merge. There is no "where you can't" exemption — that clause
   used to live here, and 46 of 63 tests took it.

## How source-level assertions stop working

This is not hypothetical. A full sweep against the 1.5.0 suite scored 66 %:
24 of 71 mutations survived, and 17 of 63 tests never failed under any of them.
Four mechanisms accounted for all of it.

**Comment-carrier.** The assertion is satisfied by the prose that explains the
fix. `"casesensitive" in getsource(_get_rag_index).lower()` — the attribute is
spelled `case_sensitive`, so the only carrier was the comment above it; deleting
the attribute kept the suite green. Same for the gantt lane's `useWidth`.
*Countermeasure:* every source assertion goes through `_code()`, which strips
whole-line `#` and `//` comments, and matches the code form (`"case_sensitive": True`,
`tickInterval:'`) rather than a bare word.

**Existence, not enforcement.** `hasattr(m, "_MAX_UPLOAD_BYTES")` and the name
appearing in the handler both survive `if False:  # _MAX_UPLOAD_BYTES`. Nine tests
asserted a constant exists and nothing asserted it is applied.
*Countermeasure:* post the oversized upload, submit the oversized feedback field,
race eight threads at the lock, record what the embedder actually received.

**Fixed-window slicing.** `switchSession()` is 6,934 characters; the test sliced
400. An early `return` added past the window cleared neither `_pendingRegen` nor
the source scope. `html[html.index("const RICH_LANES={"):]` sliced 92,322
characters — to end of file — so a lane key pasted anywhere later satisfied it.
*Countermeasure:* `_js_fn()` slices to the next `function` marker; `_rich_lanes_src()`
stops at the object's own closing brace; the lane registry is parsed by `node`.

**Assertions that cannot discriminate.** `test_issue5_overlap_is_preserved`
measured overlap by word-set intersection over a corpus whose every sentence read
"sentence*i* about databases and storage systems" — the filler alone made every
boundary intersect (measured 1.000 against a 0.50 threshold **with the overlap
carry-back deleted**). `test_issue13` sliced from `append_log(` to the end of the
function and asserted `"except" in tail`, which the handler's outer `except` also
satisfies. `test_issue8` asserted that two sha256 digests computed inside the test
differ.
*Countermeasure:* measure shared sentence identity (0.505 vs 0.000), make
`append_log` raise and check the response, assert the production hash expression.

## Coverage and its limits

The suite covers backend logic, Redis integration, executed frontend JavaScript
and source-level invariants that a catalogued mutation has been shown to break.
It still does **not** drive a real browser, so layout, paint and CSS remain
verified by hand — a lane that parses, runs and sanitises here can still lay out
wrong. `KILLED` is also not the same as "behavioural": a few kills come from
renaming an identifier a test greps for, which would not catch a semantic break
that keeps the name.

The catalogue is not exhaustive either — it targets roughly one defect per
guarded behaviour, and whole surfaces (the math-sentinel pipeline, chart zoom
ranges, molecule card clipping, geometry dark mode) have no entry at all. Every
new fix must arrive with its own entry, or the score will look healthy while the
same class of defect ships again.
