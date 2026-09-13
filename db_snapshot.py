"""Consistent sqlite snapshots for the nightly backup (bigbuff Phase 1).

Uses the sqlite backup API so live WAL databases snapshot cleanly.
Point a nightly scheduler at it; safe to run any time.
"""

import sqlite3
import sys
from pathlib import Path
import sys as _sys  # noqa: E402
_sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths as _paths  # noqa: E402

MEM = _paths.META_DIR

PAIRS = [
    (MEM / "memory.db", "memory.db"),
    (MEM / "observations.db", "observations.db"),
]


def main(dst_dir: str) -> int:
    dst = Path(dst_dir)
    dst.mkdir(parents=True, exist_ok=True)
    failures = 0
    for src, name in PAIRS:
        if not src.exists():
            print(f"skip (missing): {src}")
            continue
        try:
            with sqlite3.connect(src) as s, sqlite3.connect(dst / name) as d:
                s.backup(d)
            print(f"snapshot ok: {name}")
        except Exception as e:  # keep going — partial backup beats none
            failures += 1
            print(f"SNAPSHOT FAILED {name}: {e}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(
        main(
            sys.argv[1]
            if len(sys.argv) > 1
            else str((_paths.BACKUP_DIR or _paths.META_DIR / "backups") / "db-snapshots")
        )
    )
