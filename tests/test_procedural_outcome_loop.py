"""Tests for the procedural-L2 outcome loop (thread t9).

Covers: injected_since, _session_start_ts, _injected_habits_section,
review_epilogue (idempotent downvote), burn() telemetry, the CLI `review`
subcommand, and the session_end wiring. DB isolation is provided by
tests/conftest.py — seeds hit an isolated copy, never the real habit pool.
"""
import sys, time, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import paths as _paths  # noqa: E402
import procedural_lib as pl
import session_end as se
import epilogue as ep


def _seed(trigger, action, last_used_ts=None, status="active", importance=2):
    """Insert one heuristic; return its id. Hits the isolated test DB via conftest."""
    pl.ensure_schema()
    with pl.me.db() as c:
        cur = c.execute(
            "INSERT INTO heuristics (trigger, action, insight, origin, polarity, "
            "importance, created_ts, last_used_ts, status) VALUES (?,?,?,?,?,?,?,?,?)",
            (trigger, action, "", "test", "failure_derived", importance,
             int(time.time()), last_used_ts, status))
        c.commit()
        return cur.lastrowid


def test_injected_since_returns_only_active_recent(monkeypatch):
    cutoff = 10_000
    old = _seed("When old", "do old", last_used_ts=cutoff - 5)
    fresh = _seed("When fresh", "do fresh", last_used_ts=cutoff + 5)
    never = _seed("When never", "do never", last_used_ts=None)
    archived = _seed("When arch", "do arch", last_used_ts=cutoff + 5, status="archived")
    ids = [h["id"] for h in pl.injected_since(cutoff)]
    assert fresh in ids
    assert old not in ids and never not in ids and archived not in ids


def test_session_start_ts_reads_marker(monkeypatch, tmp_path):
    marker = tmp_path / ".session_start"
    marker.write_text("1700000000")
    monkeypatch.setattr(se, "_SESSION_START_MARKER", marker)
    assert se._session_start_ts() == 1700000000


def test_session_start_ts_none_when_absent_or_garbage(monkeypatch, tmp_path):
    monkeypatch.setattr(se, "_SESSION_START_MARKER", tmp_path / "missing")
    assert se._session_start_ts() is None
    bad = tmp_path / ".session_start"; bad.write_text("not-a-number")
    monkeypatch.setattr(se, "_SESSION_START_MARKER", bad)
    assert se._session_start_ts() is None


def test_injected_habits_section_empty_is_blank():
    assert se._injected_habits_section([]) == ""


def test_injected_habits_section_lists_each_habit():
    habits = [{"id": 42, "trigger": "When debugging", "action": "do reproduce first"},
              {"id": 17, "trigger": "When editing hooks", "action": "do drain the queue first"}]
    out = se._injected_habits_section(habits)
    assert "## Habits injected this session" in out
    assert "[h:42] When debugging → do reproduce first" in out
    assert "[h:17]" in out
    assert "❌" in out  # the dud-flag legend


def test_review_epilogue_downvotes_only_flagged(tmp_path):
    keep = _seed("When keep", "do keep", importance=2)
    dud = _seed("When dud", "do dud", importance=2)
    ep = tmp_path / "2026-06-03-x.md"
    ep.write_text(
        f"## Habits injected this session\n"
        f"- [h:{keep}] When keep → do keep\n"
        f"- [h:{dud}] When dud → do dud  ❌\n", encoding="utf-8")
    downvoted = pl.review_epilogue(ep)
    assert downvoted == [dud]
    with pl.me.db() as c:
        c.row_factory = pl.sqlite3.Row
        imp = {r["id"]: r["importance"] for r in c.execute("SELECT id, importance FROM heuristics")}
    assert imp[dud] == 1 and imp[keep] == 2          # only the dud lost a point
    assert pl.review_epilogue(ep) == []              # idempotent: re-run is a no-op
    with pl.me.db() as c:
        c.row_factory = pl.sqlite3.Row
        imp2 = {r["id"]: r["importance"] for r in c.execute("SELECT id, importance FROM heuristics")}
    assert imp2[dud] == 1                              # not double-downvoted


def test_burn_traces_injected(monkeypatch):
    import time as _t
    recent = _seed("When recent", "do x", last_used_ts=int(_t.time()) - 60)
    stale = _seed("When stale", "do y", last_used_ts=int(_t.time()) - 10 * 3600)
    traces = []
    monkeypatch.setattr(pl, "_telemetry", lambda rec: traces.append(rec))
    pl.burn(recent); pl.burn(stale)
    by_id = {t["id"]: t for t in traces if t.get("event") == "burned"}
    assert by_id[recent]["was_injected"] is True
    assert by_id[stale]["was_injected"] is False


