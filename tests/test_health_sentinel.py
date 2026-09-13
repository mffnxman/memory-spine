"""TDD: health sentinel — the brain checks its own pulse daily.

Bus-factor-of-one failure mode: the promotion pipeline sat DEAD for weeks and
nobody noticed, because the only maintainer is us. The sentinel runs cheap
data-flow invariants (is anything actually moving?) daily + at boot, writes
_meta/health_status.json, and surfaces failures as boot lines. Silent when
healthy — attention is a budget.
"""

import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import health_sentinel as hs

DAY = 86400


def _obs_db(tmp_path, last_obs_age_h):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "observations.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE observations (id INTEGER PRIMARY KEY, ts INTEGER)")
    conn.execute(
        "INSERT INTO observations (ts) VALUES (?)",
        (int(time.time() - last_obs_age_h * 3600),),
    )
    conn.commit()
    conn.close()
    return db


def test_observations_flowing_ok_and_stale(tmp_path):
    ok = hs.check_observations_flowing(_obs_db(tmp_path, last_obs_age_h=2))
    assert ok["ok"] is True
    stale = hs.check_observations_flowing(_obs_db(tmp_path / "b", last_obs_age_h=100))
    assert stale["ok"] is False
    assert "h" in stale["detail"] or "stale" in stale["detail"].lower()


def test_outbox_backlog_counts_last_status_wins(tmp_path):
    jobs = tmp_path / "jobs.jsonl"
    rows = [
        {"event_id": "a", "status": "pending"},
        {"event_id": "a", "status": "done"},  # a resolved
        {"event_id": "b", "status": "pending"},  # b still pending
        {"event_id": "c", "status": "failed", "attempts": 3},  # dead
    ]
    jobs.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    res = hs.check_outbox_backlog(jobs, max_pending=5)
    assert res["ok"] is False, "dead-lettered job must trip the check"
    assert "1" in res["detail"]

    rows2 = [{"event_id": "x", "status": "pending"}]
    jobs2 = tmp_path / "jobs2.jsonl"
    jobs2.write_text("\n".join(json.dumps(r) for r in rows2), encoding="utf-8")
    assert hs.check_outbox_backlog(jobs2, max_pending=5)["ok"] is True


def test_consolidate_freshness(tmp_path):
    marker = tmp_path / ".last_worker_consolidate"
    marker.write_text("x")
    assert hs.check_consolidate_fresh(marker)["ok"] is True
    import os

    old = time.time() - 12 * DAY
    os.utime(marker, (old, old))
    assert hs.check_consolidate_fresh(marker, max_age_days=9)["ok"] is False
    assert (
        hs.check_consolidate_fresh(tmp_path / "missing", max_age_days=9)["ok"] is False
    )


def test_candidate_backlog_gate(tmp_path):
    cand = tmp_path / "cands"
    cand.mkdir()
    for i in range(3):
        (cand / f"c{i}.md").write_text("x", encoding="utf-8")
    assert hs.check_candidate_backlog(cand, max_pending=60)["ok"] is True
    assert hs.check_candidate_backlog(cand, max_pending=2)["ok"] is False


def test_run_all_writes_status_and_boot_lines(tmp_path, monkeypatch):
    status_path = tmp_path / "health_status.json"
    monkeypatch.setattr(hs, "STATUS_PATH", status_path)
    fake_checks = [
        {"name": "good", "ok": True, "detail": "fine"},
        {"name": "bad", "ok": False, "detail": "broken thing"},
    ]
    monkeypatch.setattr(hs, "_all_checks", lambda: fake_checks)

    status = hs.run_all(write=True)
    assert status["ok"] is False
    assert status_path.exists()
    saved = json.loads(status_path.read_text(encoding="utf-8"))
    assert saved["warnings"] == ["bad: broken thing"]

    lines = hs.boot_lines(max_status_age_h=26)
    assert lines and "broken thing" in lines[0]


def test_boot_lines_silent_when_healthy(tmp_path, monkeypatch):
    status_path = tmp_path / "health_status.json"
    monkeypatch.setattr(hs, "STATUS_PATH", status_path)
    monkeypatch.setattr(
        hs, "_all_checks", lambda: [{"name": "g", "ok": True, "detail": ""}]
    )
    hs.run_all(write=True)
    assert hs.boot_lines(max_status_age_h=26) == []


def test_boot_lines_rerun_when_status_stale(tmp_path, monkeypatch):
    status_path = tmp_path / "health_status.json"
    monkeypatch.setattr(hs, "STATUS_PATH", status_path)
    calls = []

    def fake_checks():
        calls.append(1)
        return [{"name": "g", "ok": True, "detail": ""}]

    monkeypatch.setattr(hs, "_all_checks", fake_checks)
    hs.run_all(write=True)
    import os

    old = time.time() - 30 * 3600
    os.utime(status_path, (old, old))
    hs.boot_lines(max_status_age_h=26)
    assert len(calls) >= 2, "stale status file must trigger a live re-check"


# -- dart lane (BIGBUFF 3, 2026-08-13 squeeze autopsy) -----------------------
# The green-looking dead lane: dartd exits, the dart2-daemon task records the
# exit code, and nothing else ever says a word. The sentinel closes the loop
# per the silent-failure-classes doctrine: teach the sentinel every signature.


def _tasks(daemon_state="Ready", daemon_result=0, watchdog_state="Ready"):
    def q(name):
        if name == "dart2-daemon":
            return {"state": daemon_state, "last_result": daemon_result}
        if name == "dart2-watchdog":
            return None if watchdog_state is None else {"state": watchdog_state}
        return None

    return q


def test_dart_lane_up_and_guarded_is_ok():
    res = hs.check_dart_lane(port_open=lambda: True, task_query=_tasks())
    assert res["ok"] is True
    assert "up" in res["detail"]


def test_dart_lane_up_but_watchdog_missing_warns():
    res = hs.check_dart_lane(
        port_open=lambda: True, task_query=_tasks(watchdog_state=None)
    )
    assert res["ok"] is False
    assert "watchdog" in res["detail"].lower()


def test_dart_lane_down_deliberate_is_ok():
    """Exit 0 = /admin/shutdown (window close): the user chose down — the
    7/27 no-logon-trigger call. Not a warning."""
    res = hs.check_dart_lane(port_open=lambda: False, task_query=_tasks())
    assert res["ok"] is True
    assert "deliberate" in res["detail"]


def test_dart_lane_down_crashed_warns():
    res = hs.check_dart_lane(
        port_open=lambda: False, task_query=_tasks(daemon_result=1)
    )
    assert res["ok"] is False
    assert "crash" in res["detail"].lower()
    assert "watchdog" in res["detail"].lower()  # points at the revival path


def test_dart_lane_loading_is_ok():
    """daemon_loop Running with the port not yet listening = mid-load or an
    exit-42 bounce, not an outage."""
    res = hs.check_dart_lane(
        port_open=lambda: False, task_query=_tasks(daemon_state="Running")
    )
    assert res["ok"] is True


def test_dart_lane_daemon_task_missing_warns():
    res = hs.check_dart_lane(port_open=lambda: False, task_query=lambda n: None)
    assert res["ok"] is False
    assert "missing" in res["detail"].lower()
