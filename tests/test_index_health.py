"""TDD: surface index + outbox health at boot.

A new memory whose memory_write event dead-letters is permanently un-indexed
with zero signal; likewise a stale/missing embedding is invisible. Boot should
say so (it already runs deterministically at SessionStart).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import memory_engine as me


def test_index_health_reports_missing():
    mems = me.list_memories()
    assert mems
    target = mems[0]
    base = me.index_health()
    assert base["memories"] == len(mems)
    with me.db() as c:
        c.execute("DELETE FROM embeddings WHERE filename=?", (target.filename,))
    try:
        h = me.index_health()
        assert h["missing"] == base["missing"] + 1
    finally:
        me.reindex_embeddings([target], force=True)


def test_boot_health_lines_flag_problems(monkeypatch):
    import boot_ritual
    import event_bus
    monkeypatch.setattr(me, "index_health",
                        lambda: {"memories": 10, "embedded": 8, "missing": 2, "stale": 0})
    monkeypatch.setattr(event_bus, "read_jobs",
                        lambda *a, **k: [{"event_id": "x"}] if k.get("status_filter") == "failed" else [])
    lines = boot_ritual.index_health_lines()
    assert any("2" in l for l in lines), "missing-embedding count not surfaced"
    assert any(("outbox" in l.lower()) or ("dead" in l.lower()) for l in lines)


def test_boot_health_lines_silent_when_clean(monkeypatch):
    import boot_ritual
    import event_bus
    monkeypatch.setattr(me, "index_health",
                        lambda: {"memories": 10, "embedded": 10, "missing": 0, "stale": 0})
    monkeypatch.setattr(event_bus, "read_jobs", lambda *a, **k: [])
    assert boot_ritual.index_health_lines() == []
