"""TDD: Hebbian co-recall edges — fire together, wire together.

The access log records which memories were injected/recalled in the same
prefetch batch (same exact ts). Co-fired memories get weighted synapses in a
co_recall table: raw count + exponentially decayed weight (recent co-firing
counts more — synaptic decay), pruned below a floor (synaptic pruning).
`spread(seeds)` is the retrieval API: given the current top hits, return
their strongest historical co-firing neighbors.
"""

import math
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hebbian

DAY = 86400


def _mkdb(tmp_path, rows):
    """rows: list of (filename, ts) — session_id/source irrelevant to mining."""
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE access (filename TEXT, ts INTEGER, session_id TEXT, source TEXT)"
    )
    conn.executemany(
        "INSERT INTO access VALUES (?,?,'','prefetch')", [(f, t) for f, t in rows]
    )
    conn.commit()
    conn.close()
    return db


def test_rebuild_counts_batch_pairs(tmp_path):
    now = int(time.time())
    db = _mkdb(
        tmp_path,
        [
            ("a.md", now - DAY),
            ("b.md", now - DAY),
            ("c.md", now - DAY),  # batch 1
            ("a.md", now),
            ("b.md", now),  # batch 2
        ],
    )
    summary = hebbian.rebuild(db_path=db, now=now)
    assert summary["batches"] == 2
    conn = sqlite3.connect(db)
    w = {(a, b): c for a, b, c in conn.execute("SELECT a, b, weight FROM co_recall")}
    assert w[("a.md", "b.md")] == 2, "a-b co-fired in both batches"
    assert w[("a.md", "c.md")] == 1
    assert w[("b.md", "c.md")] == 1
    assert ("b.md", "a.md") not in w, "pairs must be normalized a<b"


def test_rebuild_decay_recent_outweighs_old(tmp_path):
    now = int(time.time())
    db = _mkdb(
        tmp_path,
        [
            ("old1.md", now - 300 * DAY),
            ("old2.md", now - 300 * DAY),
            ("new1.md", now - DAY),
            ("new2.md", now - DAY),
        ],
    )
    hebbian.rebuild(db_path=db, now=now, prune_below=0.0)
    conn = sqlite3.connect(db)
    d = {(a, b): x for a, b, x in conn.execute("SELECT a, b, decayed FROM co_recall")}
    assert d[("new1.md", "new2.md")] > d[("old1.md", "old2.md")]
    # decay curve sanity: one-day-old batch is worth nearly a full unit
    assert 0.9 < d[("new1.md", "new2.md")] <= 1.0
    expected_old = math.exp(-math.log(2) * 300 / hebbian.HALF_LIFE_DAYS)
    assert abs(d[("old1.md", "old2.md")] - expected_old) < 1e-6


def test_rebuild_prunes_below_threshold(tmp_path):
    now = int(time.time())
    db = _mkdb(
        tmp_path,
        [
            ("old1.md", now - 400 * DAY),
            ("old2.md", now - 400 * DAY),  # decayed ~0.002
            ("new1.md", now - DAY),
            ("new2.md", now - DAY),
        ],
    )
    summary = hebbian.rebuild(db_path=db, now=now)  # default prune floor
    conn = sqlite3.connect(db)
    pairs = list(conn.execute("SELECT a, b FROM co_recall"))
    assert ("new1.md", "new2.md") in pairs
    assert (
        "old1.md",
        "old2.md",
    ) not in pairs, "ancient unreinforced synapse must prune"
    assert summary["pruned"] == 1


def test_rebuild_is_idempotent(tmp_path):
    now = int(time.time())
    db = _mkdb(tmp_path, [("a.md", now), ("b.md", now)])
    hebbian.rebuild(db_path=db, now=now)
    hebbian.rebuild(db_path=db, now=now)
    conn = sqlite3.connect(db)
    rows = list(conn.execute("SELECT a, b, weight FROM co_recall"))
    assert rows == [("a.md", "b.md", 1)], "double rebuild must not double-count"


def test_neighbors_sorted_and_excludes_self(tmp_path):
    now = int(time.time())
    db = _mkdb(
        tmp_path,
        [
            ("hub.md", now),
            ("strong.md", now),
            ("hub.md", now - 10),
            ("strong.md", now - 10),
            ("hub.md", now - 20),
            ("weak.md", now - 20),
        ],
    )
    hebbian.rebuild(db_path=db, now=now)
    ns = hebbian.neighbors("hub.md", db_path=db)
    names = [n for n, _ in ns]
    assert names[0] == "strong.md"
    assert "hub.md" not in names
    assert ns[0][1] > ns[1][1]


def test_spread_aggregates_over_seeds(tmp_path):
    now = int(time.time())
    db = _mkdb(
        tmp_path,
        [
            ("s1.md", now),
            ("shared.md", now),
            ("s2.md", now - 10),
            ("shared.md", now - 10),
            ("s1.md", now - 20),
            ("solo.md", now - 20),
        ],
    )
    hebbian.rebuild(db_path=db, now=now)
    hits = hebbian.spread(["s1.md", "s2.md"], db_path=db)
    names = [n for n, _ in hits]
    assert "s1.md" not in names and "s2.md" not in names, "seeds excluded"
    assert (
        names[0] == "shared.md"
    ), "neighbor of BOTH seeds must outrank single-seed neighbor"
    assert "solo.md" in names
