"""
2026_05_27_a_importance_tables — schema for v3.2 Phase 3 (importance scoring).

Adds two new tables to memory.db:

  importance_scores
    filename       TEXT      — memory filename
    content_hash   TEXT      — md5 of name+description+body; cache key
    score          INTEGER   — 1-10 LLM-rated salience
    reasoning      TEXT      — one-line justification from the LLM
    scored_at      INTEGER   — unix ts
    model_used     TEXT      — which provider+model produced the score
    UNIQUE(filename, content_hash)

  reflection_state
    category               TEXT PRIMARY KEY  — 'self' | 'feedback' | 'project' | 'user'
    accumulated_score      INTEGER NOT NULL DEFAULT 0
    last_reflection_ts     INTEGER NULL
    threshold              INTEGER NOT NULL DEFAULT 150
    last_score_added_ts    INTEGER NULL

Both tables are additive. No existing table modified. Pre-migration tarball
written to _meta/migrations_backups/ in case rollback needed.
"""
from __future__ import annotations

import sqlite3
import sys
import tarfile
import time
from pathlib import Path
import sys as _sys  # noqa: E402
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import paths as _paths  # noqa: E402

MIGRATION_ID = "2026_05_27_a_importance_tables"

META_DIR = _paths.META_DIR
DB_PATH = META_DIR / "memory.db"
BACKUP_DIR = META_DIR / "migrations_backups"


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def run(dry_run: bool = False) -> dict:
    if not DB_PATH.exists():
        return {"error": "memory.db not found — nothing to migrate"}

    summary = {"created_tables": [], "seeded_reflection_state": 0}

    if dry_run:
        with sqlite3.connect(DB_PATH) as conn:
            if not _table_exists(conn, "importance_scores"):
                summary["created_tables"].append("importance_scores")
            if not _table_exists(conn, "reflection_state"):
                summary["created_tables"].append("reflection_state")
            summary["dry_run"] = True
        return summary

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    tar_path = BACKUP_DIR / f"{MIGRATION_ID}.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(DB_PATH, arcname="memory.db.pre-importance-tables")

    with sqlite3.connect(DB_PATH) as conn:
        if not _table_exists(conn, "importance_scores"):
            conn.executescript("""
                CREATE TABLE importance_scores (
                    filename TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    score INTEGER NOT NULL,
                    reasoning TEXT,
                    scored_at INTEGER NOT NULL,
                    model_used TEXT,
                    UNIQUE(filename, content_hash)
                );
                CREATE INDEX idx_imp_filename ON importance_scores(filename);
                CREATE INDEX idx_imp_scored_at ON importance_scores(scored_at);
            """)
            summary["created_tables"].append("importance_scores")

        if not _table_exists(conn, "reflection_state"):
            conn.executescript("""
                CREATE TABLE reflection_state (
                    category TEXT PRIMARY KEY,
                    accumulated_score INTEGER NOT NULL DEFAULT 0,
                    last_reflection_ts INTEGER,
                    threshold INTEGER NOT NULL DEFAULT 150,
                    last_score_added_ts INTEGER
                );
            """)
            summary["created_tables"].append("reflection_state")

            # Seed the four canonical categories so accumulate() can just UPDATE.
            now_ts = int(time.time())
            for cat in ("self", "feedback", "project", "user"):
                conn.execute(
                    "INSERT INTO reflection_state(category, accumulated_score, threshold, last_score_added_ts) "
                    "VALUES (?, 0, 150, ?)",
                    (cat, now_ts),
                )
            summary["seeded_reflection_state"] = 4

    return summary


def main():
    import json
    dry = "--dry-run" in sys.argv
    result = run(dry_run=dry)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
