# SPDX-License-Identifier: AGPL-3.0-or-later
"""The mutation sweep's own logic: verdicts, expect matching, change selection, memory
pre-flight, interrupt cleanup — against fake suites in throwaway directories, never the
real catalogue and never a container runtime."""
import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

import mutation_sweep as ms
from mutation_sweep import Outcome


def _o(name, outcome, module="tests.test_a"):
    return Outcome(module, name, outcome)


# ── verdicts ────────────────────────────────────────────────────────────────────

def test_a_failure_outside_expect_is_not_a_kill():
    """Old behaviour: any red test scored KILLED, so one flaky failure elsewhere hid a
    mutation the expect test did not notice."""
    v = ms.compute_verdict(["test_guard"], [_o("test_guard", "passed"), _o("test_flaky", "failed")], 1)
    assert v["verdict"] == "KILLED-ELSEWHERE"
    assert v["killers"] == [] and v["elsewhere"] == ["tests.test_a::test_flaky"]
    assert "KILLED-ELSEWHERE" in ms.FAILING            # it fails the gate, like a survivor


def test_the_expect_test_failing_is_a_kill_even_with_other_failures():
    v = ms.compute_verdict(["test_guard"], [_o("test_guard", "failed"), _o("test_flaky", "failed")], 1)
    assert v["verdict"] == "KILLED" and v["killers"] == ["tests.test_a::test_guard"]
    assert v["elsewhere"] == ["tests.test_a::test_flaky"]


def test_a_green_expect_with_a_green_suite_survives():
    v = ms.compute_verdict(["test_guard"], [_o("test_guard", "passed"), _o("test_b", "passed")], 0)
    assert v["verdict"] == "SURVIVED"


def test_a_skipped_expect_is_not_a_survivor():
    """A skipped expect test proved nothing either way — it must not read as SURVIVED
    (nor as a kill)."""
    v = ms.compute_verdict(["test_guard"], [_o("test_guard[firefox]", "skipped")], 0)
    assert v["verdict"] == "EXPECT-NOT-RUN" and "skipped" in v["reason"]


def test_an_uncollected_expect_is_not_run():
    v = ms.compute_verdict(["test_guard"], [_o("test_b", "failed")], 1)
    assert v["verdict"] == "EXPECT-NOT-RUN" and v["elsewhere"] == ["tests.test_a::test_b"]


def test_a_red_exit_with_a_green_expect_is_red_no_killer():
    v = ms.compute_verdict(["test_guard"], [_o("test_guard", "passed")], 3)
    assert v["verdict"] == "RED-NO-KILLER"


def test_a_collection_error_counts_only_in_the_expect_tests_own_file():
    err = Outcome("tests.test_a", "", "failed")
    v = ms.compute_verdict(["test_guard"], [err], 2, {"tests/test_a.py"})
    assert v["verdict"] == "KILLED" and v["killers"] == ["<collection error: tests.test_a>"]
    v = ms.compute_verdict(["test_guard"], [err, _o("test_guard", "passed", "tests.test_b")], 2,
                           {"tests/test_b.py"})
    assert v["verdict"] == "KILLED-ELSEWHERE"


def test_an_entry_without_expect_keeps_the_any_red_rule():
    assert ms.compute_verdict([], [_o("test_x", "failed")], 1)["verdict"] == "KILLED"
    assert ms.compute_verdict([], [_o("test_x", "passed")], 0)["verdict"] == "SURVIVED"


# ── expect names ────────────────────────────────────────────────────────────────

def test_a_parametrized_expect_matches_only_its_own_id():
    assert ms.name_matches("test_x", "test_x[firefox]")
    assert ms.name_matches("test_x[firefox]", "test_x[firefox]")
    assert not ms.name_matches("test_x[firefox]", "test_x[chromium]")
    v = ms.compute_verdict(["test_x[firefox]"], [_o("test_x[chromium]", "failed"),
                                                 _o("test_x[firefox]", "passed")], 1)
    assert v["verdict"] == "KILLED-ELSEWHERE"


def test_expect_name_is_not_a_prefix_match():
    assert not ms.name_matches("test_x", "test_x_more")
    assert not ms.name_matches("test_x", "test_x_more[a]")


def test_expect_names_are_read_from_free_text():
    assert ms.expect_names({"expect": "test_a / test_b[webkit] (asserts the wrong invariant) test_a"}) \
        == ["test_a", "test_b[webkit]"]
    assert ms.expect_names({"expect": "NOTHING — the dot lane has no test"}) == []


def test_expect_nodes_point_at_the_defining_file(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_one.py").write_text(
        "def test_a(): pass\nclass TestK:\n    def test_m(self): pass\n")
    idx = ms.index_tests(tmp_path)
    nodes, missing = ms.expect_nodes({"expect": "test_a[p1] test_m test_gone"}, idx)
    assert nodes == ["tests/test_one.py::test_a[p1]", "tests/test_one.py::TestK::test_m"]
    assert missing == ["test_gone"]


