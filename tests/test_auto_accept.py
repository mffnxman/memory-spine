"""TDD: observer candidates must be able to GRADUATE automatically.

Root cause found 2026-07-09: generation works (73 pending candidates) but the
only graduation lane was a manual CLI (--accept) never run once, and the
auto_promote_high_confidence_enabled flag shipped OFF for a 4-week trust
window nobody closed. The user explicitly authorized closing it ("make it so it
auto promotes"). These tests specify the auto-accept lane:

  - gates: flag ON, cluster_score >= min, candidate age >= min hours,
    source observations still unresolved
  - graduation: memory file in spine FORMAT (flat name/description/type/weight
    frontmatter, not the nested metadata block), observations bookkeeping
    updated, candidate archived to accepted/, reindex event emitted per file
  - safety: filename collisions uniquify instead of aborting
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import promotion_candidate_generator as pcg
from observer_lib import ensure_db, get_connection


def _fake_rows(ids, ts, refs=4):
    return [
        {
            "id": i,
            "session_id": "sess-1234-abcd",
            "ts": ts,
            "tool_name": "Bash",
            "cmd_excerpt": "python build_car_strike.py --wave 3",
            "tool_input_excerpt": "",
            "tool_output_excerpt": "",
            "file_paths": json.dumps(["C:\\x\\build_car_strike.py"]),
            "referenced_count": refs,
            "last_referenced_at": ts,
        }
        for i in ids
    ]


def _env(monkeypatch, tmp_path, flag=True):
    """Isolated candidates dir, memory dir, observations db. Returns db path."""
    cand = tmp_path / "candidates"
    monkeypatch.setattr(pcg, "CANDIDATES_DIR", cand)
    monkeypatch.setattr(pcg, "AUTO_DIR", cand / "auto")
    monkeypatch.setattr(pcg, "ACCEPTED_DIR", cand / "accepted")
    monkeypatch.setattr(pcg, "REJECTED_DIR", cand / "rejected")
    monkeypatch.setattr(pcg, "MEMORY_DIR", tmp_path / "memory")
    (tmp_path / "memory").mkdir(exist_ok=True)

    db = tmp_path / "obs.db"
    ensure_db(db)
    monkeypatch.setattr(pcg, "get_connection", lambda: get_connection(db))
    monkeypatch.setattr(pcg, "ensure_db", lambda: None)
    monkeypatch.setattr(
        pcg,
        "_flag",
        lambda name, default: (
            flag if name == "auto_promote_high_confidence_enabled" else default
        ),
    )
    monkeypatch.setattr(pcg, "_emit_reindex", lambda path: None, raising=False)
    return db


def _seed(db, ids, ts):
    with get_connection(db) as conn:
        for r in _fake_rows(ids, ts):
            conn.execute(
                """INSERT INTO observations
                   (id, session_id, ts, tool_name, cmd_excerpt, file_paths, referenced_count)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    r["id"],
                    r["session_id"],
                    r["ts"],
                    r["tool_name"],
                    r["cmd_excerpt"],
                    r["file_paths"],
                    r["referenced_count"],
                ),
            )
        conn.commit()


def _make_candidate(db, ids=(1, 2, 3), age_hours=48.0):
    ts = int(time.time() - age_hours * 3600)
    _seed(db, ids, ts)
    path = pcg.write_candidate("sess-1234-abcd", _fake_rows(list(ids), ts))
    # write_candidate stamps generated_at=now; rewrite it to honor age_hours
    text = path.read_text(encoding="utf-8")
    text = text.replace(
        [l for l in text.splitlines() if "generated_at:" in l][0],
        "  generated_at: {}".format(ts),
    )
    path.write_text(text, encoding="utf-8")
    return path


def test_auto_accept_promotes_qualifying_candidate(monkeypatch, tmp_path):
    db = _env(monkeypatch, tmp_path)
    cand = _make_candidate(db)

    result = pcg.auto_accept()

    assert result["promoted"], "qualifying candidate was not promoted"
    target = pcg.MEMORY_DIR / result["promoted"][0]
    assert target.exists(), "promoted memory file missing from memory dir"

    fm = target.read_text(encoding="utf-8")
    for key in (
        "name:",
        "description:",
        "type:",
        "weight:",
        "origin: observer-promoted",
    ):
        assert key in fm, "spine frontmatter missing {}".format(key)
    assert "node_type:" not in fm, "nested metadata block leaked into spine file"

    with get_connection(db) as conn:
        vals = [
            r[0]
            for r in conn.execute(
                "SELECT promoted_to_memory_id FROM observations WHERE id IN (1,2,3)"
            )
        ]
    assert all(v == result["promoted"][0] for v in vals), "obs bookkeeping not updated"

    assert not cand.exists(), "candidate file not archived"
    assert (pcg.ACCEPTED_DIR / cand.name).exists(), "candidate not moved to accepted/"


