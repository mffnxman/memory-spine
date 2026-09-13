"""TDD: reconsolidation — hot-but-stale memories get refreshed with new evidence.

The most-recalled memories (650+ recalls) are also the stalest (untouched
since May). Biology reconsolidates memories on heavy retrieval; we do it at
sleep time: memories with high access counts and old mtimes get re-synthesized
with evidence that accumulated since (epilogue mentions), capped per cycle.

Safety: auto-rewrite ONLY for project/reference types. self/user/feedback are
identity — they surface for human-gated refresh, never machine-rewritten.
"""

import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reconsolidate as rc

DAY = 86400


def _mkdb(tmp_path, access_rows):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE access (filename TEXT, ts INTEGER, session_id TEXT, source TEXT)"
    )
    conn.executemany("INSERT INTO access VALUES (?,?,'','prefetch')", access_rows)
    conn.commit()
    conn.close()
    return db


def _mem(tmp_path, fn, mem_type="project", age_days=90):
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir(exist_ok=True)
    p = mem_dir / fn
    p.write_text(
        "---\nname: {}\ndescription: d\ntype: {}\n---\nold body\n".format(fn, mem_type),
        encoding="utf-8",
    )
    old = time.time() - age_days * DAY
    import os

    os.utime(p, (old, old))
    return p


def test_select_targets_hot_and_stale_only(tmp_path):
    now = int(time.time())
    db = _mkdb(
        tmp_path,
        [("hot_stale.md", now - i) for i in range(30)]  # 30 recalls
        + [("cold_stale.md", now)] * 2  # 2 recalls
        + [("hot_fresh.md", now - i) for i in range(30)],
    )
    mem = tmp_path / "memory"
    _mem(tmp_path, "hot_stale.md", age_days=90)
    _mem(tmp_path, "cold_stale.md", age_days=90)
    _mem(tmp_path, "hot_fresh.md", age_days=1)

    targets = rc.select_targets(memory_dir=mem, db_path=db, hot_min=20, stale_days=45)
    names = [t["filename"] for t in targets]
    assert "hot_stale.md" in names
    assert "cold_stale.md" not in names, "cold memory selected"
    assert "hot_fresh.md" not in names, "fresh memory selected"


def test_select_separates_identity_from_auto(tmp_path):
    now = int(time.time())
    db = _mkdb(
        tmp_path,
        [("proj.md", now - i) for i in range(30)]
        + [("self_core.md", now - i) for i in range(30)],
    )
    mem = tmp_path / "memory"
    _mem(tmp_path, "proj.md", mem_type="project", age_days=90)
    _mem(tmp_path, "self_core.md", mem_type="self", age_days=90)

    targets = rc.select_targets(memory_dir=mem, db_path=db, hot_min=20, stale_days=45)
    by_name = {t["filename"]: t for t in targets}
    assert by_name["proj.md"]["auto"] is True
    assert by_name["self_core.md"]["auto"] is False, "identity memory marked auto!"


def test_gather_evidence_finds_mentions_since_mtime(tmp_path):
    epi = tmp_path / "epilogues"
    epi.mkdir()
    (epi / "2026-07-01-1200.md").write_text(
        "we improved proj today, proj.md carried the build", encoding="utf-8"
    )
    (epi / "2026-01-01-1200.md").write_text(
        "ancient proj mention in proj.md", encoding="utf-8"
    )
    since = time.mktime(time.strptime("2026-06-01", "%Y-%m-%d"))
    ev = rc.gather_evidence("proj.md", epilogue_dir=epi, since_ts=since)
    assert len(ev) == 1, "only mentions NEWER than the memory should count"
    assert "improved proj" in ev[0]["excerpt"]


