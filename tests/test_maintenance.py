"""TDD: automated maintenance pass — the weekly upkeep tax goes to zero-touch.

The observer plan's phase 6 ('~30 min/week manual') never became a habit.
This pass does it in the sleep cycle: vacuum/analyze the dbs, mark orphaned
active sessions abandoned, and run the floor-benchmark diff so retrieval rot
is detected weekly instead of whenever someone remembers to check.
"""

import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import maintenance as mt

DAY = 86400


def _obs_db(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "observations.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sessions (session_id TEXT PRIMARY KEY, started_at INTEGER, "
        "ended_at INTEGER, cwd TEXT, prompt_count INTEGER, obs_count INTEGER, status TEXT)"
    )
    now = int(time.time())
    conn.executemany(
        "INSERT INTO sessions VALUES (?,?,NULL,'x',0,5,?)",
        [
            ("fresh-active", now - 3600, "active"),  # legit live session
            ("orphan-active", now - 3 * DAY, "active"),  # crashed, never ended
            ("done", now - 5 * DAY, "ended"),
        ],
    )
    conn.commit()
    conn.close()
    return db


def test_mark_abandoned_sessions(tmp_path):
    db = _obs_db(tmp_path)
    res = mt.mark_abandoned(db_path=db, max_active_hours=24)
    assert res["abandoned"] == 1
    conn = sqlite3.connect(db)
    status = dict(conn.execute("SELECT session_id, status FROM sessions"))
    assert status["orphan-active"] == "abandoned"
    assert status["fresh-active"] == "active", "live session must not be touched"
    assert status["done"] == "ended"


def test_vacuum_databases(tmp_path):
    db = _obs_db(tmp_path)
    res = mt.vacuum_dbs(paths=[db])
    assert res["vacuumed"] == 1
    assert not res.get("errors")


def test_floor_check_writes_result(tmp_path, monkeypatch):
    out = tmp_path / "floor_last.json"
    monkeypatch.setattr(mt, "FLOOR_LAST_PATH", out)
    monkeypatch.setattr(
        mt,
        "_run_floor_suite",
        lambda: {
            "n": 117,
            "passed": 116,
            "mrr": 0.94,
            "failed_ids": ["floor_plan_bigbuff"],
        },
    )
    monkeypatch.setattr(
        mt,
        "_load_floor_baseline",
        lambda: {"failed_ids": ["floor_plan_bigbuff"]},
    )
    res = mt.floor_check()
    assert res["regressions"] == []
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["passed"] == 116


def test_floor_check_flags_regressions(tmp_path, monkeypatch):
    out = tmp_path / "floor_last.json"
    monkeypatch.setattr(mt, "FLOOR_LAST_PATH", out)
    monkeypatch.setattr(
        mt,
        "_run_floor_suite",
        lambda: {
            "n": 117,
            "passed": 114,
            "mrr": 0.90,
            "failed_ids": ["floor_plan_bigbuff", "floor_x", "floor_y"],
        },
    )
    monkeypatch.setattr(
        mt,
        "_load_floor_baseline",
        lambda: {"failed_ids": ["floor_plan_bigbuff"]},
    )
    res = mt.floor_check()
    assert sorted(res["regressions"]) == ["floor_x", "floor_y"]
    saved = json.loads(out.read_text(encoding="utf-8"))
    assert saved["regressions"] == sorted(res["regressions"])
