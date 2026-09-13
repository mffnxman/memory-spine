"""Hot-path (PostToolUse Edit/Write hook) drain must never run LLM-backed jobs.

Root cause of the recurring "Tool result missing"/freeze (diagnosed 2026-06-03):
the Write|Edit PostToolUse hook called outbox_worker.drain(max_jobs=5)
SYNCHRONOUSLY, in-process, with no timeout. With a backlog of `consolidate`
jobs (each → procedural_lib.run_designer → subscription `claude -p`, 180s, the
provider's own docstring says "NOT for hot-path hooks"), the hook blew past its
60s budget. The edit landed but the harness killed the hook before the
tool_result returned → "Tool result missing" / whole-UI freeze.

Fix: drain(hot_path=True) leaves the LLM-backed kinds pending for the cron
drainer and processes only cheap, bounded work, under a wall-clock cap. The
cron/manual path (hot_path=False) is unchanged and still does the heavy work
off the tool's critical path.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import outbox_worker as ow


def _setup(monkeypatch, jobs_events):
    """jobs_events: list of (event_id, kind). Returns (processed_kinds, updates)."""
    jobs = [{"event_id": eid, "attempts": 0, "status": "pending"} for eid, _ in jobs_events]
    events = {eid: {"event_id": eid, "kind": kind, "payload": {}} for eid, kind in jobs_events}
    processed, updates = [], []

    def fake_read_jobs(status_filter=None, limit=None):
        if status_filter == "pending":
            return jobs[:limit] if limit else jobs
        return []  # 'processing' (reaper) → nothing

    monkeypatch.setattr(ow.event_bus, "read_jobs", fake_read_jobs)
    monkeypatch.setattr(ow.event_bus, "lookup_event", lambda eid: events.get(eid))
    monkeypatch.setattr(ow.event_bus, "update_job", lambda eid, status, **k: updates.append((eid, status)))
    monkeypatch.setattr(ow, "process_event", lambda ev: processed.append(ev["kind"]))
    return processed, updates


HEAVY = ["consolidate", "importance_score", "reflection"]
CHEAP = ["memory_write", "reindex"]


def test_hot_path_skips_llm_backed_kinds(monkeypatch):
    processed, updates = _setup(
        monkeypatch,
        [("c1", "consolidate"), ("i1", "importance_score"), ("r1", "reflection"),
         ("m1", "memory_write"), ("x1", "reindex")],
    )
    ow.drain(max_jobs=10, hot_path=True)

    for k in HEAVY:
        assert k not in processed, f"hot path ran a {k} job synchronously (freeze risk)"
    for k in CHEAP:
        assert k in processed, f"hot path skipped cheap job {k}"
    # Heavy jobs must be LEFT PENDING — never marked processing/done — so cron gets them.
    for eid in ("c1", "i1", "r1"):
        assert all(u[0] != eid for u in updates), f"{eid} was touched; must stay pending for cron"


def test_cron_path_processes_all_kinds(monkeypatch):
    # hot_path=False (cron/manual) keeps the original behavior: heavy work runs,
    # just off the tool's critical path.
    processed, _ = _setup(monkeypatch, [("c1", "consolidate"), ("m1", "memory_write")])
    ow.drain(max_jobs=10, hot_path=False)
    assert "consolidate" in processed and "memory_write" in processed


def test_hot_path_respects_wall_clock_budget(monkeypatch):
    # Even cheap kinds shouldn't run unbounded on the hook. If a job overruns the
    # budget, the drain stops and leaves the rest pending rather than risking the
    # 60s hook kill.
    processed, _ = _setup(monkeypatch, [("a", "reindex"), ("b", "reindex"), ("c", "reindex")])

    def slow_process(ev):
        processed.append(ev["kind"])
        time.sleep(ow.HOT_PATH_BUDGET_SEC + 0.05)  # first job alone exceeds budget

    monkeypatch.setattr(ow, "process_event", slow_process)
    ow.drain(max_jobs=10, hot_path=True)
    assert len(processed) == 1, "wall-clock cap should stop after the first over-budget job"


def test_default_drain_is_not_hot_path(monkeypatch):
    # Backwards-compat: existing callers that don't pass hot_path get full behavior.
    processed, _ = _setup(monkeypatch, [("c1", "consolidate")])
    ow.drain(max_jobs=5)  # no hot_path kwarg
    assert "consolidate" in processed
