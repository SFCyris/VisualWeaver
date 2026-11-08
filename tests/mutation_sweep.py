#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mutation sweep — the gate that decides whether tests/ is worth anything.

    venv/bin/python3 tests/mutation_sweep.py                 # whole catalogue, fast mode
    venv/bin/python3 tests/mutation_sweep.py -k lane         # ids matching /lane/
    venv/bin/python3 tests/mutation_sweep.py --since         # entries touched since the last tag
    venv/bin/python3 tests/mutation_sweep.py --changed       # entries touched in the working tree
    venv/bin/python3 tests/mutation_sweep.py -j 4            # 4 labs in parallel
    venv/bin/python3 tests/mutation_sweep.py --full          # whole suite per lab (+ never-red list)
    venv/bin/python3 tests/mutation_sweep.py --check         # catalogue freshness only

For each entry it copies the repo to a throwaway lab, applies exactly one edit and runs
tests. A mutation counts as KILLED only when a test its `expect` field names goes red —
any other failure is a different verdict, so a flaky test elsewhere can no longer hide a
survivor.

    KILLED            an `expect` test failed                 -> that defect is guarded
    KILLED-ELSEWHERE  `expect` stayed green, other tests went red -> repoint `expect`
    SURVIVED          `expect` (and, escalated, the whole suite) stayed green
    EXPECT-NOT-RUN    no `expect` test ran: unknown name, skipped, not collected
    RED-NO-KILLER     pytest exited red with no test named (internal/usage error)
    TIMEOUT           the `expect` tests (or the whole suite) ran out of time: not a kill
    STALE             the `find` text is gone, or matches a different number of sites

Fast mode (the default) runs only the `expect` tests' node ids in the lab; when they stay
green it escalates to the whole suite (--no-escalate turns that off) to tell SURVIVED
from KILLED-ELSEWHERE. --full runs the whole suite for every entry, as the sweep always
used to, and on an unfiltered run lists every test that never went red.

Exit code 1 for any SURVIVED, KILLED-ELSEWHERE, EXPECT-NOT-RUN, RED-NO-KILLER, TIMEOUT or STALE.

MongoDB: one built-in MongoDB stack (visualweaver.local_mongo, as tests/conftest.py starts
it) is shared by every lab of a run, through VISUALWEAVER_TEST_MONGO_URI plus
VISUALWEAVER_TEST_MONGO_SHARED=1. It is started only when an entry needs it and removed
on exit, Ctrl-C and SIGTERM. Labs never see each other's data: every test database name
carries the pytest pid, and conftest drops its own pid's databases before and after a
lab. Before starting a stack the sweep checks the container VM's memory (`docker info`
MemTotal minus what running containers use); --no-shared-mongo gives each lab its own
stack again and caps -j to what the VM can hold.

Safety: the lab is a copy under $TMPDIR; venv/ is symlinked, .git/ and caches skipped,
and the real working tree is never written to. The suite's Redis namespacing
(__vwtest_<pid>__) still applies, so no FLUSHDB and no touching db 0's real keys.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE = Path(__file__).with_name("mutations.json")
PYTHON = ROOT / "venv" / "bin" / "python3"
SKIP = shutil.ignore_patterns("venv", ".git", ".pytest_cache", "__pycache__",
                              "screenshots", "node_modules", ".venv")

MONGO_URI_ENV = "VISUALWEAVER_TEST_MONGO_URI"
MONGO_SHARED_ENV = "VISUALWEAVER_TEST_MONGO_SHARED"
# One built-in MongoDB stack (mongod + mongot) idles at ~520 MB (local_mongo.IDLE_MEMORY);
# plan with headroom, and keep a reserve for the VM's own kernel and dockerd.
STACK_BYTES = 640 * 2**20
VM_RESERVE_BYTES = 1024 * 2**20

FAILING = {"SURVIVED", "KILLED-ELSEWHERE", "EXPECT-NOT-RUN", "RED-NO-KILLER", "TIMEOUT", "STALE"}
_TAG = {"KILLED": "kill", "KILLED-ELSEWHERE": "ELSEWHERE", "SURVIVED": "SURVIVED",
        "EXPECT-NOT-RUN": "NOT-RUN", "RED-NO-KILLER": "RED-NOKILL", "TIMEOUT": "timeout",
        "STALE": "STALE"}
_NAME = re.compile(r"test_[A-Za-z0-9_]+(?:\[[^\]]+\])?")
# A collection error in one file must not abort the session: the other files still say
# whether the expect test noticed.
PYTEST_FLAGS = ["-q", "-p", "no:cacheprovider", "--tb=no", "-rfE",
                "--continue-on-collection-errors", "-o", "junit_family=xunit1"]


# ── catalogue helpers ────────────────────────────────────────────────────────

