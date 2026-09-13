"""TDD: live conflict recording — the conflicts table has had 0 rows EVER.

The near-dup pass finds high-similarity memory pairs but only records
corroborations; genuine tension pairs never reach the conflicts table, so
nothing ever surfaces for resolution. conflict_recorder gives the table a
heartbeat: record (deduped, pair-normalized), list unresolved, resolve, and
surface a boot line when unresolved conflicts exist.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import conflict_recorder as cr


def _mkdb(tmp_path):
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE conflicts (
               ts INTEGER, new_file TEXT, existing_file TEXT,
               similarity REAL, note TEXT, resolved INTEGER DEFAULT 0)""")
    conn.commit()
    conn.close()
    return db


def test_record_pair_inserts_row(tmp_path):
    db = _mkdb(tmp_path)
    added = cr.record_near_dups([("b.md", "a.md", 0.95)], db_path=db)
    assert added["recorded"] == 1
    rows = list(
        sqlite3.connect(db).execute(
            "SELECT new_file, existing_file, similarity, resolved FROM conflicts"
        )
    )
    assert rows == [("a.md", "b.md", 0.95, 0)], "pair must be normalized a<b"


def test_record_pair_dedupes_unresolved(tmp_path):
    db = _mkdb(tmp_path)
    cr.record_near_dups([("a.md", "b.md", 0.95)], db_path=db)
    added = cr.record_near_dups([("b.md", "a.md", 0.97)], db_path=db)
    assert added["recorded"] == 0, "unresolved pair must not duplicate"
    n = sqlite3.connect(db).execute("SELECT COUNT(*) FROM conflicts").fetchone()[0]
    assert n == 1


def test_resolved_pair_can_recur(tmp_path):
    db = _mkdb(tmp_path)
    cr.record_near_dups([("a.md", "b.md", 0.95)], db_path=db)
    cr.resolve("a.md", "b.md", note="merged", db_path=db)
    added = cr.record_near_dups([("a.md", "b.md", 0.96)], db_path=db)
    assert added["recorded"] == 1, "re-emerged conflict after resolution must record"


def test_unresolved_listing(tmp_path):
    db = _mkdb(tmp_path)
    cr.record_near_dups([("a.md", "b.md", 0.95), ("c.md", "d.md", 0.93)], db_path=db)
    cr.resolve("a.md", "b.md", note="ok", db_path=db)
    un = cr.unresolved(db_path=db)
    assert len(un) == 1
    assert un[0]["new_file"] == "c.md"


def test_boot_lines_surface_and_stay_silent(tmp_path):
    db = _mkdb(tmp_path)
    assert cr.boot_lines(db_path=db) == [], "no conflicts -> silent boot"
    cr.record_near_dups([("a.md", "b.md", 0.95)], db_path=db)
    lines = cr.boot_lines(db_path=db)
    assert lines and "1" in lines[0], "unresolved count must surface at boot"
