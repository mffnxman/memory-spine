"""TDD: sleep-time LLM compression of session observations.

The claude-mem comparison exposed two cons at once: our observations are raw
1KB excerpts (no semantic density until promotion), and background synthesis
burns subscription quota. Fix: a sleep-time pass that compresses each ended
session's observations into a dense semantic summary, routed LOCAL-FIRST to
d'Artagnan (zero quota when the local model is awake, subscription fallback when
not). Hot path untouched — the Stop-hook heuristic summarizer stays as-is.

Compressed summaries land in session_summaries (compressed_at stamped) and a
summaries FTS table so the prefetch fallback can find them semantically.
"""

import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import compress_sessions as cs

DAY = 86400


def _mkdb(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "observations.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE sessions (session_id TEXT PRIMARY KEY, started_at INTEGER,
            ended_at INTEGER, cwd TEXT, prompt_count INTEGER DEFAULT 0,
            obs_count INTEGER DEFAULT 0, status TEXT DEFAULT 'active');
        CREATE TABLE observations (id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, ts INTEGER, tool_name TEXT,
            tool_input_excerpt TEXT, tool_input_size INTEGER,
            tool_output_excerpt TEXT, tool_output_size INTEGER,
            file_paths TEXT, cmd_excerpt TEXT, ms_elapsed INTEGER,
            promoted_to_memory_id TEXT, referenced_count INTEGER DEFAULT 0,
            last_referenced_at INTEGER);
        CREATE TABLE session_summaries (session_id TEXT PRIMARY KEY,
            summary_text TEXT, key_files TEXT, key_topics TEXT,
            observation_count INTEGER, generated_at INTEGER);
        """)
    now = int(time.time())
    for i, (sid, status, has_summary) in enumerate(
        [
            ("s-new", "ended", True),
            ("s-old", "ended", True),
            ("s-live", "active", False),
        ]
    ):
        conn.execute(
            "INSERT INTO sessions VALUES (?,?,?,?,0,10,?)",
            (sid, now - (i + 1) * DAY, now - (i + 1) * DAY + 3600, "C:/x", status),
        )
        for j in range(4):
            conn.execute(
                "INSERT INTO observations (session_id, ts, tool_name, cmd_excerpt, file_paths) "
                "VALUES (?,?,?,?,?)",
                (
                    sid,
                    now - (i + 1) * DAY + j,
                    "Bash",
                    f"python build_thing.py --step {j}",
                    json.dumps(["C:/x/build_thing.py"]),
                ),
            )
        if has_summary:
            conn.execute(
                "INSERT INTO session_summaries VALUES (?,?,?,?,4,?)",
                (sid, "heuristic summary", "[]", "[]", now),
            )
    conn.commit()
    conn.close()
    return db


def test_migration_adds_compressed_at_idempotently(tmp_path):
    db = _mkdb(tmp_path)
    cs.ensure_schema(db)
    cs.ensure_schema(db)  # twice — must not raise
    conn = sqlite3.connect(db)
    cols = [d[1] for d in conn.execute("PRAGMA table_info(session_summaries)")]
    assert "compressed_at" in cols
    # FTS table for semantic search over summaries
    tables = [
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    ]
    assert any("summaries_fts" in t for t in tables)


def test_select_targets_ended_uncompressed_only(tmp_path):
    db = _mkdb(tmp_path)
    cs.ensure_schema(db)
    targets = cs.select_targets(db_path=db, cap=10)
    assert "s-new" in targets and "s-old" in targets
    assert "s-live" not in targets, "active session must not compress"
    assert targets[0] == "s-new", "newest first"


def test_run_compresses_and_is_idempotent(tmp_path, monkeypatch):
    db = _mkdb(tmp_path)
    monkeypatch.setattr(
        cs,
        "_draft",
        lambda prompt: {
            "summary": "dense semantic summary of the build",
            "topics": ["build_thing"],
        },
    )
    s1 = cs.run(db_path=db, cap=10)
    assert s1["compressed"] == 2
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT summary_text, compressed_at FROM session_summaries WHERE session_id='s-new'"
    ).fetchone()
    assert "dense semantic" in row[0]
    assert row[1] is not None

    s2 = cs.run(db_path=db, cap=10)
    assert s2["compressed"] == 0, "second run must be a no-op"


def test_run_respects_cap_and_survives_draft_failure(tmp_path, monkeypatch):
    db = _mkdb(tmp_path)
    monkeypatch.setattr(cs, "_draft", lambda prompt: None)  # provider down
    s = cs.run(db_path=db, cap=1)
    assert s["compressed"] == 0
    assert s["skipped_draft_failed"] == 1
    assert s["considered"] == 1, "cap not respected"


def test_prompt_includes_observations(tmp_path):
    db = _mkdb(tmp_path)
    cs.ensure_schema(db)
    prompt = cs.build_prompt("s-new", db_path=db)
    assert "build_thing" in prompt
    assert "Bash" in prompt


def test_compressed_summary_lands_in_fts(tmp_path, monkeypatch):
    db = _mkdb(tmp_path)
    monkeypatch.setattr(
        cs,
        "_draft",
        lambda prompt: {
            "summary": "wired the flux capacitor to the spine",
            "topics": ["flux"],
        },
    )
    cs.run(db_path=db, cap=10)
    conn = sqlite3.connect(db)
    hits = conn.execute(
        "SELECT session_id FROM summaries_fts WHERE summaries_fts MATCH 'flux'"
    ).fetchall()
    assert hits, "compressed summary not searchable via FTS"