def test_cli_review_invokes_review_epilogue(monkeypatch):
    called = {}
    monkeypatch.setattr(pl, "review_epilogue", lambda path: called.setdefault("path", path) or [7])
    monkeypatch.setattr(sys, "argv", ["procedural_lib.py", "review", "/tmp/ep.md"])
    pl.main()
    assert called["path"] == "/tmp/ep.md"


def test_append_habits_review_writes_section_and_sidecar(monkeypatch, tmp_path):
    draft = tmp_path / "draft.md"; draft.write_text("# Draft\n\nbody\n", encoding="utf-8")
    monkeypatch.setattr(se, "_session_start_ts", lambda: 1700000000)
    monkeypatch.setattr(se, "META_DIR", tmp_path)
    import procedural_lib
    monkeypatch.setattr(procedural_lib, "injected_since",
                        lambda ts: [{"id": 9, "trigger": "When X", "action": "do Y"}])
    se._append_habits_review(draft)
    body = draft.read_text(encoding="utf-8")
    assert "## Habits injected this session" in body and "[h:9]" in body
    sidecar = json.loads((tmp_path / ".session_habits.json").read_text(encoding="utf-8"))
    assert sidecar["session_start"] == 1700000000 and sidecar["injected"][0]["id"] == 9


def test_append_habits_review_skips_without_session_start(monkeypatch, tmp_path):
    draft = tmp_path / "draft.md"; draft.write_text("# Draft\n", encoding="utf-8")
    monkeypatch.setattr(se, "_session_start_ts", lambda: None)
    se._append_habits_review(draft)
    assert "Habits injected" not in draft.read_text(encoding="utf-8")


# ─── Seam hardening: epilogue.py auto-carries the section from the sidecar ────

def test_habits_section_from_sidecar_renders(monkeypatch, tmp_path):
    sc = tmp_path / ".session_habits.json"
    sc.write_text(json.dumps({"session_start": 1,
                              "injected": [{"id": 9, "trigger": "When X", "action": "do Y"}]}),
                  encoding="utf-8")
    monkeypatch.setattr(ep, "_SESSION_HABITS_SIDECAR", sc)
    out = ep._habits_section_from_sidecar()
    assert "## Habits injected this session" in out and "[h:9]" in out


def test_habits_section_from_sidecar_blank_when_absent_or_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(ep, "_SESSION_HABITS_SIDECAR", tmp_path / "missing.json")
    assert ep._habits_section_from_sidecar() == ""
    empty = tmp_path / ".session_habits.json"
    empty.write_text(json.dumps({"injected": []}), encoding="utf-8")
    monkeypatch.setattr(ep, "_SESSION_HABITS_SIDECAR", empty)
    assert ep._habits_section_from_sidecar() == ""


def test_write_command_auto_appends_habits_section(monkeypatch, tmp_path):
    import io
    monkeypatch.setattr(ep, "EPILOGUE_DIR", tmp_path)
    sc = tmp_path / ".session_habits.json"
    sc.write_text(json.dumps({"session_start": 1,
                              "injected": [{"id": 9, "trigger": "When X", "action": "do Y"}]}),
                  encoding="utf-8")
    monkeypatch.setattr(ep, "_SESSION_HABITS_SIDECAR", sc)
    monkeypatch.setattr(ep, "_observer_flag", lambda *a, **k: False)  # observer ingest off
    monkeypatch.setattr(sys, "argv", ["epilogue.py", "write"])
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"session_label": "t"}'))
    ep.main()
    written = list(tmp_path.glob("*.md"))
    assert len(written) == 1
    body = written[0].read_text(encoding="utf-8")
    assert "## Habits injected this session" in body and "[h:9]" in body


def test_write_command_decodes_utf8_stdin():
    """Regression: non-ASCII in the epilogue body (❌, →) must not crash `write`.
    Forces a cp1252 stdin (the Windows default that bit us writing this very
    epilogue) via env; the fix reconfigures stdin to UTF-8 so the bytes decode
    cleanly. Spawns the real CLI so the OS-level stdin path is exercised; writes
    one real epilogue and removes it."""
    import subprocess, os, re
    script = _paths.SCRIPTS_DIR / "epilogue.py"
    payload = '{"session_label": "utf8-regression", "what_we_did": "arrow → cross ❌"}'
    env = {**os.environ, "PYTHONUTF8": "0", "PYTHONIOENCODING": "cp1252",
           "OBSERVER_FLAG_OBSERVER_EPILOGUE_INGEST_ENABLED": "0"}
    r = subprocess.run([sys.executable, str(script), "write"],
                       input=payload.encode("utf-8"), capture_output=True, env=env)
    assert r.returncode == 0, f"write crashed on utf-8 stdin: {r.stderr.decode('utf-8', 'replace')}"
    m = re.search(r"Wrote (.+\.md)", r.stdout.decode("utf-8", "replace"))
    assert m, f"no Wrote line in stdout: {r.stdout.decode('utf-8', 'replace')}"
    path = Path(m.group(1).strip())
    try:
        body = path.read_text(encoding="utf-8")
        assert "❌" in body and "→" in body
    finally:
        path.unlink(missing_ok=True)
        Path(str(path) + ".reviewed").unlink(missing_ok=True)


