"""TDD: event_bus durability-spine resilience + observability (recon H2 + H6).

- read_jobs must skip a single malformed line instead of returning [] for the
  whole file (a torn write should cost one row, not blank the entire queue).
- the otherwise-silent fail-soft swallows on the durability spine emit a one-line
  telemetry breadcrumb so a degraded outbox can be seen, not guessed.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import event_bus as eb


def test_read_jobs_skips_bad_line(tmp_path, monkeypatch):
    jl = tmp_path / "jobs.jsonl"
    monkeypatch.setattr(eb, "JOBS_LOG", jl)
    monkeypatch.setattr(eb, "META_DIR", tmp_path)
    jl.write_text(
        json.dumps({"event_id": "A", "status": "pending", "updated_at": "2026-05-29T00:00:00+00:00"}) + "\n"
        + "{ this is not valid json\n"
        + json.dumps({"event_id": "B", "status": "done", "updated_at": "2026-05-29T00:00:01+00:00"}) + "\n",
        encoding="utf-8")
    ids = {j["event_id"] for j in eb.read_jobs()}
    assert ids == {"A", "B"}  # one bad line dropped, NOT the whole file


def test_read_jobs_bad_line_emits_breadcrumb(tmp_path, monkeypatch):
    jl = tmp_path / "jobs.jsonl"
    monkeypatch.setattr(eb, "JOBS_LOG", jl)
    monkeypatch.setattr(eb, "META_DIR", tmp_path)
    jl.write_text("{bad line\n" + json.dumps({"event_id": "A", "status": "pending"}) + "\n", encoding="utf-8")
    eb.read_jobs()
    tel = tmp_path / "v3_2_telemetry.jsonl"
    assert tel.exists() and "read_jobs_bad_line" in tel.read_text(encoding="utf-8")


def test_emit_event_failure_emits_breadcrumb(tmp_path, monkeypatch):
    monkeypatch.setattr(eb, "META_DIR", tmp_path)
    def _boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(eb, "_append_jsonl", _boom)
    assert eb.emit_event("memory_write", {"x": 1}) is None  # fail-soft preserved
    tel = tmp_path / "v3_2_telemetry.jsonl"
    assert tel.exists() and "emit_event_error" in tel.read_text(encoding="utf-8")