def _apply(text: str, mut: dict) -> tuple[str, int]:
    """Apply every (find, replace) pair in `mut`. Returns (new_text, sites)."""
    pairs = [(mut["find"], mut["replace"])]
    pairs += [(e["find"], e["replace"]) for e in mut.get("also", [])]
    sites = 0
    for find, repl in pairs:
        n = text.count(find)
        if n == 0:
            return text, 0
        sites += n
        text = text.replace(find, repl)
    return text, sites


def expect_names(mut: dict) -> list[str]:
    """The test names an entry's `expect` field vouches for, in order, deduplicated.
    `test_x` means every parametrization of test_x; `test_x[firefox]` means that one."""
    seen: list[str] = []
    for n in _NAME.findall(mut.get("expect", "") or ""):
        if n not in seen:
            seen.append(n)
    return seen


def name_matches(expect: str, test_name: str) -> bool:
    """Does the collected test `test_name` (e.g. "test_x[firefox]") satisfy `expect`?"""
    if "[" in expect:
        return test_name == expect
    return test_name.split("[", 1)[0] == expect


def index_tests(root: Path) -> dict[str, list[str]]:
    """test function name -> the test files (repo-relative) that define it, by parsing
    tests/test_*.py. Methods of Test* classes are indexed as "Class::name"-qualified
    node paths, so a node id can be built for them too."""
    out: dict[str, list[str]] = {}
    for f in sorted((root / "tests").glob("test_*.py")):
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        rel = f.relative_to(root).as_posix()

        def walk(node, prefix):
            for n in getattr(node, "body", []):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test"):
                    out.setdefault(n.name, []).append(f"{rel}{prefix}")
                elif isinstance(n, ast.ClassDef) and n.name.startswith("Test"):
                    walk(n, f"{prefix}::{n.name}")
        walk(tree, "")
    return out


def expect_nodes(mut: dict, index: dict[str, list[str]]) -> tuple[list[str], list[str]]:
    """(pytest node ids for the entry's expect tests, expect names that match no test)."""
    nodes, missing = [], []
    for e in expect_names(mut):
        where = index.get(e.split("[", 1)[0])
        if not where:
            missing.append(e)
            continue
        nodes += [f"{w}::{e}" for w in where]
    return nodes, missing


def _node_file(node: str) -> str:
    return node.split("::", 1)[0]


# ── results ──────────────────────────────────────────────────────────────────

@dataclass
class Outcome:
    module: str           # dotted, e.g. "tests.test_x" (a collection error: the module itself)
    name: str             # e.g. "test_y[firefox]"; "" for a collection error
    outcome: str          # "passed" | "failed" | "skipped"


def parse_junit(path: Path) -> list[Outcome]:
    """Every test case pytest's --junitxml recorded. Failures and errors (setup, teardown,
    collection) are both "failed"; a collection error has an empty `name`."""
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return []
    merged: dict[tuple[str, str], str] = {}
    rank = {"passed": 0, "skipped": 1, "failed": 2}
    for tc in root.iter("testcase"):
        cls, name = tc.get("classname", ""), tc.get("name", "")
        tags = {c.tag for c in tc}
        outcome = ("failed" if tags & {"failure", "error"}
                   else "skipped" if "skipped" in tags else "passed")
        if not cls:                       # collection error: <testcase classname="" name="tests.test_x">
            key = (name, "")
        else:
            # Methods of a class carry "tests.test_x.TestC" — keep the module part only;
            # the name alone is what `expect` is matched against.
            key = (cls, name)
        if rank[outcome] >= rank.get(merged.get(key, "passed"), 0):
            merged[key] = outcome
    return [Outcome(m, n, o) for (m, n), o in merged.items()]


def _module_of(rel_file: str) -> str:
    return rel_file[:-3].replace("/", ".") if rel_file.endswith(".py") else rel_file


