"""TDD: worker-side consolidate coalesce guard (recon H5).

The producer 30-min cap (session_end) is bypassable (clock skew / restore / other
enqueue paths) and the worker had NO guard, so a backlog of distinct consolidate
jobs each ran the full heavy pass — the 5/29 runaway that fed the multi-week
freeze (207 bulk-cancelled jobs). The worker must skip the heavy pass if one
completed within a coalesce window, with a `force` bypass for manual runs.
"""
import os
import time
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import consolidate_worker as cw


def test_consolidate_recently_done_true_when_fresh(tmp_path, monkeypatch):
    m = tmp_path / ".lw"; m.write_text("x")
    monkeypatch.setattr(cw, "_LAST_CONSOLIDATE_MARKER", m)
    assert cw._consolidate_recently_done(time.time()) is True


def test_consolidate_recently_done_false_when_stale_missing_or_future(tmp_path, monkeypatch):
    m = tmp_path / ".lw"
    monkeypatch.setattr(cw, "_LAST_CONSOLIDATE_MARKER", m)
    assert cw._consolidate_recently_done(time.time()) is False          # missing
    m.write_text("x")
    old = time.time() - cw.CONSOLIDATE_COALESCE_SEC - 100
    os.utime(m, (old, old))
    assert cw._consolidate_recently_done(time.time()) is False          # stale
    future = time.time() + 10_000
    os.utime(m, (future, future))
    assert cw._consolidate_recently_done(time.time()) is False          # clock skew → allow


def test_mark_consolidate_done_writes_recent_marker(tmp_path, monkeypatch):
    m = tmp_path / ".lw"
    monkeypatch.setattr(cw, "_LAST_CONSOLIDATE_MARKER", m)
    cw._mark_consolidate_done()
    assert m.exists() and cw._consolidate_recently_done(time.time()) is True


def test_process_consolidate_skips_heavy_work_when_recent(tmp_path, monkeypatch):
    m = tmp_path / ".lw"; m.write_text("x")  # fresh → recently done
    monkeypatch.setattr(cw, "_LAST_CONSOLIDATE_MARKER", m)
    monkeypatch.setattr(cw, "_log", lambda rec: None)
    def _boom():
        raise AssertionError("heavy pass ran during coalesce window")
    monkeypatch.setattr(cw, "_near_duplicate_pass", _boom)
    out = cw.process_consolidate({"session_id": "s1"})
    assert out.get("skipped") == "coalesced"


def test_process_consolidate_force_bypasses_coalesce(tmp_path, monkeypatch):
    m = tmp_path / ".lw"; m.write_text("x")  # fresh, but force must override
    monkeypatch.setattr(cw, "_LAST_CONSOLIDATE_MARKER", m)
    monkeypatch.setattr(cw, "_log", lambda rec: None)
    def _raise():
        raise RuntimeError("entered heavy work")
    monkeypatch.setattr(cw, "_near_duplicate_pass", _raise)
    # force=True must bypass the guard and reach the (stubbed) first heavy pass
    with pytest.raises(RuntimeError, match="entered heavy work"):
        cw.process_consolidate({"force": True})
