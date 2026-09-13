"""hebbian.py — co-recall synapses over the access log (v15, 2026-07-09).

Fire together, wire together: the `access` table in memory.db records which
memories were injected/recalled in the same prefetch batch (same exact ts).
Each batch is a co-activation event. This module mines those events into a
`co_recall` edge table:

    co_recall(a, b, weight, decayed, last_fired)
      a < b            normalized pair
      weight           raw co-firing count (all history)
      decayed          sum over batches of exp(-ln2 * age_days / HALF_LIFE_DAYS)
                       — recent co-firing counts more (synaptic decay)
      last_fired       ts of most recent co-firing

Pairs whose decayed weight falls below PRUNE_BELOW are dropped (synaptic
pruning): an ancient one-off co-recall that was never reinforced disappears;
a heavily reinforced pair survives indefinitely.

`rebuild()` is a deterministic full re-mine (idempotent — safe to run every
consolidate cycle). `spread(seeds)` is the retrieval API: given the current
top-ranked memories, return their strongest historical co-firing neighbors —
spreading activation, fused into search_hybrid as a 5th RRF signal behind the
`hebbian_enabled` flag.

CLI:
    python hebbian.py rebuild
    python hebbian.py top [n]
    python hebbian.py neighbors <filename>
"""

from __future__ import annotations

import json
import math
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import paths as _paths  # noqa: E402

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

MEMORY_DIR = _paths.MEMORY_DIR
DEFAULT_DB = MEMORY_DIR / "_meta" / "memory.db"

HALF_LIFE_DAYS = 45.0  # co-firing older than ~6 half-lives contributes ~0
PRUNE_BELOW = 0.05  # decayed weight floor — below this the synapse prunes
DAY = 86400.0


def _conn(db_path=None) -> sqlite3.Connection:
    return sqlite3.connect(str(db_path or DEFAULT_DB), timeout=5.0)


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS co_recall (
               a TEXT NOT NULL,
               b TEXT NOT NULL,
               weight INTEGER NOT NULL,
               decayed REAL NOT NULL,
               last_fired INTEGER NOT NULL,
               PRIMARY KEY (a, b)
           )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_co_recall_b ON co_recall(b)")


def rebuild(db_path=None, now=None, prune_below=PRUNE_BELOW, valid_files=None) -> dict:
    """Deterministic full re-mine of co_recall from the access log.

    valid_files: optional set of filenames still on disk — access history for
    vanished memories is skipped so no synapse points into the void. Pass
    live_files() in production; None (tests) skips the filter."""
    started = time.time()
    now = now or time.time()
    with _conn(db_path) as conn:
        _ensure_table(conn)
        batches = defaultdict(set)
        for fn, ts in conn.execute("SELECT filename, ts FROM access"):
            if valid_files is not None and fn not in valid_files:
                continue  # dead synapse: memory no longer on disk
            batches[ts].add(fn)

        weight = defaultdict(int)
        decayed = defaultdict(float)
        last_fired = defaultdict(int)
        for ts, files in batches.items():
            if len(files) < 2:
                continue
            age_days = max(0.0, (now - ts) / DAY)
            contrib = math.exp(-math.log(2) * age_days / HALF_LIFE_DAYS)
            ordered = sorted(files)
            for i, a in enumerate(ordered):
                for b in ordered[i + 1 :]:
                    weight[(a, b)] += 1
                    decayed[(a, b)] += contrib
                    last_fired[(a, b)] = max(last_fired[(a, b)], int(ts))

        pruned = sum(1 for pair, d in decayed.items() if d < prune_below)
        conn.execute("DELETE FROM co_recall")
        conn.executemany(
            "INSERT INTO co_recall (a, b, weight, decayed, last_fired) VALUES (?,?,?,?,?)",
            [
                (a, b, weight[(a, b)], round(d, 6), last_fired[(a, b)])
                for (a, b), d in decayed.items()
                if d >= prune_below
            ],
        )
        conn.commit()
        kept = conn.execute("SELECT COUNT(*) FROM co_recall").fetchone()[0]

    return {
        "batches": sum(1 for files in batches.values() if len(files) >= 2),
        "pairs": kept,
        "pruned": pruned,
        "duration_sec": round(time.time() - started, 2),
    }


def live_files():
    """Filenames currently on disk in the memory root."""
    return {p.name for p in MEMORY_DIR.glob("*.md")}


def neighbors(filename, db_path=None, limit=8, min_decayed=0.0):
    """Strongest co-firing partners of one memory, sorted by decayed weight."""
    with _conn(db_path) as conn:
        _ensure_table(conn)
        rows = conn.execute(
            """SELECT CASE WHEN a = ? THEN b ELSE a END AS other, decayed
               FROM co_recall WHERE (a = ? OR b = ?) AND decayed >= ?
               ORDER BY decayed DESC LIMIT ?""",
            (filename, filename, filename, min_decayed, limit),
        ).fetchall()
    return [(fn, d) for fn, d in rows]


def spread(seed_files, db_path=None, limit=10, per_seed=8):
    """Spreading activation: aggregate co-recall neighbors over seed memories.

    score(n) = sum over seeds of decayed(seed, n). Seeds themselves excluded.
    Returns [(filename, score)] sorted desc, capped at `limit`."""
    seeds = set(seed_files)
    scores = defaultdict(float)
    for seed in seed_files:
        for fn, d in neighbors(seed, db_path=db_path, limit=per_seed):
            if fn not in seeds:
                scores[fn] += d
    return sorted(scores.items(), key=lambda kv: -kv[1])[:limit]


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "rebuild"
    if cmd == "rebuild":
        print(json.dumps(rebuild(valid_files=live_files()), indent=2))
    elif cmd == "top":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 15
        with _conn() as conn:
            _ensure_table(conn)
            for a, b, w, d in conn.execute(
                "SELECT a, b, weight, decayed FROM co_recall ORDER BY decayed DESC LIMIT ?",
                (n,),
            ):
                print(f"  {d:8.2f}  (x{w:<4d}) {a} <-> {b}")
    elif cmd == "neighbors":
        if len(sys.argv) < 3:
            print("usage: python hebbian.py neighbors <filename>")
            return
        for fn, d in neighbors(sys.argv[2]):
            print(f"  {d:8.2f}  {fn}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
