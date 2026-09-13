"""maintenance.py — zero-touch weekly upkeep (v15, 2026-07-09).

The observer plan's phase 6 prescribed ~30 min/week of manual maintenance
(vacuum, orphan sweep) that never became a habit. This module does it inside
the sleep cycle instead:

  - mark_abandoned(): active sessions older than 24h (crash orphans, the Stop
    hook never fired) get status='abandoned'
  - vacuum_dbs(): VACUUM + ANALYZE observations.db and memory.db
  - floor_check(): run the generated retrieval-floor suite and diff against
    its baseline — retrieval rot gets detected weekly, not whenever someone
    remembers. Result lands in _meta/floor_last.json; the health sentinel
    surfaces regressions at boot.

CLI:
    python maintenance.py run          # all three
    python maintenance.py floor        # floor check only
"""

from __future__ import annotations

import json
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
META_DIR = MEMORY_DIR / "_meta"
FLOOR_LAST_PATH = META_DIR / "floor_last.json"
DEFAULT_DBS = [META_DIR / "observations.db", META_DIR / "memory.db"]


def mark_abandoned(db_path=None, max_active_hours=24):
    """Crash-orphaned sessions (active, old, Stop hook never fired) → abandoned."""
    db_path = db_path or META_DIR / "observations.db"
    cutoff = int(time.time() - max_active_hours * 3600)
    try:
        with sqlite3.connect(str(db_path), timeout=5.0) as conn:
            cur = conn.execute(
                "UPDATE sessions SET status='abandoned' "
                "WHERE status='active' AND started_at < ?",
                (cutoff,),
            )
            conn.commit()
            return {"abandoned": cur.rowcount}
    except Exception as e:
        return {"abandoned": 0, "error": str(e)[:120]}


def vacuum_dbs(paths=None):
    """VACUUM + ANALYZE each db. Cheap at our sizes; keeps FTS indexes tight."""
    res = {"vacuumed": 0, "errors": []}
    for p in paths or DEFAULT_DBS:
        p = Path(p)
        if not p.exists():
            continue
        try:
            conn = sqlite3.connect(str(p), timeout=10.0)
            conn.execute("VACUUM")
            conn.execute("ANALYZE")
            conn.close()
            res["vacuumed"] += 1
        except Exception as e:
            res["errors"].append(f"{p.name}: {e}"[:120])
    return res


def _run_floor_suite():
    """Live floor run (generated cases via search_hybrid). Isolated for tests."""
    from benchmark_gen import generate, run_floor

    gen = generate()
    report = run_floor(gen["floor"] + gen["temporal"])
    return {
        "n": report["n"],
        "passed": report["passed"],
        "mrr": report["mrr"],
        "failed_ids": sorted(f["id"] for f in report["failures"]),
    }


def _load_floor_baseline():
    from benchmark_gen import FLOOR_BASELINE

    if not FLOOR_BASELINE.exists():
        return None
    return json.loads(FLOOR_BASELINE.read_text(encoding="utf-8"))


def floor_check():
    """Run the floor suite, diff vs baseline, persist for the sentinel."""
    current = _run_floor_suite()
    base = _load_floor_baseline()
    regressions = []
    if base is not None:
        regressions = sorted(
            set(current["failed_ids"]) - set(base.get("failed_ids", []))
        )
    result = {
        "ts": int(time.time()),
        "n": current["n"],
        "passed": current["passed"],
        "mrr": current["mrr"],
        "regressions": regressions,
    }
    try:
        FLOOR_LAST_PATH.parent.mkdir(parents=True, exist_ok=True)
        FLOOR_LAST_PATH.write_text(json.dumps(result, indent=2), encoding="utf-8")
    except OSError:
        pass
    return result


def run():
    return {
        "abandoned_sessions": mark_abandoned(),
        "vacuum": vacuum_dbs(),
        "floor": floor_check(),
    }


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        print(json.dumps(run(), indent=2))
    elif cmd == "floor":
        print(json.dumps(floor_check(), indent=2))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
