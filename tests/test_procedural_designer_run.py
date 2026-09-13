"""TDD P6: designer orchestration + observability.

run_designer ties the three passes (failure / feedback / epilogue) into one
sleep-cycle entrypoint, gated by procedural_extraction_enabled. index_stats and
_telemetry give the layer the same observability the rest of the brain has.
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import paths as _paths  # noqa: E402
import procedural_lib as pl
import memory_engine as me

OBS_DDL = """
CREATE TABLE observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT, ts INTEGER, tool_name TEXT,
    tool_input_excerpt TEXT, tool_output_excerpt TEXT,
    cmd_excerpt TEXT, file_paths TEXT
);
"""


def _reset():
    pl.ensure_schema()
    with me.db() as c:
        c.execute("DELETE FROM heuristics")
        c.commit()


def _obs_with_pair():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(OBS_DDL)
    c.execute("INSERT INTO observations (session_id,ts,tool_name,cmd_excerpt,tool_output_excerpt) "
              "VALUES ('s1',100,'Bash','python3 idx.py','{\"stderr\":\"UnicodeEncodeError: x\"}')")
    c.execute("INSERT INTO observations (session_id,ts,tool_name,cmd_excerpt,tool_output_excerpt) "
              "VALUES ('s1',160,'Bash','PYTHONIOENCODING=utf-8 python3 idx.py','{\"stdout\":\"ok\"}')")
    c.commit()
    return c


def _stub_llm(_p):
    return ('{"trigger":"running python3 on Windows with unicode output",'
            '"action":"set PYTHONIOENCODING=utf-8 first","insight":"ascii codec"}')


def test_run_designer_skips_when_flag_off(monkeypatch):
    _reset()
    monkeypatch.setattr(pl, "_enabled", lambda name: False)
    out = pl.run_designer(obs_conn=_obs_with_pair(), llm_fn=_stub_llm)
    assert out.get("skipped")
    with me.db() as c:
        assert c.execute("SELECT count(*) FROM heuristics").fetchone()[0] == 0


def test_run_designer_failure_pass_mints_heuristic(monkeypatch):
    _reset()
    monkeypatch.setattr(pl, "_enabled", lambda name: True)
    out = pl.run_designer(obs_conn=_obs_with_pair(), llm_fn=_stub_llm)
    assert out["failure"] == 1
    with me.db() as c:
        n = c.execute("SELECT count(*) FROM heuristics WHERE origin='tool_recovery'").fetchone()[0]
    assert n == 1


def test_run_designer_failure_pass_is_idempotent(monkeypatch):
    _reset()
    monkeypatch.setattr(pl, "_enabled", lambda name: True)
    pl.run_designer(obs_conn=_obs_with_pair(), llm_fn=_stub_llm)
    out2 = pl.run_designer(obs_conn=_obs_with_pair(), llm_fn=_stub_llm)
    assert out2["failure"] == 0  # same obs ids already mined


def test_index_stats_counts_by_status():
    _reset()
    a = pl.upsert_heuristic({"trigger": "t", "action": "a", "insight": ""}, origin="x", polarity="failure_derived")["id"]
    pl.upsert_heuristic({"trigger": "totally different topic about gardening", "action": "b", "insight": ""}, origin="x", polarity="failure_derived")
    with me.db() as c:
        c.execute("UPDATE heuristics SET status='archived' WHERE id=?", (a,))
        c.commit()
    s = pl.index_stats()
    assert s["active"] == 1 and s["archived"] == 1


def test_telemetry_writes_a_record():
    pl._telemetry({"event": "test_probe", "n": 7})
    path = _paths.META_DIR / "v3_2_telemetry.jsonl"
    last = path.read_text(encoding="utf-8").strip().splitlines()[-1]
    rec = json.loads(last)
    assert rec["component"] == "procedural" and rec["event"] == "test_probe"
