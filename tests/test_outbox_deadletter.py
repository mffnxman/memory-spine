"""Outbox retry + dead-letter semantics.

2026-06-03 recon finding: the MAX_ATTEMPTS=3 / dead-letter machinery was
unreachable. drain() only reads status='pending', but a failed job was set to
the terminal status 'failed' and never re-queued — so it never retried and never
reached the attempts>=3 dead-letter state. counts['dead'] could never increment.

Fixed semantics:
  - transient failure (process_event raised) → 'pending' (retried on a later
    drain) until attempts >= MAX_ATTEMPTS, then terminal 'dead'.
  - permanent failure (event-not-found) → 'dead' immediately (retrying can't help).
  - stats() counts an explicit 'dead' status exactly once (no double count) and
    surfaces 'cancelled'.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import outbox_worker as ow
import event_bus


def _mutable_queue(monkeypatch, job, kind="reindex", event=True):
    """One mutable job; drain reads it while pending and mutates it via update_job."""
    state = dict(job)
    seen = []

    def fake_read_jobs(status_filter=None, limit=None):
        if status_filter == "pending" and state["status"] == "pending":
            return [dict(state)]
        return []  # 'processing' (reaper) → nothing

    def fake_update(eid, status, error=None, attempts_delta=0):
        state["status"] = status
        state["attempts"] = state.get("attempts", 0) + attempts_delta
        seen.append((status, state["attempts"]))

    monkeypatch.setattr(ow.event_bus, "read_jobs", fake_read_jobs)
    monkeypatch.setattr(ow.event_bus, "lookup_event",
                        lambda eid: {"event_id": eid, "kind": kind, "payload": {}} if event else None)
    monkeypatch.setattr(ow.event_bus, "update_job", fake_update)
    return state, seen


def test_transient_failure_retries_then_dead_letters(monkeypatch):
    state, seen = _mutable_queue(monkeypatch, {"event_id": "j1", "attempts": 0, "status": "pending"})
    monkeypatch.setattr(ow, "process_event",
                        lambda ev: (_ for _ in ()).throw(RuntimeError("boom")))
    end_states = []
    for _ in range(5):
        ow.drain(max_jobs=5)
        end_states.append((state["status"], state["attempts"]))

    # Retried as 'pending' with rising attempts, NOT terminal at attempts=1...
    assert ("pending", 1) in end_states
    assert ("pending", 2) in end_states
    # ...then dead-lettered exactly at MAX_ATTEMPTS.
    assert ("dead", event_bus.MAX_ATTEMPTS) in end_states
    assert state["status"] == "dead"


def test_dead_letter_increments_dead_count(monkeypatch):
    state, seen = _mutable_queue(monkeypatch, {"event_id": "j2", "attempts": 2, "status": "pending"})
    monkeypatch.setattr(ow, "process_event",
                        lambda ev: (_ for _ in ()).throw(RuntimeError("boom")))
    counts = ow.drain(max_jobs=5)  # attempts 2 -> 3 -> dead this drain
    assert counts["dead"] == 1
    assert state["status"] == "dead"


def test_event_not_found_dead_letters_immediately(monkeypatch):
    state, seen = _mutable_queue(monkeypatch, {"event_id": "missing", "attempts": 0, "status": "pending"}, event=False)
    counts = ow.drain(max_jobs=5)
    # Permanent failure: straight to 'dead', not retried as 'pending'.
    assert state["status"] == "dead"
    assert counts["dead"] == 1
    assert all(s != "pending" for s, _ in seen)


def test_stats_counts_dead_once_and_surfaces_cancelled(monkeypatch):
    jobs = [
        {"event_id": "d", "status": "dead", "attempts": 3},      # must count once, not twice
        {"event_id": "c", "status": "cancelled", "attempts": 0},
        {"event_id": "ok", "status": "done", "attempts": 0},
        {"event_id": "p", "status": "pending", "attempts": 0},
    ]
    monkeypatch.setattr(event_bus, "read_jobs", lambda status_filter=None, limit=None: jobs)
    s = event_bus.stats()
    assert s["dead"] == 1, "dead status must not be double-counted with the attempts heuristic"
    assert s["cancelled"] == 1
    assert s["done"] == 1 and s["pending"] == 1
    assert s["total"] == 4