# ─── Loop closure: auto-apply ❌ flags at boot + always-write sidecar (H1/H3) ──

def test_review_recent_epilogues_applies_finalized_flags_only(tmp_path):
    keep = _seed("When keep", "do keep", importance=2)
    dud = _seed("When dud", "do dud", importance=2)
    drafted = _seed("When draft", "do draft", importance=2)
    # finalized epilogue: one flagged dud + one unflagged keep
    (tmp_path / "2026-06-03-1000.md").write_text(
        f"## Habits injected this session\n"
        f"- [h:{keep}] When keep → do keep\n"
        f"- [h:{dud}] When dud → do dud  ❌\n", encoding="utf-8")
    # a DRAFT epilogue with a flagged habit — must be IGNORED
    (tmp_path / "draft-2026-06-03-1001.md").write_text(
        f"- [h:{drafted}] When draft → do draft  ❌\n", encoding="utf-8")
    summary = pl.review_recent_epilogues(tmp_path)
    assert dud in summary["downvoted"]
    assert keep not in summary["downvoted"] and drafted not in summary["downvoted"]
    with pl.me.db() as c:
        c.row_factory = pl.sqlite3.Row
        imp = {r["id"]: r["importance"] for r in c.execute("SELECT id, importance FROM heuristics")}
    assert imp[dud] == 1 and imp[keep] == 2 and imp[drafted] == 2   # only the flagged finalized id
    # idempotent: re-run downvotes nothing more
    assert pl.review_recent_epilogues(tmp_path)["downvoted"] == []
    with pl.me.db() as c:
        c.row_factory = pl.sqlite3.Row
        imp2 = {r["id"]: r["importance"] for r in c.execute("SELECT id, importance FROM heuristics")}
    assert imp2[dud] == 1


def test_procedural_review_line_summarizes_downvotes(monkeypatch):
    import boot_ritual
    monkeypatch.setattr(pl, "review_recent_epilogues",
                        lambda *a, **k: {"reviewed": ["e.md"], "downvoted": [7, 9]})
    line = boot_ritual._procedural_review_line()
    assert line is not None and "2 habit downvote" in line


def test_procedural_review_line_none_when_clean_or_error(monkeypatch):
    import boot_ritual
    monkeypatch.setattr(pl, "review_recent_epilogues",
                        lambda *a, **k: {"reviewed": ["e.md"], "downvoted": []})
    assert boot_ritual._procedural_review_line() is None
    def boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(pl, "review_recent_epilogues", boom)
    assert boot_ritual._procedural_review_line() is None  # fail-soft


def test_append_habits_review_always_writes_sidecar_even_when_empty(monkeypatch, tmp_path):
    draft = tmp_path / "draft.md"; draft.write_text("# Draft\n", encoding="utf-8")
    monkeypatch.setattr(se, "_session_start_ts", lambda: 1700000000)
    monkeypatch.setattr(se, "META_DIR", tmp_path)
    import procedural_lib
    monkeypatch.setattr(procedural_lib, "injected_since", lambda ts: [])  # no injections
    se._append_habits_review(draft)
    assert "Habits injected" not in draft.read_text(encoding="utf-8")          # no section
    sidecar = json.loads((tmp_path / ".session_habits.json").read_text(encoding="utf-8"))
    assert sidecar["injected"] == []                                            # but sidecar IS written


def test_append_habits_review_no_stale_carryover(monkeypatch, tmp_path):
    draft = tmp_path / "draft.md"; draft.write_text("# Draft\n", encoding="utf-8")
    monkeypatch.setattr(se, "_session_start_ts", lambda: 1700000000)
    monkeypatch.setattr(se, "META_DIR", tmp_path)
    import procedural_lib
    # session A injects a habit → sidecar holds its id
    monkeypatch.setattr(procedural_lib, "injected_since",
                        lambda ts: [{"id": 5, "trigger": "When A", "action": "do A"}])
    se._append_habits_review(draft)
    assert json.loads((tmp_path / ".session_habits.json").read_text(encoding="utf-8"))["injected"][0]["id"] == 5
    # session B injects nothing → sidecar overwritten empty, NOT stale id 5
    monkeypatch.setattr(procedural_lib, "injected_since", lambda ts: [])
    se._append_habits_review(draft)
    assert json.loads((tmp_path / ".session_habits.json").read_text(encoding="utf-8"))["injected"] == []