def test_auto_accept_respects_flag_gate(monkeypatch, tmp_path):
    db = _env(monkeypatch, tmp_path, flag=False)
    _make_candidate(db)
    result = pcg.auto_accept()
    assert result.get("skipped_flag_off"), "flag OFF must short-circuit"
    assert not result.get("promoted"), "promoted despite flag OFF"


def test_auto_accept_respects_score_gate(monkeypatch, tmp_path):
    db = _env(monkeypatch, tmp_path)
    _make_candidate(db, ids=(1, 2), age_hours=48.0)  # 2 obs -> low cluster_score
    result = pcg.auto_accept(score_min=0.99)
    assert not result["promoted"]
    assert result["skipped"].get("score", 0) == 1
    assert list(pcg.CANDIDATES_DIR.glob("*.md")), "skipped candidate must stay pending"


def test_auto_accept_respects_age_gate(monkeypatch, tmp_path):
    db = _env(monkeypatch, tmp_path)
    _make_candidate(db, age_hours=1.0)  # too fresh
    result = pcg.auto_accept(age_min_hours=24)
    assert not result["promoted"]
    assert result["skipped"].get("age", 0) == 1


def test_auto_accept_skips_already_resolved_observations(monkeypatch, tmp_path):
    db = _env(monkeypatch, tmp_path)
    _make_candidate(db)
    with get_connection(db) as conn:
        conn.execute("UPDATE observations SET promoted_to_memory_id = 'rejected'")
        conn.commit()
    result = pcg.auto_accept()
    assert not result["promoted"]
    assert result["skipped"].get("obs_resolved", 0) == 1


def test_graduation_target_falls_back_to_file_on_junk_topic(monkeypatch, tmp_path):
    """TF-IDF topics like the project slug ('c--users-alex') or 'grep' are
    path/command noise: the promoted memory must take its name from the
    dominant FILE instead."""
    db = _env(monkeypatch, tmp_path)
    cand = _make_candidate(db)
    import re

    slug = pcg._paths.PROJECT_SLUG.lower()
    text = cand.read_text(encoding="utf-8")
    text = re.sub(r"name: auto-cluster: .+", f"name: auto-cluster: {slug}", text)
    text = re.sub(r"## Topic\n.+", f"## Topic\n{slug}, grep, env", text)
    cand.write_text(text, encoding="utf-8")

    meta = pcg.parse_candidate(cand)
    target = pcg.graduation_target(meta)
    assert slug not in target, "junk topic leaked into memory filename"
    assert "build_car_strike" in target, "dominant file not used as fallback name"

    result = pcg.auto_accept()
    assert result["promoted"] == [target]
    fm = (pcg.MEMORY_DIR / target).read_text(encoding="utf-8")
    assert "build_car_strike" in fm.splitlines()[1], "junk topic leaked into name:"


def test_auto_accept_uniquifies_on_collision(monkeypatch, tmp_path):
    db = _env(monkeypatch, tmp_path)
    cand = _make_candidate(db)
    # occupy the computed target name
    rec = pcg.graduation_target(pcg.parse_candidate(cand))
    (pcg.MEMORY_DIR / rec).write_text("occupied", encoding="utf-8")

    result = pcg.auto_accept()
    assert result["promoted"], "collision must uniquify, not abort"
    assert result["promoted"][0] != rec
    assert (pcg.MEMORY_DIR / result["promoted"][0]).exists()
    assert (pcg.MEMORY_DIR / rec).read_text(encoding="utf-8") == "occupied"


def test_auto_accept_emits_reindex_per_promotion(monkeypatch, tmp_path):
    db = _env(monkeypatch, tmp_path)
    _make_candidate(db)
    emitted = []
    monkeypatch.setattr(pcg, "_emit_reindex", lambda path: emitted.append(str(path)))
    result = pcg.auto_accept()
    assert len(emitted) == len(result["promoted"]) == 1
