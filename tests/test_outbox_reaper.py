"""Reaper must not re-queue a still-running long job.

A consolidate job legitimately sits in 'processing' for 20-35 min (the LLM
designer pass). The reaper promotes 'processing' jobs older than
STUCK_PROCESSING_TIMEOUT_SEC back to 'pending'. If that timeout is below a
real consolidate's runtime, the still-running job gets re-dispatched, spawning
duplicate concurrent consolidates (the thundering-herd that burned tokens).
The timeout must exceed the longest legitimate job while still recovering
genuinely dead ones.
"""
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import outbox_worker as ow


def _patch_jobs(monkeypatch, job):
    promoted = []
    monkeypatch.setattr(ow.event_bus, "read_jobs",
                        lambda status_filter=None, limit=None: [job] if status_filter == "processing" else [])
    monkeypatch.setattr(ow.event_bus, "update_job",
                        lambda eid, status, **k: promoted.append((eid, status)))
    return promoted


def test_reaper_leaves_a_long_running_consolidate_alone(monkeypatch):
    # 10 min in 'processing' — well within a real consolidate's runtime. Must
    # NOT be reaped (reaping → duplicate concurrent run).
    ten_min_ago = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    job = {"event_id": "consolidate-1", "status": "processing", "updated_at": ten_min_ago}
    promoted = _patch_jobs(monkeypatch, job)
    n = ow.reap_stuck()
    assert n == 0, "a 10-min-old running consolidate was reaped → duplicate run"
    assert promoted == []


def test_reaper_still_requeues_a_truly_dead_job(monkeypatch):
    # Crash recovery must still work: a job stuck far beyond any legitimate
    # runtime is promoted back to pending.
    long_ago = (datetime.now(timezone.utc) - timedelta(minutes=120)).isoformat()
    job = {"event_id": "dead-1", "status": "processing", "updated_at": long_ago}
    promoted = _patch_jobs(monkeypatch, job)
    n = ow.reap_stuck()
    assert n == 1 and promoted == [("dead-1", "pending")]
