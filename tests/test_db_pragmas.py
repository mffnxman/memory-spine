"""TDD: memory.db should use WAL + busy_timeout like observations.db already does.

memory.db opened with sqlite defaults (busy_timeout=0 -> instant 'database is
locked'). The KG write hook, PPR/boot reads, and consolidation run in
overlapping processes; on contention a memory's entities/edges get silently
dropped = partial graph = partial recall. Mirror observer_lib.py:74-75.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import memory_engine as me
import kg


def test_memory_db_has_busy_timeout_and_wal():
    with me.db() as c:
        bt = c.execute("PRAGMA busy_timeout").fetchone()[0]
        jm = c.execute("PRAGMA journal_mode").fetchone()[0]
    assert bt >= 5000
    assert jm.lower() == "wal"


def test_kg_db_inherits_busy_timeout():
    with kg.db() as c:
        bt = c.execute("PRAGMA busy_timeout").fetchone()[0]
    assert bt >= 5000