def compute_verdict(expect: list[str], results: list[Outcome], rc: int | None,
                    expect_files: set[str] | frozenset = frozenset()) -> dict:
    """The verdict for one lab run. `expect` are the entry's expect names; `expect_files`
    the repo-relative test files defining them (a collection error there means the expect
    test could not run against the mutated code — that file went red, so it counts)."""
    red = [r for r in results if r.outcome == "failed"]
    label = lambda r: f"{r.module}::{r.name}" if r.name else f"<collection error: {r.module}>"
    if not expect:
        # No expect named: the legacy rule — any red test is the kill.
        if red:
            return {"verdict": "KILLED", "killers": sorted(map(label, red)), "elsewhere": [],
                    "reason": "no expect named; any red test counts"}
        return {"verdict": "RED-NO-KILLER" if rc not in (0, 5) else "SURVIVED",
                "killers": [], "elsewhere": [], "reason": "no expect named"}
    exp_modules = {_module_of(f) for f in expect_files}
    matched = [r for r in results if r.name and any(name_matches(e, r.name) for e in expect)]
    exp_red = [r for r in matched if r.outcome == "failed"]
    exp_red += [r for r in red if not r.name and r.module in exp_modules]
    elsewhere = sorted(label(r) for r in red if r not in exp_red)
    if exp_red:
        return {"verdict": "KILLED", "killers": sorted(map(label, exp_red)), "elsewhere": elsewhere,
                "reason": ""}
    if not matched:
        return {"verdict": "EXPECT-NOT-RUN", "killers": [], "elsewhere": elsewhere,
                "reason": "no expect test was collected"}
    if all(r.outcome == "skipped" for r in matched):
        return {"verdict": "EXPECT-NOT-RUN", "killers": [], "elsewhere": elsewhere,
                "reason": "every expect test skipped"}
    if elsewhere:
        return {"verdict": "KILLED-ELSEWHERE", "killers": [], "elsewhere": elsewhere,
                "reason": "expect stayed green; only other tests failed"}
    if rc not in (0, 5):
        return {"verdict": "RED-NO-KILLER", "killers": [], "elsewhere": [],
                "reason": f"pytest exited {rc} with no failing test recorded"}
    return {"verdict": "SURVIVED", "killers": [], "elsewhere": [], "reason": ""}


# ── selection by change ──────────────────────────────────────────────────────

def _git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"git {' '.join(args)} failed: {(r.stderr or r.stdout).strip()}")
    return r.stdout


def last_tag(root: Path = ROOT) -> str:
    return _git(root, "describe", "--tags", "--abbrev=0").strip()


def changed_files(root: Path, ref: str | None) -> set[str]:
    """Repo-relative files that differ from `ref` (commits since it + staged + unstaged
    changes), or, with ref None, from HEAD (working tree only); untracked files count."""
    diff = _git(root, "diff", "--name-only", "--no-renames", ref or "HEAD")
    other = _git(root, "ls-files", "--others", "--exclude-standard")
    return {l.strip() for l in (diff + other).splitlines() if l.strip()}


def affected_test_files(root: Path, changed: set[str]) -> set[str]:
    """Test files whose outcome a change can move: changed test_*.py files, plus every
    test file that names a changed helper under tests/ (a probe such as
    ux_fixes_probe.py, or conftest.py via `from conftest import …`)."""
    tests = {p.relative_to(root).as_posix() for p in (root / "tests").glob("test_*.py")}
    hit = {f for f in changed if f in tests}
    helpers = [Path(f).stem for f in changed
               if f.startswith("tests/") and f.endswith(".py") and f not in tests]
    if helpers:
        # An import of the helper, or its file name in code (a probe started via
        # Path(__file__).with_name("x_probe.py")); comment lines do not count.
        pats = [re.compile(rf"^\s*(?:from|import)\s+{re.escape(h)}\b|\b{re.escape(h)}\.py\b")
                for h in helpers]
        for t in tests - hit:
            try:
                src = (root / t).read_text(encoding="utf-8")
            except OSError:
                continue
            code = [l for l in src.splitlines() if not l.lstrip().startswith("#")]
            if any(p.search(l) for p in pats for l in code):
                hit.add(t)
    return hit


def select_changed(muts: list[dict], changed: set[str], index: dict[str, list[str]],
                   root: Path = ROOT) -> list[dict]:
    """Entries whose mutated `file` changed, or whose expect test lives in an affected
    test file (a test edit can stop it catching the defect)."""
    tests = affected_test_files(root, changed)
    out = []
    for m in muts:
        nodes, _ = expect_nodes(m, index)
        if m["file"] in changed or any(_node_file(n) in tests for n in nodes):
            out.append(m)
    return out


# ── container VM memory ──────────────────────────────────────────────────────

_UNITS = {"b": 1, "kb": 1000, "kib": 1024, "mb": 1000**2, "mib": 1024**2,
          "gb": 1000**3, "gib": 1024**3, "tb": 1000**4, "tib": 1024**4}


def parse_mem(s: str) -> int:
    """"371.7MiB" -> bytes (docker stats' MemUsage units)."""
    m = re.fullmatch(r"\s*([\d.]+)\s*([A-Za-z]*)\s*", s or "")
    if not m:
        raise ValueError(f"not a memory size: {s!r}")
    return int(float(m.group(1)) * _UNITS[(m.group(2) or "b").lower()])


