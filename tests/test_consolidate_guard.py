"""session_end consolidate frequency guard.

session_end.py enqueued a 'consolidate' job on EVERY session end. Frequent short
sessions (e.g. the repeated boots while debugging) over-produced consolidate jobs
— the backlog that the 2026-06-03 freeze fix had to drain. The guard caps
enqueues to at most one per CONSOLIDATE_MIN_INTERVAL_SEC.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import session_end as se


def test_consolidate_due_first_time():
    # No prior marker → due.
    assert se._consolidate_due(None, now=1000.0) is True


def test_consolidate_not_due_within_interval():
    assert se._consolidate_due(1000.0, now=1000.0 + 60, interval=1800) is False


def test_consolidate_due_after_interval():
    assert se._consolidate_due(1000.0, now=1000.0 + 1801, interval=1800) is True


def test_consolidate_due_handles_future_marker():
    # Clock skew / restore: a future marker mtime must not wedge it permanently.
    assert se._consolidate_due(2000.0, now=1000.0, interval=1800) is True
