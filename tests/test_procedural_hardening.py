"""TDD (review hardening): fixes from the adversarial review of the go-live diff.

Covers the valid findings: a fast kill-switch for a bad live heuristic (burn),
injection-text hygiene now that habits ride in every prompt (truncate + strip
newlines), the harness's blindness to a changed-match-at-same-rank semantic
regression (diff_reports), and a parametrizable precision-probe window.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import procedural_lib as pl
import procedural_test as pt
import memory_engine as me

PY = {"trigger": "running python3 on Windows hits a UnicodeEncodeError on output",
      "action": "set PYTHONIOENCODING=utf-8 before the command", "insight": ""}


def _reset():
    pl.ensure_schema()
    with me.db() as c:
        c.execute("DELETE FROM heuristics")
        c.commit()


# ── F3.4: burn — instant archive of a bad heuristic while live ───────────────
def test_burn_archives_heuristic_immediately():
    _reset()
    hid = pl.upsert_heuristic(PY, origin="test", polarity="failure_derived")["id"]
    pl.burn(hid)
    with me.db() as c:
        status = c.execute("SELECT status FROM heuristics WHERE id=?", (hid,)).fetchone()[0]
    assert status == "archived"
    assert pl.retrieve("UnicodeEncodeError running python", k=5) == []


# ── F3.5: format_for_injection hygiene — bounded, single line per heuristic ──
def test_format_for_injection_strips_newlines_and_truncates():
    h = {"trigger": "a situation\nwith a newline", "action": "B" * 400 + "\nmore"}
    out = pl.format_for_injection([h], header="learned habits")
    lines = out.split("\n")
    # exactly one header line + one line for the single heuristic — internal
    # newlines in trigger/action must NOT spill into extra lines.
    assert len(lines) == 2, f"expected header + 1 bullet, got {len(lines)} lines"
    assert len(lines[1]) <= 260, "action/trigger must be truncated for prompt budget"


# ── F1.6: diff_reports must flag a changed match even at a stable rank ────────
def test_diff_reports_flags_changed_match_at_same_rank():
    baseline = {"results": [{"id": "a", "passed": True, "rank": 1, "matched": "old action"}]}
    current = {"results": [{"id": "a", "passed": True, "rank": 1, "matched": "new action"}]}
    d = pt.diff_reports(current, baseline)
    assert [x["id"] for x in d["match_changed"]] == ["a"]


# ── F2.4: run_precision k is parametrizable (defaults to the injection window) ─
def test_run_precision_accepts_k_param():
    _reset()
    pl.upsert_heuristic(PY, origin="test", polarity="failure_derived")
    rep = pt.run_precision(["what is the capital of France"], threshold=0.62, k=5)
    assert rep["k"] == 5
    assert rep["leaks"] == []
