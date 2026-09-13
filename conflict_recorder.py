"""conflict_recorder.py — give the conflicts table a heartbeat (v15, 2026-07-09).

The `conflicts` table (memory.db) shipped with the engine and had ZERO rows
ever written: the near-dup pass only recorded corroborations, so genuine
tension pairs (two memories similar enough to contradict or duplicate) never
surfaced anywhere. This module is the missing writer + reader:

  record_near_dups(pairs)   — insert pair-normalized, deduped-while-unresolved
  unresolved()              — list open conflicts
  resolve(a, b, note)       — mark a pair handled (it may recur later)
  boot_lines()              — one-liner for boot_ritual when conflicts exist

Recording is deliberately conservative: a pair with an OPEN conflict row is
never re-inserted (no spam across weekly cycles); once resolved, a re-emerged
conflict records fresh.

CLI:
    python conflict_recorder.py list
    python conflict_recorder.py resolve <file_a> <file_b> [note]
"""

from __future__ import annotations

import sqlite3
import sys
import time
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


def _conn(db_path=None) -> sqlite3.Connection:
    return sqlite3.connect(str(db_path or DEFAULT_DB), timeout=5.0)


def _norm(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def record_near_dups(pairs, db_path=None, note="near-dup (consolidate pass)", now=None):
    """pairs: iterable of (file_a, file_b, similarity). Returns summary."""
    now = now or int(time.time())
    summary = {"considered": 0, "recorded": 0, "skipped_open": 0}
    with _conn(db_path) as conn:
        for fa, fb, sim in pairs:
            summary["considered"] += 1
            a, b = _norm(fa, fb)
            open_row = conn.execute(
                "SELECT 1 FROM conflicts WHERE new_file=? AND existing_file=? AND resolved=0",
                (a, b),
            ).fetchone()
            if open_row:
                summary["skipped_open"] += 1
                continue
            conn.execute(
                "INSERT INTO conflicts (ts, new_file, existing_file, similarity, note, resolved) "
                "VALUES (?,?,?,?,?,0)",
                (now, a, b, round(float(sim), 4), note),
            )
            summary["recorded"] += 1
        conn.commit()
    return summary


def unresolved(db_path=None):
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT ts, new_file, existing_file, similarity, note FROM conflicts "
            "WHERE resolved=0 ORDER BY similarity DESC"
        ).fetchall()
    return [
        {
            "ts": ts,
            "new_file": nf,
            "existing_file": ef,
            "similarity": sim,
            "note": note,
        }
        for ts, nf, ef, sim, note in rows
    ]


def resolve(file_a, file_b, note="", db_path=None):
    a, b = _norm(file_a, file_b)
    with _conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE conflicts SET resolved=1, note=note || ' | resolved: ' || ? "
            "WHERE new_file=? AND existing_file=? AND resolved=0",
            (note or "manual", a, b),
        )
        conn.commit()
        return cur.rowcount


def boot_lines(db_path=None) -> list[str]:
    """One-liner for boot_ritual; silent when nothing is open."""
    try:
        open_conflicts = unresolved(db_path=db_path)
    except Exception:
        return []
    if not open_conflicts:
        return []
    worst = open_conflicts[0]
    return [
        f"  • {len(open_conflicts)} unresolved memory conflict(s) — worst: "
        f"{worst['new_file']} vs {worst['existing_file']} (sim {worst['similarity']:.2f}) — "
        f"python conflict_recorder.py list"
    ]


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        rows = unresolved()
        if not rows:
            print("no unresolved conflicts.")
            return
        for r in rows:
            print(
                f"  sim {r['similarity']:.3f}  {r['new_file']} vs {r['existing_file']}  ({r['note']})"
            )
        print(
            f"\nresolve: python conflict_recorder.py resolve <file_a> <file_b> [note]"
        )
    elif cmd == "resolve":
        if len(sys.argv) < 4:
            print("usage: python conflict_recorder.py resolve <file_a> <file_b> [note]")
            return
        n = resolve(sys.argv[2], sys.argv[3], note=" ".join(sys.argv[4:]) or "manual")
        print(f"resolved {n} conflict(s)")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
