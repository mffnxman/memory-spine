"""
2026_05_26_c_backfill_corroboration — seed observation_corroboration table
from existing memory files. Each memory file becomes a baseline observation
with corroboration_count=1 so future re-mentions can increment it.
"""
from __future__ import annotations

import sys
from pathlib import Path

MIGRATION_ID = "2026_05_26_c_backfill_corroboration"


def run(dry_run: bool = False) -> dict:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        from dedup import record_observation
        from memory_engine import list_memories
    except Exception as e:
        return {"error": f"import failed: {e}"}

    try:
        mems = list_memories()
    except Exception as e:
        return {"error": f"list_memories failed: {e}"}

    n_seeded = 0
    n_already = 0
    for m in mems:
        obs = {
            "subject": m.name or m.filename,
            "predicate": m.type or "memory",
            "object": (m.description or "")[:500],
            "kind": "memory",
        }
        if dry_run:
            n_seeded += 1
            continue
        try:
            h, count = record_observation(obs, source=f"claude_code:backfill:{m.filename}")
            if count == 1:
                n_seeded += 1
            else:
                n_already += 1
        except Exception:
            pass

    return {"seeded": n_seeded, "already_known": n_already, "total_memories": len(mems)}