def vm_memory() -> tuple[int, int] | None:
    """(MemTotal, bytes used by running containers) of the docker VM, or None."""
    try:
        info = subprocess.run(["docker", "info", "--format", "{{.MemTotal}}"],
                              capture_output=True, text=True, timeout=60)
        if info.returncode or not info.stdout.strip().isdigit():
            return None
        total = int(info.stdout.strip())
        st = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}"],
                            capture_output=True, text=True, timeout=120)
        used = 0
        for line in st.stdout.splitlines():
            try:
                used += parse_mem(line.split("/")[0])
            except (ValueError, KeyError):
                pass
        return total, used
    except (OSError, subprocess.TimeoutExpired):
        return None


def plan_stacks(stacks: int, mem: tuple[int, int] | None) -> tuple[int, str]:
    """How many of `stacks` new MongoDB stacks the VM can hold, and what to say about it.
    0 means do not start any."""
    if mem is None:
        return stacks, "container VM memory unknown (docker info failed) — not checked"
    total, used = mem
    free = total - used - VM_RESERVE_BYTES
    fit = max(0, free // STACK_BYTES)
    gib = lambda b: f"{b / 2**30:.1f} GiB"
    msg = (f"container VM: {gib(total)} total, {gib(used)} used by running containers, "
           f"{gib(max(free, 0))} free after a {gib(VM_RESERVE_BYTES)} reserve -> room for "
           f"{fit} MongoDB stack(s) of {STACK_BYTES // 2**20} MB")
    return min(stacks, fit), msg


# ── process control ──────────────────────────────────────────────────────────

_LIVE: set[subprocess.Popen] = set()
_LIVE_LOCK = threading.Lock()
_STOP = threading.Event()


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGINT first, so pytest runs its unconfigure hooks (private Redis, temp dirs), then
    SIGKILL whatever of the group is left — browsers and redis-server included."""
    for sig, wait in ((signal.SIGINT, 15), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=wait)
            if sig == signal.SIGINT:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)   # stragglers in the group
                except (ProcessLookupError, PermissionError):
                    pass
            return
        except subprocess.TimeoutExpired:
            continue


def run_proc(cmd: list[str], cwd: Path, env: dict, timeout: float) -> tuple[int | None, str, bool]:
    """(returncode, stdout+stderr, timed_out). Own process group, registered so an
    interrupt can stop it."""
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, start_new_session=True)
    with _LIVE_LOCK:
        _LIVE.add(proc)
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out, False
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            out, _ = proc.communicate(timeout=10)
        except (subprocess.TimeoutExpired, ValueError):
            out = ""
        return None, out or "", True
    finally:
        with _LIVE_LOCK:
            _LIVE.discard(proc)


def stop_everything() -> None:
    _STOP.set()
    with _LIVE_LOCK:
        procs = list(_LIVE)
    for p in procs:
        _kill_group(p)


# ── the shared MongoDB ───────────────────────────────────────────────────────

class SharedMongo:
    """One built-in MongoDB for the whole sweep, started on first need, removed by close().
    A URI the caller already set (VISUALWEAVER_TEST_MONGO_URI) is used as is."""

    def __init__(self, enabled: bool, root: Path = ROOT):
        self.enabled, self.root = enabled, root
        self.preset = os.environ.get(MONGO_URI_ENV) or None
        self.uri: str | None = self.preset
        self.error: str | None = None
        self.project: str | None = None
        self._base: Path | None = None
        self._cmd: list[str] | None = None
        self._lock = threading.Lock()
        self.boot_secs = 0.0

    def env(self) -> dict:
        """Extra env for a lab that needs MongoDB ({} when this run has no shared one)."""
        if not self.enabled and not self.preset:
            return {}
        uri = self.get()
        if not uri:
            return {}
        return {MONGO_URI_ENV: uri, MONGO_SHARED_ENV: "1"}

    def get(self) -> str | None:
        with self._lock:
            if self.uri or self.error or _STOP.is_set():
                return self.uri
            sys.path.insert(0, str(self.root))
            try:
                from visualweaver import local_mongo
            finally:
                sys.path.pop(0)
            cmd, why = local_mongo.runtime()
            if not cmd:
                self.error = f"no container runtime: {why}"
                return None
            n, msg = plan_stacks(1, vm_memory())
            print(f"shared MongoDB: {msg}", flush=True)
            if n < 1:
                self.error = "the container VM has no room for a MongoDB stack — " + msg
                return None
            import socket
            with socket.socket() as s:
                s.bind(("127.0.0.1", 0))
                port = s.getsockname()[1]
            self._base = Path(tempfile.mkdtemp(prefix="rr-mutsweep-mongo-"))
            self.project = f"vwtest-mongo-{os.getpid()}"
            self._cmd = cmd
            t0 = time.time()
            st = local_mongo.init(port=port, base=self._base, project=self.project)
            try:
                local_mongo.up(base=self._base, timeout=300)
            except local_mongo.LocalMongoError as e:
                self.error = f"could not start the shared MongoDB: {str(e)[:300]}"
                return None
            self.boot_secs = round(time.time() - t0, 1)
            self.uri = local_mongo.admin_uri(st)
            print(f"shared MongoDB up in {self.boot_secs}s (compose project {self.project}, "
                  f"port {port})", flush=True)
            return self.uri

    def close(self) -> bool:
        """`compose down -v`, retried; True when nothing of ours is left running."""
        with self._lock:
            if not self.project or not self._cmd or not self._base:
                return True
            from_file = self._base / "mongodb" / "compose.yml"
            ok, last = False, ""
            for attempt in range(1, 4):
                try:
                    r = subprocess.run(self._cmd + ["-p", self.project, "-f", str(from_file),
                                                    "down", "-v"],
                                       capture_output=True, text=True, timeout=180)
                    if r.returncode == 0:
                        ok = True
                        break
                    last = (r.stderr or r.stdout or "").strip()[-300:]
                except (subprocess.TimeoutExpired, OSError) as e:
                    last = f"{type(e).__name__}: {e}"
                time.sleep(3 * attempt)
            if ok:
                print(f"shared MongoDB {self.project} removed", flush=True)
                shutil.rmtree(self._base, ignore_errors=True)
            else:
                print(f"WARNING: shared MongoDB {self.project} was NOT removed ({last}). "
                      f"Remove it by hand: {' '.join(self._cmd)} -p {self.project} "
                      f"-f {from_file} down -v", flush=True)
            self.project = None
            return ok


# ── one lab ──────────────────────────────────────────────────────────────────

@dataclass
class Settings:
    root: Path = ROOT
    python: Path = PYTHON
    full: bool = False
    escalate: bool = True
    timeout: int = 1800
    index: dict = field(default_factory=dict)
    mongo_files: set = field(default_factory=set)
    mongo: SharedMongo | None = None


def _pytest(settings: Settings, work: Path, targets: list[str], junit: Path,
            needs_mongo: bool) -> tuple[int | None, str, bool, list[Outcome]]:
    if _STOP.is_set():                       # interrupted: start nothing new
        return None, "sweep interrupted", False, []
    env = dict(os.environ)
    if needs_mongo and settings.mongo is not None:
        env.update(settings.mongo.env())
    cmd = [str(settings.python), "-m", "pytest", *targets, *PYTEST_FLAGS, f"--junitxml={junit}"]
    rc, out, timed_out = run_proc(cmd, work, env, settings.timeout)
    results = parse_junit(junit) if junit.exists() else []
    return rc, out, timed_out, results


def run_one(mut: dict, settings: Settings) -> dict:
    nodes, missing = expect_nodes(mut, settings.index)
    names = expect_names(mut)
    base = {**mut, "sites": 0, "killers": [], "elsewhere": [], "secs": 0.0,
            "returncode": None, "skipped": 0, "scope": "", "reason": "",
            "expect_missing": missing}
    if _STOP.is_set():
        return {**base, "verdict": "EXPECT-NOT-RUN", "reason": "sweep interrupted"}
    lab = Path(tempfile.mkdtemp(prefix="rr-mutsweep-"))
    t0 = time.time()
    try:
        work = lab / "repo"
        shutil.copytree(settings.root, work, symlinks=True, ignore=SKIP)
        if (settings.root / "venv").exists():
            os.symlink(settings.root / "venv", work / "venv")
        target = work / mut["file"]
        new, sites = _apply(target.read_text(encoding="utf-8"), mut)
        if sites == 0:
            return {**base, "verdict": "STALE"}
        # A declared `sites` count that no longer matches the tree means the `find`
        # has drifted onto more (or fewer) occurrences than the catalogue vouches
        # for — the exact shape that let M31 "kill" by rewriting a prose comment
        # rather than the directive. Refuse to score it; fix the catalogue.
        if mut.get("sites") is not None and sites != mut["sites"]:
            return {**base, "verdict": "STALE", "sites": sites,
                    "stale_reason": f"declared sites={mut['sites']} but found {sites}"}
        target.write_text(new, encoding="utf-8")
        base["sites"] = sites
        expect_files = {_node_file(n) for n in nodes}

        def full_run(tag: str) -> dict:
            junit = lab / f"junit-{tag}.xml"
            rc, out, timed_out, res = _pytest(settings, work, ["tests/"], junit, True)
            skipped = sum(1 for r in res if r.outcome == "skipped")
            if timed_out:
                return {"verdict": "TIMEOUT", "killers": [], "elsewhere": [],
                        "reason": f"whole suite exceeded {settings.timeout}s", "returncode": None,
                        "skipped": skipped}
            v = compute_verdict(names, res, rc, expect_files)
            return {**v, "returncode": rc, "skipped": skipped,
                    "tail": "" if res else out[-600:]}

        if names and not nodes:
            return {**base, "verdict": "EXPECT-NOT-RUN", "secs": round(time.time() - t0, 1),
                    "reason": f"expect names no test in tests/: {', '.join(missing)}"}
        if settings.full or not names:
            v = full_run("full")
            return {**base, **v, "scope": "full", "secs": round(time.time() - t0, 1)}

        # Fast: only the expect tests.
        junit = lab / "junit-expect.xml"
        needs_mongo = bool(expect_files & settings.mongo_files)
        rc, out, timed_out, res = _pytest(settings, work, nodes, junit, needs_mongo)
        if timed_out:
            # A hang is not a failure: the defect may just have made the test slow, and
            # nothing says the test would have noticed it. Its own verdict, failing the gate.
            return {**base, "verdict": "TIMEOUT", "killers": [],
                    "scope": "expect", "secs": round(time.time() - t0, 1),
                    "reason": f"expect tests exceeded {settings.timeout}s"}
        v = compute_verdict(names, res, rc, expect_files)
        v["skipped"] = sum(1 for r in res if r.outcome == "skipped")
        v["returncode"] = rc
        if not res:
            v["tail"] = out[-600:]
        if v["verdict"] == "EXPECT-NOT-RUN" and needs_mongo and settings.mongo is not None \
                and settings.mongo.error:
            v["reason"] += f" ({settings.mongo.error})"
        if v["verdict"] != "SURVIVED" or not settings.escalate:
            return {**base, **v, "scope": "expect", "secs": round(time.time() - t0, 1)}
        # The expect tests stayed green: run everything to tell SURVIVED from
        # KILLED-ELSEWHERE (and catch an expect test that only fails in suite order).
        f = full_run("escalated")
        if f["verdict"] == "KILLED":
            f["reason"] = "expect test failed only inside the whole suite (order-dependent)"
        return {**base, **f, "scope": "expect+full", "secs": round(time.time() - t0, 1)}
    finally:
        shutil.rmtree(lab, ignore_errors=True)


# ── --check ──────────────────────────────────────────────────────────────────

def check_only(muts: list[dict], index: dict | None = None, root: Path = ROOT) -> int:
    """Fast freshness pass: every `find` must still be present in the tree, and every
    entry that names `expect` tests must name at least one that exists.

    Also flags entries whose `replace` still contains `find`. That is legitimate
    for a mutation that WRAPS code (M14, M63 …) but is the exact shape of a
    self-defeating edit for one that means to delete it — `tickInterval` ->
    `xtickIntervalx` still satisfies a test grepping for `tickInterval`, which
    produced four false survivors while this catalogue was being written. The
    verdict for a flagged entry is only trustworthy once it has been KILLED.
    """
    index = index_tests(root) if index is None else index
    bad = 0
    for mut in muts:
        text = (root / mut["file"]).read_text(encoding="utf-8")
        pairs = [mut] + list(mut.get("also", []))
        missing = [p["find"] for p in pairs if p["find"] not in text]
        if missing:
            bad += 1
            print(f"STALE {mut['id']}: {len(missing)} pattern(s) no longer in "
                  f"{mut['file']}")
            continue
        for p in pairs:
            got = text.count(p["find"])
            want = p.get("sites")
            if want is not None and got != want:
                bad += 1
                print(f"STALE {mut['id']}: {p['find']!r} matches {got} site(s), "
                      f"catalogue says {want}")
            elif want is None and got > 1:
                print(f"note  {mut['id']}: {p['find']!r} applies at {got} sites — "
                      f"pin it with \"sites\": {got} so a kill via one of them is caught")
        if any(p["find"] and p["find"] in p["replace"] for p in pairs):
            print(f"note  {mut['id']}: replacement retains the pattern (wrapping "
                  f"mutation — confirm it is KILLED, not merely green)")
        nodes, gone = expect_nodes(mut, index)
        if gone and not nodes:
            bad += 1
            print(f"STALE {mut['id']}: expect names no test in tests/ ({', '.join(gone)})")
        elif gone:
            print(f"note  {mut['id']}: expect name(s) not found in tests/: {', '.join(gone)}")
    print(f"{len(muts)} entries checked, {bad} stale")
    return 1 if bad else 0


# ── main ─────────────────────────────────────────────────────────────────────

def _print_row(done: int, total: int, res: dict, base_skipped: int | None) -> None:
    tag = _TAG[res["verdict"]]
    who = res["killers"] or res.get("elsewhere", [])
    shown = ", ".join(k.split("::")[-1] for k in who[:4]) + (" …" if len(who) > 4 else "")
    skipnote = ("" if base_skipped is None or res.get("skipped", 0) <= base_skipped
                else f"  [!{res['skipped']} skipped]")
    scope = f" [{res['scope']}]" if res.get("scope") else ""
    print(f"[{done}/{total}] {tag:10s} {res['id']:44s} {res['secs']}s{scope}  {shown}{skipnote}",
          flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="run the mutation sweep")
    ap.add_argument("-k", metavar="REGEX", help="only ids matching this regex")
    ap.add_argument("--since", nargs="?", const="", metavar="REF",
                    help="only entries whose file (or expect test) changed since REF — "
                         "committed and working-tree changes; REF defaults to the last tag")
    ap.add_argument("--changed", action="store_true",
                    help="only entries whose file (or expect test) has working-tree changes")
    ap.add_argument("-j", type=int, default=1, help="parallel labs (default 1)")
    ap.add_argument("--full", action="store_true",
                    help="run the whole suite in every lab (slow; lists never-red tests)")
    ap.add_argument("--no-escalate", dest="escalate", action="store_false",
                    help="fast mode: do not run the whole suite when the expect tests stay green")
    ap.add_argument("--no-shared-mongo", dest="shared_mongo", action="store_false",
                    help="each lab starts its own MongoDB stack (caps -j to the VM's memory)")
    ap.add_argument("--no-baseline", dest="baseline", action="store_false",
                    help="skip the green-baseline run (only when you just ran it)")
    ap.add_argument("--list", action="store_true", help="print the selected ids and stop")
    ap.add_argument("--timeout", type=int, default=1800, help="per-run seconds")
    ap.add_argument("--check", action="store_true", help="freshness pass only")
    ap.add_argument("--json", metavar="PATH", help="write full results here")
    ap.add_argument("--catalogue", default=str(CATALOGUE),
                    help="mutation catalogue to sweep (default tests/mutations.json)")
    args = ap.parse_args(argv)

    muts = json.loads(Path(args.catalogue).read_text(encoding="utf-8"))
    total_catalogue = len(muts)
    index = index_tests(ROOT)
    if args.k:
        rx = re.compile(args.k)
        muts = [m for m in muts if rx.search(m["id"])]
    if args.since is not None or args.changed:
        ref = None if args.changed and args.since is None else (args.since or last_tag(ROOT))
        changed = changed_files(ROOT, ref)
        muts = select_changed(muts, changed, index)
        print(f"{len(changed)} file(s) changed since {ref or 'HEAD (working tree)'} -> "
              f"{len(muts)} of {total_catalogue} entries selected")
    if not muts:
        print("no mutations selected")
        return 1
    if args.check:
        return check_only(muts, index)
    if args.list:
        for m in muts:
            print(m["id"])
        return 0

    mongo_files = {p.relative_to(ROOT).as_posix() for p in (ROOT / "tests").glob("test_*.py")
                   if "mongo_uri" in p.read_text(encoding="utf-8", errors="replace")}
    mongo = SharedMongo(enabled=args.shared_mongo)
    settings = Settings(full=args.full, escalate=args.escalate, timeout=args.timeout,
                        index=index, mongo_files=mongo_files,
                        mongo=mongo if (args.shared_mongo or mongo.preset) else None)
    jobs = max(1, args.j)
    if not args.shared_mongo and not mongo.preset:
        # Old shape: every lab whose tests need MongoDB boots its own stack.
        fit, msg = plan_stacks(jobs, vm_memory())
        print(msg)
        if fit < 1:
            print("ABORT: the container VM cannot hold even one MongoDB stack; free memory "
                  "or use the shared stack (drop --no-shared-mongo).")
            return 1
        if fit < jobs:
            print(f"-j {jobs} capped to {fit}: one MongoDB stack per lab would not fit")
            jobs = fit

    def _term(signum, frame):
        raise KeyboardInterrupt
    old_term = signal.signal(signal.SIGTERM, _term)
    results: list[dict] = []
    pool = None
    try:
        base_skipped: int | None = None
        if args.baseline:
            if args.full:
                targets, need = ["tests/"], True
                what = "the whole suite"
            else:
                targets = sorted({n for m in muts for n in expect_nodes(m, index)[0]})
                need = any(_node_file(n) in mongo_files for n in targets)
                what = f"{len(targets)} expect test node(s)"
            print(f"baseline: {what} must be green before a sweep means anything", flush=True)
            if targets:
                env = dict(os.environ)
                if need and settings.mongo is not None:
                    env.update(settings.mongo.env())
                with tempfile.TemporaryDirectory(prefix="rr-mutsweep-base-") as d:
                    junit = Path(d) / "junit.xml"
                    t0 = time.time()
                    rc, out, timed_out = run_proc(
                        [str(PYTHON), "-m", "pytest", *targets, *PYTEST_FLAGS,
                         f"--junitxml={junit}"],
                        ROOT, env, args.timeout)
                    res = parse_junit(junit)
                if timed_out or rc not in (0, 5):
                    print(out[-2500:])
                    red = [f"{r.module}::{r.name}" for r in res if r.outcome == "failed"]
                    print("ABORT: the baseline is red" + (f" ({', '.join(red[:10])})" if red else "")
                          + "; fix that first.")
                    return 1
                base_skipped = sum(1 for r in res if r.outcome == "skipped")
                print(f"baseline green in {time.time() - t0:.0f}s ({base_skipped} skipped)")
        mode = "full suite per lab" if args.full else (
            "expect tests per lab" + (", whole suite when they stay green" if args.escalate else ""))
        print(f"sweeping {len(muts)} mutations, {jobs} lab(s), {mode}\n", flush=True)

        pool = ThreadPoolExecutor(max_workers=jobs)
        futures = [pool.submit(run_one, m, settings) for m in muts]
        for done, fut in enumerate(futures, 1):
            res = fut.result()
            results.append(res)
            _print_row(done, len(muts), res, base_skipped if args.full else None)
    except KeyboardInterrupt:
        print("\ninterrupted — stopping labs and removing the shared MongoDB", flush=True)
        stop_everything()
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
            pool = None
        return 130
    finally:
        if pool is not None:
            pool.shutdown(wait=True)
        if settings.mongo is not None:
            settings.mongo.close()
        signal.signal(signal.SIGTERM, old_term)

    by = {v: [r for r in results if r["verdict"] == v] for v in _TAG}
    scored = max(1, len(results) - len(by["STALE"]))
    killed = len(by["KILLED"])
    secs = [r["secs"] for r in results if r["verdict"] != "STALE"]
    print("\n" + "=" * 78)
    print(f"{len(results)} mutations | " + " | ".join(f"{v} {len(by[v])}" for v in _TAG if by[v])
          + f"  ->  score {killed / scored:.0%}")
    if secs:
        print(f"lab time: {sum(secs):.0f}s total, {sum(secs) / len(secs):.1f}s mean per entry")
    print("=" * 78)
    for r in by["SURVIVED"]:
        print(f"\nSURVIVED {r['id']}" + (f" [{r['scope']}]" if r.get("scope") else ""))
        print(f"  defect that shipped : {r['defect']}")
        print(f"  test that claims it : {r['expect']}")
    for r in by["KILLED-ELSEWHERE"]:
        print(f"\nKILLED-ELSEWHERE {r['id']}")
        print(f"  expect names        : {r['expect']}  (stayed green)")
        print(f"  but the reds were   : {', '.join(r['elsewhere']) or '(none)'}")
    for r in by["EXPECT-NOT-RUN"]:
        print(f"\nEXPECT-NOT-RUN {r['id']}  — {r.get('reason')}")
        if r.get("tail"):
            print("  " + r["tail"].strip().replace("\n", "\n  ")[-400:])
    for r in by["RED-NO-KILLER"]:
        print(f"\nRED-NO-KILLER {r['id']}  — suite exited {r.get('returncode')} with no "
              f"named failure (collection/fixture/internal error); NOT a survivor")
    for r in by["TIMEOUT"]:
        print(f"\nTIMEOUT  {r['id']}  — {r.get('reason')}; NOT a kill: make the expect test "
              f"fail (not hang) on this defect, or raise --timeout")
    for r in by["STALE"]:
        why = r.get("stale_reason", f"`find` text no longer in {r['file']}")
        print(f"\nSTALE    {r['id']}  — {why}")
    for r in results:
        if r.get("expect_missing") and r["verdict"] != "EXPECT-NOT-RUN":
            print(f"\nnote     {r['id']}: expect name(s) not found in tests/: "
                  f"{', '.join(r['expect_missing'])}")
        if args.full and base_skipped is not None and r.get("skipped", 0) > base_skipped:
            print(f"\nWARNING  {r['id']}: {r['skipped']} tests skipped (baseline "
                  f"{base_skipped}) — its expect-test may not have run at all")

    # Every test that never went red across the whole sweep. A test in this list
    # has not been shown capable of failing; it is not coverage until it is.
    if args.full and not args.k and args.since is None and not args.changed:
        noticed = {k.split("::")[-1] for r in results for k in r["killers"] + r.get("elsewhere", [])}
        listed = subprocess.run([str(PYTHON), "-m", "pytest", "tests/", "-q",
                                 "--collect-only", "-p", "no:cacheprovider"],
                                cwd=ROOT, capture_output=True, text=True)
        allt = sorted({l.split("::")[-1].strip() for l in listed.stdout.splitlines()
                       if "::" in l})
        never = [t for t in allt if t not in noticed]
        print(f"\n{len(never)}/{len(allt)} tests never failed anywhere in this sweep:")
        for t in never:
            print("   ", t)

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1), encoding="utf-8")
    return 1 if any(r["verdict"] in FAILING for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