def test_check_flags_an_entry_whose_expect_names_no_test(tmp_path, capsys):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_one.py").write_text("def test_a(): pass\n")
    (tmp_path / "m.py").write_text("x = 1\n")
    muts = [{"id": "A", "file": "m.py", "find": "x = 1", "replace": "x = 2", "expect": "test_a"},
            {"id": "B", "file": "m.py", "find": "x = 1", "replace": "x = 3", "expect": "test_gone"}]
    assert ms.check_only(muts, root=tmp_path) == 1
    out = capsys.readouterr().out
    assert "STALE B: expect names no test" in out and "STALE A" not in out


# ── junit parsing against a real pytest run ─────────────────────────────────────

def _fake_repo(root: Path, body: str) -> Path:
    (root / "tests").mkdir(parents=True)
    (root / "pytest.ini").write_text("[pytest]\n")
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "mod.py").write_text("def add(a, b):\n    return a + b\n\n"
                                         "def neg(a):\n    return -a\n")
    (root / "tests" / "test_mod.py").write_text(textwrap.dedent(body))
    return root


_BODY = """
    import pytest
    from pkg.mod import add, neg

    @pytest.mark.parametrize("n", [1, 2])
    def test_add(n):
        assert add(n, 1) == n + 1

    def test_neg():
        assert neg(3) == -3

    def test_skipped():
        pytest.skip("no engine")
"""


def test_junit_results_carry_params_skips_and_collection_errors(tmp_path):
    repo = _fake_repo(tmp_path / "r", _BODY)
    (repo / "tests" / "test_broken.py").write_text("import nonexistent_module_xyz\n")
    junit = tmp_path / "j.xml"
    rc, out, timed_out = ms.run_proc([sys.executable, "-m", "pytest", "tests/", *ms.PYTEST_FLAGS,
                                      f"--junitxml={junit}"], repo, dict(os.environ), 120)
    assert not timed_out and rc == 1, out
    got = {(r.module, r.name): r.outcome for r in ms.parse_junit(junit)}
    assert got[("tests.test_mod", "test_add[1]")] == "passed"
    assert got[("tests.test_mod", "test_skipped")] == "skipped"
    assert got[("tests.test_broken", "")] == "failed"


# ── one lab, end to end, on a fake repo ─────────────────────────────────────────

def _settings(repo, **kw):
    return ms.Settings(root=repo, python=Path(sys.executable), index=ms.index_tests(repo),
                       timeout=120, **kw)


def _mut(**kw):
    return {"id": "MX", "file": "pkg/mod.py", "defect": "d", **kw}


def test_fast_mode_runs_only_the_expect_test_and_kills(tmp_path):
    repo = _fake_repo(tmp_path / "r", _BODY)
    r = ms.run_one(_mut(find="return a + b", replace="return a - b", expect="test_add"), _settings(repo))
    assert r["verdict"] == "KILLED" and r["scope"] == "expect"
    assert r["killers"] == ["tests.test_mod::test_add[1]", "tests.test_mod::test_add[2]"]
    assert (repo / "pkg" / "mod.py").read_text().count("a + b") == 1   # tree untouched


def test_fast_mode_escalates_to_tell_survived_from_killed_elsewhere(tmp_path):
    """The expect test stays green: only the whole suite can say whether anything else
    noticed. Without escalation both cases read SURVIVED."""
    repo = _fake_repo(tmp_path / "r", _BODY)
    r = ms.run_one(_mut(find="return -a", replace="return a", expect="test_add"), _settings(repo))
    assert r["verdict"] == "KILLED-ELSEWHERE" and r["scope"] == "expect+full"
    assert r["elsewhere"] == ["tests.test_mod::test_neg"]
    r = ms.run_one(_mut(find="return -a", replace="return -a  # same", expect="test_add"),
                   _settings(repo))
    assert r["verdict"] == "SURVIVED" and r["scope"] == "expect+full"
    r = ms.run_one(_mut(find="return -a", replace="return a", expect="test_add"),
                   _settings(repo, escalate=False))
    assert r["verdict"] == "SURVIVED" and r["scope"] == "expect"


def test_full_mode_and_unknown_expect(tmp_path):
    repo = _fake_repo(tmp_path / "r", _BODY)
    r = ms.run_one(_mut(find="return -a", replace="return a", expect="test_neg"), _settings(repo, full=True))
    assert r["verdict"] == "KILLED" and r["scope"] == "full"
    r = ms.run_one(_mut(find="return -a", replace="return a", expect="test_nobody"), _settings(repo))
    assert r["verdict"] == "EXPECT-NOT-RUN" and "test_nobody" in r["reason"]
    r = ms.run_one(_mut(find="not in the file", replace="x", expect="test_neg"), _settings(repo))
    assert r["verdict"] == "STALE"


