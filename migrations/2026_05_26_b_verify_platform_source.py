"""
2026_05_26_b_verify_platform_source — verification migration for Phase 2.

Re-runs migrate_platform_source.py (which is idempotent — skips files that
already have the field). This catches any memory file added between Phase 2
and the migration framework rollout.
"""
from __future__ import annotations

import sys
from pathlib import Path

MIGRATION_ID = "2026_05_26_b_verify_platform_source"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import paths as _paths  # noqa: E402

MEMORY_DIR = _paths.MEMORY_DIR
# Local FRONTMATTER_RE / PLATFORM_RE removed — this migration delegates to
# migrate_platform_source.process_file which uses the canonical regex from
# memory_engine. Unused local copies risk drifting from the source of truth.


def run(dry_run: bool = False) -> dict:
    # Re-import the existing migrator and call its functions.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        import migrate_platform_source as mps
    except Exception as e:
        return {"error": f"could not import migrator: {e}"}

    md_files = sorted(MEMORY_DIR.glob("*.md"))
    md_files = [f for f in md_files if f.name != "MEMORY.md"]

    counts = {"checked": 0, "added": 0, "would_add": 0, "already_has": 0}
    for path in md_files:
        counts["checked"] += 1
        status, _ = mps.process_file(path, dry_run)
        if status == "added":
            counts["added"] += 1
        elif status == "would-add":
            counts["would_add"] += 1
        elif status == "already-has":
            counts["already_has"] += 1

    return counts
