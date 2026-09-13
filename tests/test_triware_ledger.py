"""TDD: Triware Ledger Phase 1 — cross-agent observation feedstock (flag OFF)."""
import json, sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import event_bus as eb
import outbox_worker as ow


def test_triware_flag_registered_off():
    import feature_flags
    flags = feature_flags.all_flags()
    assert "triware_ledger_enabled" in flags
    assert isinstance(flags["triware_ledger_enabled"], bool)
    assert flags["triware_ledger_enabled"] is False


def test_schema_version_is_additive_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(eb, "EVENTS_LOG", tmp_path / "events.jsonl")
    monkeypatch.setattr(eb, "JOBS_LOG", tmp_path / "jobs.jsonl")
    monkeypatch.setattr(eb, "META_DIR", tmp_path)
    eid = eb.emit_event("test_event", {"a": 1})
    rec = eb.lookup_event(eid)
    assert rec["schema_version"] == 1
    # event_id is computed from (ts, sid, kind, payload) ONLY — schema_version excluded
    assert rec["event_id"] == eb.compute_event_id(
        rec["timestamp"], rec["session_id"], "test_event", {"a": 1})


def test_lookup_event_tolerates_torn_line(tmp_path, monkeypatch):
    log = tmp_path / "events.jsonl"
    monkeypatch.setattr(eb, "EVENTS_LOG", log)
    monkeypatch.setattr(eb, "META_DIR", tmp_path)
    good_a = {"event_id": "sha256:aaa", "kind": "x", "payload": {}}
    good_b = {"event_id": "sha256:bbb", "kind": "y", "payload": {}}
    log.write_text(
        json.dumps(good_a) + "\n" + '{"event_id": "sha256:tor'  # torn line
        + "\n" + json.dumps(good_b) + "\n", encoding="utf-8")
    assert eb.lookup_event("sha256:bbb") == good_b  # valid neighbor survives
    assert eb.lookup_event("sha256:aaa") == good_a


def test_observation_kinds_constant():
    assert ow.OBSERVATION_KINDS == frozenset({"agent_activity", "duck_signal", "dartagnan_task"})


def test_kind_gate_quarantines_agent_canon_write(monkeypatch):
    called = {"process": False}
    import memory_write_postprocess as mwp
    monkeypatch.setattr(mwp, "_process", lambda fp: called.__setitem__("process", True))
    ow.process_event({"kind": "memory_write", "platform_source": "dartagnan",
                      "payload": {"file_path": "/x/attacker.md"}})
    assert called["process"] is False  # gate blocked the low-trust canon write


def test_kind_gate_allows_claude_code_memory_write(monkeypatch):
    called = {"process": False}
    import memory_write_postprocess as mwp
    monkeypatch.setattr(mwp, "_process", lambda fp: called.__setitem__("process", True))
    ow.process_event({"kind": "memory_write", "platform_source": "claude_code",
                      "payload": {"file_path": "/x/real.md"}})
    assert called["process"] is True  # no regression for claude_code


def test_observation_kind_logged_not_routed(monkeypatch):
    seen = []
    monkeypatch.setattr(eb, "_telemetry", lambda rec: seen.append(rec))
    import memory_write_postprocess as mwp
    monkeypatch.setattr(mwp, "_process", lambda fp: (_ for _ in ()).throw(AssertionError("routed!")))
    ow.process_event({"kind": "duck_signal", "platform_source": "duck_sentinel", "payload": {"x": 1}})
    assert any(r.get("event") == "agent_observation"
               and r.get("platform_source") == "duck_sentinel" for r in seen)


def test_claude_code_unknown_kind_is_silent(monkeypatch):
    # claude_code unknown kinds reach the silent else (byte-identical to pre-Triware);
    # agent unknown kinds never get here — the gate quarantines them first.
    seen = []
    monkeypatch.setattr(eb, "_telemetry", lambda rec: seen.append(rec))
    ow.process_event({"kind": "legacy_test_event", "platform_source": "claude_code", "payload": {}})
    assert seen == []  # silent — no behavior change for claude_code unknown kinds


def test_agent_unknown_kind_quarantined_by_gate(monkeypatch):
    seen = []
    monkeypatch.setattr(eb, "_telemetry", lambda rec: seen.append(rec))
    ow.process_event({"kind": "totally_unknown", "platform_source": "dartagnan", "payload": {}})
    assert any(r.get("event") == "kind_gate_quarantine" and r.get("kind") == "totally_unknown"
               for r in seen)


def test_kind_gate_quarantines_missing_platform_source(monkeypatch):
    # HIGH fix: a non-observation event with NO platform_source defaults to UNTRUSTED.
    called = {"process": False}
    import memory_write_postprocess as mwp
    monkeypatch.setattr(mwp, "_process", lambda fp: called.__setitem__("process", True))
    ow.process_event({"kind": "memory_write", "payload": {"file_path": "/x/no_source.md"}})
    assert called["process"] is False  # missing source -> untrusted -> quarantined


def test_process_event_tolerates_non_dict_event():
    ow.process_event(None)       # must not raise
    ow.process_event([1, 2, 3])  # must not raise


def test_lookup_event_tolerates_nondict_json_line(tmp_path, monkeypatch):
    log = tmp_path / "events.jsonl"
    monkeypatch.setattr(eb, "EVENTS_LOG", log)
    monkeypatch.setattr(eb, "META_DIR", tmp_path)
    good = {"event_id": "sha256:zzz", "kind": "y", "payload": {}}
    log.write_text("[1, 2, 3]\n" + json.dumps(good) + "\n", encoding="utf-8")  # valid JSON, non-dict
    assert eb.lookup_event("sha256:zzz") == good  # non-dict line doesn't dead-letter the neighbor


