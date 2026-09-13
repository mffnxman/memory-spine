"""TDD (eval harness): procedural_test.py — retrieval hit-rate / MRR regression.

Sibling of continuity_test.py, but for the L2 heuristic pool. Curated
(task_context -> expected heuristic) cases are scored by whether a retrieved
heuristic (within top-k) carries an expected action/trigger marker. These tests
pin the harness's own logic (case scoring, MRR, baseline diff) on a controlled
seeded pool, so the harness can be trusted before it gates the flag flip.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import procedural_lib as pl
import procedural_test as pt
import memory_engine as me

PY = {"trigger": "running python3 on Windows hits a UnicodeEncodeError on output",
      "action": "set PYTHONIOENCODING=utf-8 before the command", "insight": ""}
GIT = {"trigger": "about to git push --force to a shared branch",
       "action": "stop and confirm with the human first", "insight": ""}
BOOT = {"trigger": "booting a fresh session with the user",
        "action": "start at baseline, do not perform enthusiasm", "insight": ""}


def _reset_and_seed():
    pl.ensure_schema()
    with me.db() as c:
        c.execute("DELETE FROM heuristics")
        c.commit()
    for h in (PY, GIT, BOOT):
        pl.upsert_heuristic(h, origin="test", polarity="failure_derived")


def test_run_case_passes_when_marker_in_topk():
    _reset_and_seed()
    case = {"id": "py", "task_context": "UnicodeEncodeError running my python script",
            "expect_contains": ["PYTHONIOENCODING"], "weight": "core"}
    r = pt._run_case(case, k=3)
    assert r["passed"] is True
    assert r["rank"] == 1


def test_run_case_misses_when_marker_absent():
    _reset_and_seed()
    case = {"id": "absent", "task_context": "how do I bake sourdough bread",
            "expect_contains": ["preheat the oven to 450"], "weight": "core"}
    r = pt._run_case(case, k=3)
    assert r["passed"] is False
    assert r["rank"] is None


def test_mrr_computation():
    results = [{"rank": 1}, {"rank": 2}, {"rank": None}]
    assert abs(pt._mrr(results) - (1.0 + 0.5 + 0.0) / 3) < 1e-9


def test_run_aggregates_report():
    _reset_and_seed()
    cases = [
        {"id": "py", "task_context": "UnicodeEncodeError running my python script",
         "expect_contains": ["PYTHONIOENCODING"], "weight": "core"},
        {"id": "git", "task_context": "I'm about to force push to the shared main branch",
         "expect_contains": ["confirm with the human"], "weight": "core"},
    ]
    report = pt.run(cases=cases, k=3)
    assert report["total"] == 2
    assert report["passed"] == 2
    assert report["pass_rate"] == 1.0
    assert report["mrr"] > 0
    assert report["k"] == 3


def test_precision_no_leak_above_threshold():
    # A distractor prompt with no applicable habit must not clear MATCH_MIN
    # against the seeded pool — guards the false-injection bug found at pool scale.
    _reset_and_seed()
    rep = pt.run_precision(["what is the capital of France"], threshold=0.62)
    assert rep["distractors"] == 1
    assert rep["leaks"] == []
    assert rep["false_inject_rate"] == 0.0


def test_precision_flags_a_leak_below_threshold():
    # With an absurdly low threshold even an unrelated prompt "leaks" — proves the
    # metric actually measures threshold-clearing, not always-zero.
    _reset_and_seed()
    rep = pt.run_precision(["set PYTHONIOENCODING before running python"], threshold=0.0)
    assert len(rep["leaks"]) == 1
    assert rep["false_inject_rate"] == 1.0


def test_report_is_json_serializable():
    # The real embedding path yields numpy float32 cosines; --baseline save and
    # --json both json.dumps the report, so every field must be plain-serializable.
    _reset_and_seed()
    import json
    cases = [{"id": "py", "task_context": "UnicodeEncodeError running my python script",
              "expect_contains": ["PYTHONIOENCODING"], "weight": "core"}]
    report = pt.run(cases=cases, k=3)
    json.dumps(report)  # must not raise TypeError on float32


def test_diff_reports_detects_regression():
    baseline = {"passed": 2, "total": 2, "mrr": 1.0,
                "results": [{"id": "py", "passed": True, "rank": 1},
                            {"id": "git", "passed": True, "rank": 1}]}
    current = {"passed": 1, "total": 2, "mrr": 0.5,
               "results": [{"id": "py", "passed": True, "rank": 1},
                           {"id": "git", "passed": False, "rank": None}]}
    diff = pt.diff_reports(current, baseline)
    assert [r["id"] for r in diff["regressions"]] == ["git"]
    assert diff["new_passes"] == []