# ── selection by change ─────────────────────────────────────────────────────────

def _sel_repo(tmp_path):
    t = tmp_path / "tests"
    t.mkdir()
    (t / "test_a.py").write_text("def test_alpha(): pass\n")
    (t / "test_b.py").write_text("import b_probe\ndef test_beta(): pass\n")
    (t / "b_probe.py").write_text("")
    return tmp_path


_MUTS = [{"id": "M1", "file": "app/one.py", "expect": "test_alpha"},
         {"id": "M2", "file": "app/two.py", "expect": "test_beta"},
         {"id": "M3", "file": "app/three.py", "expect": "test_alpha"}]


def test_a_changed_source_file_selects_its_entries(tmp_path):
    root = _sel_repo(tmp_path)
    got = ms.select_changed(_MUTS, {"app/one.py"}, ms.index_tests(root), root)
    assert [m["id"] for m in got] == ["M1"]


def test_a_changed_expect_test_selects_its_entries(tmp_path):
    """Editing a test can stop it catching its defect, so the entries it guards re-run."""
    root = _sel_repo(tmp_path)
    got = ms.select_changed(_MUTS, {"tests/test_a.py"}, ms.index_tests(root), root)
    assert [m["id"] for m in got] == ["M1", "M3"]
    got = ms.select_changed(_MUTS, {"tests/b_probe.py"}, ms.index_tests(root), root)
    assert [m["id"] for m in got] == ["M2"]


def test_changed_files_asks_git_for_ref_diff_and_untracked(monkeypatch, tmp_path):
    calls = []

    def fake_git(root, *args):
        calls.append(args)
        return {"diff": "a.py\nb.py\n", "ls-files": "new.py\n"}[args[0]]
    monkeypatch.setattr(ms, "_git", fake_git)
    assert ms.changed_files(tmp_path, "v1.0.0") == {"a.py", "b.py", "new.py"}
    assert calls[0] == ("diff", "--name-only", "--no-renames", "v1.0.0")
    assert calls[1] == ("ls-files", "--others", "--exclude-standard")
    calls.clear()
    ms.changed_files(tmp_path, None)
    assert calls[0][-1] == "HEAD"


# ── memory pre-flight ───────────────────────────────────────────────────────────

def test_memory_sizes_parse():
    assert ms.parse_mem("371.7MiB") == int(371.7 * 2**20)
    assert ms.parse_mem("7.738GiB") == int(7.738 * 2**30)
    assert ms.parse_mem("512kB") == 512000 and ms.parse_mem("0B") == 0


def test_the_memory_preflight_counts_what_running_containers_use():
    """Old behaviour: no pre-flight at all; four labs each started a ~0.5 GB stack in a
    4 GiB VM that already ran the user's MongoDB, and the VM ran out of memory."""
    gib = 2**30
    n, msg = ms.plan_stacks(20, (8 * gib, int(0.7 * gib)))
    assert n == (8 * gib - int(0.7 * gib) - ms.VM_RESERVE_BYTES) // ms.STACK_BYTES and "0.7 GiB used" in msg
    n, _ = ms.plan_stacks(4, (8 * gib, int(6.8 * gib)))
    assert n == 0
    assert ms.plan_stacks(3, None)[0] == 3                 # unknown: not checked


# ── interrupts ──────────────────────────────────────────────────────────────────

class _FakeMongo:
    closed = 0

    def __init__(self, enabled, root=None):
        self.enabled, self.preset, self.error = enabled, None, None

    def env(self):
        return {}

    def close(self):
        type(self).closed += 1
        return True


def test_an_interrupted_sweep_still_removes_the_shared_mongodb(tmp_path, monkeypatch):
    cat = tmp_path / "cat.json"
    cat.write_text(json.dumps([{"id": "MZ", "file": "x", "find": "a", "replace": "b",
                                "defect": "d", "expect": "test_x"}]))

    def boom(mut, settings):
        raise KeyboardInterrupt
    monkeypatch.setattr(ms, "SharedMongo", _FakeMongo)
    monkeypatch.setattr(ms, "run_one", boom)
    monkeypatch.setattr(ms, "stop_everything", lambda: None)
    _FakeMongo.closed = 0
    assert ms.main(["--catalogue", str(cat), "--no-baseline"]) == 130
    assert _FakeMongo.closed == 1


# ── conftest: per-lab database ownership on the shared server ───────────────────

def test_a_lab_owns_only_databases_with_its_own_pid():
    import conftest
    pid = os.getpid()
    names = [f"vwtest_{pid}_123", f"vwtest_sw_{pid}_9_d", f"vwtest_cache_{pid}_1",
             f"vwtest_{pid}0_1", f"vwtest_1{pid}_1", "vwtest_99999999_1", "visualweaver", "admin"]
    assert conftest._own_test_databases(names) == names[:3]