def test_run_rewrites_auto_target_with_provenance(tmp_path, monkeypatch):
    now = int(time.time())
    db = _mkdb(tmp_path, [("proj.md", now - i) for i in range(30)])
    mem = tmp_path / "memory"
    p = _mem(tmp_path, "proj.md", mem_type="project", age_days=90)
    epi = tmp_path / "epilogues"
    epi.mkdir()
    (epi / "2026-07-01-1200.md").write_text(
        "proj.md gained a new module", encoding="utf-8"
    )

    snapshots = []
    monkeypatch.setattr(rc, "_snapshot", lambda path: snapshots.append(str(path)))
    monkeypatch.setattr(rc, "_emit_reindex", lambda path: None)
    monkeypatch.setattr(
        rc,
        "_draft",
        lambda memory_text, evidence: {"body": "refreshed body with new module"},
    )

    summary = rc.run(
        memory_dir=mem,
        db_path=db,
        epilogue_dir=epi,
        hot_min=20,
        stale_days=45,
        cap=2,
    )
    assert summary["rewritten"] == 1
    text = p.read_text(encoding="utf-8")
    assert "refreshed body" in text
    assert "reconsolidated_at:" in text
    assert snapshots, "no provenance snapshot before rewrite!"


def test_run_never_rewrites_identity(tmp_path, monkeypatch):
    now = int(time.time())
    db = _mkdb(tmp_path, [("self_core.md", now - i) for i in range(30)])
    mem = tmp_path / "memory"
    p = _mem(tmp_path, "self_core.md", mem_type="self", age_days=90)
    epi = tmp_path / "epilogues"
    epi.mkdir()
    (epi / "2026-07-01-1200.md").write_text("self_core.md matters", encoding="utf-8")

    monkeypatch.setattr(rc, "_snapshot", lambda path: None)
    monkeypatch.setattr(rc, "_emit_reindex", lambda path: None)
    monkeypatch.setattr(rc, "_draft", lambda m, e: {"body": "MACHINE REWRITE"})

    summary = rc.run(
        memory_dir=mem,
        db_path=db,
        epilogue_dir=epi,
        hot_min=20,
        stale_days=45,
        cap=2,
    )
    assert summary["rewritten"] == 0
    assert summary["identity_flagged"] == 1
    assert "MACHINE REWRITE" not in p.read_text(encoding="utf-8")


def test_run_respects_cap_and_skips_no_evidence(tmp_path, monkeypatch):
    now = int(time.time())
    rows = []
    for fn in ("p1.md", "p2.md", "p3.md"):
        rows += [(fn, now - i) for i in range(30)]
    db = _mkdb(tmp_path, rows)
    mem = tmp_path / "memory"
    for fn in ("p1.md", "p2.md", "p3.md"):
        _mem(tmp_path, fn, age_days=90)
    epi = tmp_path / "epilogues"
    epi.mkdir()
    (epi / "2026-07-01-1200.md").write_text(
        "p1.md and p2.md and p3.md", encoding="utf-8"
    )

    monkeypatch.setattr(rc, "_snapshot", lambda path: None)
    monkeypatch.setattr(rc, "_emit_reindex", lambda path: None)
    monkeypatch.setattr(rc, "_draft", lambda m, e: {"body": "x"})

    summary = rc.run(
        memory_dir=mem,
        db_path=db,
        epilogue_dir=epi,
        hot_min=20,
        stale_days=45,
        cap=2,
    )
    assert summary["rewritten"] == 2, "cap not respected"

    # no-evidence case: fresh dir with no epilogue mentions
    db2 = _mkdb(tmp_path / "x", [("p9.md", now - i) for i in range(30)])
    mem2 = (tmp_path / "x") / "memory"
    mem2.mkdir(parents=True, exist_ok=True)
    p9 = mem2 / "p9.md"
    p9.write_text(
        "---\nname: p9\ndescription: d\ntype: project\n---\nb\n", encoding="utf-8"
    )
    import os

    old = time.time() - 90 * DAY
    os.utime(p9, (old, old))
    epi2 = (tmp_path / "x") / "epi"
    epi2.mkdir()
    s2 = rc.run(
        memory_dir=mem2,
        db_path=db2,
        epilogue_dir=epi2,
        hot_min=20,
        stale_days=45,
        cap=2,
    )
    assert s2["rewritten"] == 0
    assert s2["skipped_no_evidence"] == 1
