"""TDD P1: procedural designer — failure-pass (tool error -> recovery).

The failure-derived signal: within a session, a tool call that errored followed
by a similar call that succeeded is a recovered mistake — the change between them
is the lesson. The designer detects those pairs, asks an LLM to contrast them into
a trigger->action heuristic, and upserts into the pool (ADD new, or UPVOTE/EDIT a
near-duplicate per ExpeL's importance-counter lifecycle).

LLM calls are injected (llm_fn) so extraction is tested deterministically — the
one unavoidable mock. Detection, pairing, parsing, and dedup are all real code.
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
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


def _obs_conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(OBS_DDL)
    return c


def _ins(c, **kw):
    keys = ",".join(kw)
    qs = ",".join("?" for _ in kw)
    c.execute(f"INSERT INTO observations ({keys}) VALUES ({qs})", tuple(kw.values()))
    c.commit()


# ── error detection ──────────────────────────────────────────────────────────
def test_looks_like_error_detects_traceback_and_stderr():
    assert pl._looks_like_error('{"stdout": "", "stderr": "Traceback (most recent call last):\\nUnicodeEncodeError"}')
    assert pl._looks_like_error('{"stdout": "ModuleNotFoundError: No module named foo", "stderr": ""}')


def test_looks_like_error_clean_output_is_not_error():
    assert not pl._looks_like_error('{"stdout": "3 passed in 0.03s", "stderr": ""}')


def test_looks_like_error_ignores_errorwords_in_file_content():
    # cat-ing a doc/source that mentions errors mid-line is NOT a failure.
    assert not pl._looks_like_error(
        '{"stdout": "## Lessons\\n42: raise ValueError(bad)\\nhandle UnicodeEncodeError gracefully", "stderr": ""}')


# ── error -> recovery pairing ──────────────────────────────────────────────────
def test_detect_error_recoveries_finds_a_pair():
    c = _obs_conn()
    sid = "s1"
    _ins(c, session_id=sid, ts=100, tool_name="Bash",
         cmd_excerpt="python3 build_index.py",
         tool_output_excerpt='{"stdout":"","stderr":"UnicodeEncodeError: ascii codec"}')
    _ins(c, session_id=sid, ts=160, tool_name="Bash",
         cmd_excerpt="PYTHONIOENCODING=utf-8 python3 build_index.py",
         tool_output_excerpt='{"stdout":"indexed 70 ok","stderr":""}')
    pairs = pl.detect_error_recoveries(c)
    assert len(pairs) == 1
    assert pairs[0]["error"]["id"] == 1
    assert pairs[0]["recovery"]["id"] == 2


def test_detect_skips_search_tools_with_errorish_content():
    # A Grep whose RESULTS contain the string "ValueError" (it's grepping source
    # code) is NOT an execution failure. Two such greps must not become a pair.
    c = _obs_conn()
    _ins(c, session_id="s1", ts=100, tool_name="Grep",
         tool_input_excerpt='{"pattern":"def foo","path":"app/tools.py"}',
         tool_output_excerpt='{"stdout":"42: raise ValueError(bad)\\n88: except TypeError"}')
    _ins(c, session_id="s1", ts=160, tool_name="Grep",
         tool_input_excerpt='{"pattern":"def bar","path":"app/tools.py"}',
         tool_output_excerpt='{"stdout":"12: def bar(): pass"}')
    assert pl.detect_error_recoveries(c) == []


def test_detect_only_pairs_executor_tools():
    # Even with a real error marker, only executor tools (Bash/PowerShell) count;
    # a Read that errored then succeeded is not an actionable "how to run" lesson.
    c = _obs_conn()
    _ins(c, session_id="s1", ts=100, tool_name="Read",
         tool_input_excerpt='{"file_path":"/x"}',
         tool_output_excerpt='{"stderr":"FileNotFoundError: /x"}')
    _ins(c, session_id="s1", ts=160, tool_name="Read",
         tool_input_excerpt='{"file_path":"/x"}',
         tool_output_excerpt='{"stdout":"ok"}')
    assert pl.detect_error_recoveries(c) == []


def test_detect_ignores_unrecovered_error():
    c = _obs_conn()
    _ins(c, session_id="s1", ts=100, tool_name="Bash", cmd_excerpt="python3 x.py",
         tool_output_excerpt='{"stdout":"","stderr":"Traceback: boom"}')
    # a later UNRELATED successful call must not be treated as the recovery
    _ins(c, session_id="s1", ts=160, tool_name="Read",
         tool_input_excerpt='{"file_path":"/tmp/other"}',
         tool_output_excerpt='{"stdout":"contents"}')
    assert pl.detect_error_recoveries(c) == []


# ── LLM extraction (stubbed) ───────────────────────────────────────────────────
def test_extract_heuristic_parses_fenced_json():
    pair = {"tool_name": "Bash",
            "error": {"cmd_excerpt": "python3 build_index.py", "tool_output_excerpt": "UnicodeEncodeError"},
            "recovery": {"cmd_excerpt": "PYTHONIOENCODING=utf-8 python3 build_index.py", "tool_output_excerpt": "ok"}}

    def fake_llm(prompt):
        return ('Here is the heuristic:\n```json\n'
                '{"trigger": "running python3 on this Windows box with unicode output",'
                ' "action": "set PYTHONIOENCODING=utf-8 before the command",'
                ' "insight": "default ascii codec crashes on unicode stdout"}\n```')

    h = pl.extract_heuristic(pair, llm_fn=fake_llm)
    assert h["trigger"].startswith("running python3")
    assert "PYTHONIOENCODING" in h["action"]


def test_extract_heuristic_returns_none_on_garbage():
    pair = {"tool_name": "Bash", "error": {}, "recovery": {}}
    assert pl.extract_heuristic(pair, llm_fn=lambda p: "sorry, no idea") is None


# ── upsert + dedup (ExpeL importance counter) ──────────────────────────────────
def test_upsert_adds_then_dedups_near_duplicate():
    pl.ensure_schema()
    with me.db() as c:
        c.execute("DELETE FROM heuristics")
        c.commit()

    r1 = pl.upsert_heuristic(
        {"trigger": "running python3 on Windows produces a UnicodeEncodeError",
         "action": "set PYTHONIOENCODING=utf-8 first", "insight": ""},
        origin="tool_recovery", polarity="failure_derived", source_session="s1")
    assert r1["op"] == "added"

    r2 = pl.upsert_heuristic(
        {"trigger": "running python3 on Windows causes a UnicodeEncodeError crash",
         "action": "export PYTHONIOENCODING=utf-8 before running", "insight": ""},
        origin="tool_recovery", polarity="failure_derived", source_session="s2")
    assert r2["op"] == "upvoted"
    assert r2["id"] == r1["id"]

    with me.db() as c:
        rows = c.execute("SELECT importance, corroboration FROM heuristics WHERE status='active'").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 3   # importance: 2 -> 3 on upvote
    assert rows[0][1] == 2   # corroboration: 1 -> 2
