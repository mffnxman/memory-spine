"""TDD: stop the session_log_append flood + compact jobs.jsonl.

session_log.append emitted a 'session_log_append' event with NO outbox consumer
(87% of all events). jobs.jsonl is append-only and full-scanned on every drain
(1447 rows / 212KB live). Drop the consumerless emit; compact jobs.jsonl to the
latest row per event_id (events.jsonl, the immutable log, is never touched).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import event_bus as eb


def _write_jobs(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_compact_keeps_latest_status_per_event(tmp_path, monkeypatch):
    jl = tmp_path / "jobs.jsonl"
    monkeypatch.setattr(eb, "JOBS_LOG", jl)
    _write_jobs(jl, [
        {"event_id": "A", "status": "pending", "attempts": 0, "updated_at": "2026-05-20T00:00:00+00:00"},
        {"event_id": "A", "status": "processing", "attempts": 0, "updated_at": "2026-05-20T00:00:01+00:00"},
        {"event_id": "A", "status": "done", "attempts": 0, "updated_at": "2026-05-29T00:00:00+00:00"},
        {"event_id": "B", "status": "pending", "attempts": 0, "updated_at": "2026-05-29T00:00:00+00:00"},
    ])
    removed = eb.compact_jobs(keep_done_days=3650)  # keep recent done
    kept = [l for l in jl.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(kept) == 2 and removed == 2
    statuses = {j["event_id"]: j["status"] for j in eb.read_jobs()}
    assert statuses == {"A": "done", "B": "pending"}


def test_compact_drops_old_done_keeps_pending(tmp_path, monkeypatch):
    jl = tmp_path / "jobs.jsonl"
    monkeypatch.setattr(eb, "JOBS_LOG", jl)
    _write_jobs(jl, [
        {"event_id": "OLD", "status": "done", "attempts": 0, "updated_at": "2020-01-01T00:00:00+00:00"},
        {"event_id": "B", "status": "pending", "attempts": 0, "updated_at": "2026-05-29T00:00:00+00:00"},
    ])
    eb.compact_jobs(keep_done_days=30)
    ids = {j["event_id"] for j in eb.read_jobs()}
    assert ids == {"B"}  # old done dropped; pending always kept


def test_session_log_append_no_longer_emits(monkeypatch, tmp_path):
    import session_log
    emitted = []
    monkeypatch.setattr(eb, "emit_event", lambda *a, **k: emitted.append(a))
    monkeypatch.setattr(session_log, "ACTIVE_LOG", tmp_path / "active.md")
    session_log.append("a test activity line")
    assert emitted == [], "session_log.append still emits a consumerless event"