def test_client_event_id_matches_event_bus():
    import triware_client as tc
    ts, sid = "2026-06-04T00:00:00+00:00", "s1"
    assert tc.compute_event_id(ts, sid, "duck_signal", {"a": 1}) == \
        eb.compute_event_id(ts, sid, "duck_signal", {"a": 1})  # no canonicalization drift


def test_client_observation_kinds_match_worker():
    import triware_client as tc
    assert tc.OBSERVATION_KINDS == ow.OBSERVATION_KINDS  # no drift between client + gate


def test_client_refuses_non_observation_kind(tmp_path, monkeypatch):
    import triware_client as tc
    monkeypatch.setenv("MEMORY_FLAG_TRIWARE_LEDGER_ENABLED", "1")
    monkeypatch.setenv("DUCK_RUNTIME", "1")
    ev, jb = tmp_path / "events.jsonl", tmp_path / "jobs.jsonl"
    monkeypatch.setattr(tc, "_events_path", lambda: ev)
    monkeypatch.setattr(tc, "_jobs_path", lambda: jb)
    assert tc.emit("memory_write", {"file_path": "/x.md"}) is None
    assert not ev.exists()  # nothing written


def test_client_emits_observation_when_enabled(tmp_path, monkeypatch):
    import triware_client as tc
    monkeypatch.setenv("MEMORY_FLAG_TRIWARE_LEDGER_ENABLED", "1")
    monkeypatch.setenv("DUCK_RUNTIME", "1")
    ev, jb = tmp_path / "events.jsonl", tmp_path / "jobs.jsonl"
    monkeypatch.setattr(tc, "_events_path", lambda: ev)
    monkeypatch.setattr(tc, "_jobs_path", lambda: jb)
    monkeypatch.setattr(tc, "_debounce_path", lambda: tmp_path / "deb.json")
    eid = tc.emit("duck_signal", {"hit": "recon"})
    assert eid and eid.startswith("sha256:")
    rec = json.loads(ev.read_text(encoding="utf-8").strip())
    assert rec["kind"] == "duck_signal" and rec["platform_source"] == "duck_sentinel"
    assert rec["schema_version"] == 1 and rec["event_id"] == eid
    job = json.loads(jb.read_text(encoding="utf-8").strip())
    assert job["status"] == "pending" and job["event_id"] == eid


def test_client_flag_off_emits_nothing(tmp_path, monkeypatch):
    import triware_client as tc
    monkeypatch.delenv("MEMORY_FLAG_TRIWARE_LEDGER_ENABLED", raising=False)
    monkeypatch.setattr(tc, "_enabled", lambda: False)
    monkeypatch.setattr(tc, "_events_path", lambda: tmp_path / "events.jsonl")
    assert tc.emit("duck_signal", {"x": 1}) is None


def test_client_rejects_oversized_payload(tmp_path, monkeypatch):
    import triware_client as tc
    monkeypatch.setattr(tc, "_enabled", lambda: True)
    monkeypatch.setenv("DUCK_RUNTIME", "1")
    monkeypatch.setattr(tc, "_events_path", lambda: tmp_path / "events.jsonl")
    monkeypatch.setattr(tc, "_jobs_path", lambda: tmp_path / "jobs.jsonl")
    assert tc.emit("duck_signal", {"blob": "z" * 9000}) is None  # > 4KB


def test_client_failsoft_on_append_error(tmp_path, monkeypatch):
    import triware_client as tc
    monkeypatch.setattr(tc, "_enabled", lambda: True)
    monkeypatch.setenv("DUCK_RUNTIME", "1")
    monkeypatch.setattr(tc, "_events_path", lambda: tmp_path / "events.jsonl")
    monkeypatch.setattr(tc, "_jobs_path", lambda: tmp_path / "jobs.jsonl")
    monkeypatch.setattr(tc, "_debounce_path", lambda: tmp_path / "deb.json")
    def _boom(path, record): raise OSError("disk full")
    monkeypatch.setattr(tc, "_append_jsonl", _boom)
    assert tc.emit("duck_signal", {"x": 1}) is None  # never raises


def test_client_debounces_identical_payload_burst(tmp_path, monkeypatch):
    import triware_client as tc
    monkeypatch.setattr(tc, "_enabled", lambda: True)
    monkeypatch.setenv("DUCK_RUNTIME", "1")
    monkeypatch.setattr(tc, "_events_path", lambda: tmp_path / "events.jsonl")
    monkeypatch.setattr(tc, "_jobs_path", lambda: tmp_path / "jobs.jsonl")
    monkeypatch.setattr(tc, "_debounce_path", lambda: tmp_path / "deb.json")
    n_written = sum(1 for _ in range(100) if tc.emit("duck_signal", {"hb": "tick"}) is not None)
    assert n_written == 1  # 99 identical heartbeats debounced
    assert tc.emit("duck_signal", {"hb": "different"}) is not None  # different payload passes


def test_client_debounce_fails_open_on_read_error(tmp_path, monkeypatch):
    import triware_client as tc
    monkeypatch.setattr(tc, "_enabled", lambda: True)
    monkeypatch.setenv("DUCK_RUNTIME", "1")
    monkeypatch.setattr(tc, "_events_path", lambda: tmp_path / "events.jsonl")
    monkeypatch.setattr(tc, "_jobs_path", lambda: tmp_path / "jobs.jsonl")
    def _boom(): raise OSError("cannot read debounce")
    monkeypatch.setattr(tc, "_debounce_path", _boom)
    assert tc.emit("duck_signal", {"x": 1}) is not None  # fail-open: event allowed
