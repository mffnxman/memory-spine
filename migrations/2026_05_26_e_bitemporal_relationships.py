"""
2026_05_26_e_bitemporal_relationships — add validity columns to KG tables.

Adds:
  relationships.valid_from INTEGER  (backfilled from created)
  relationships.valid_to INTEGER NULL
  relationships.superseded_by INTEGER NULL
  entity_mentions.valid_from INTEGER  (backfilled from ts)
  entity_mentions.valid_to INTEGER NULL
  observation_corroboration.valid_to INTEGER NULL
  observation_corroboration.superseded_by_hash TEXT NULL

All additive. Existing rows continue to work — they read as "valid forever
starting from their creation timestamp." Retrieval code in memory_engine
gains the active_filter_sql() WHERE clause from bitemporal module.

Pre-migration tarball: _meta/migrations_backups/2026_05_26_e_bitemporal_relationships.tar.gz
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tarfile
import time
from pathlib import Path
import sys as _sys  # noqa: E402
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import paths as _paths  # noqa: E402

MIGRATION_ID = "2026_05_26_e_bitemporal_relationships"

META_DIR = _paths.META_DIR
DB_PATH = META_DIR / "memory.db"
BACKUP_DIR = META_DIR / "migrations_backups"


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> bool:
    if _column_exists(conn, table, column):
        return False
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    return True


def run(dry_run: bool = False) -> dict:
    if not DB_PATH.exists():
        return {"error": "memory.db not found — nothing to migrate"}

    summary = {
        "added_columns": [],
        "backfilled_relationships": 0,
        "backfilled_mentions": 0,
        "backfilled_corroboration": 0,
    }

    if dry_run:
        # Just report what would change
        with sqlite3.connect(DB_PATH) as conn:
            for tbl, col in [
                ("relationships", "valid_from"),
                ("relationships", "valid_to"),
                ("relationships", "superseded_by"),
                ("entity_mentions", "valid_from"),
                ("entity_mentions", "valid_to"),
                ("observation_corroboration", "valid_to"),
                ("observation_corroboration", "superseded_by_hash"),
            ]:
                if not _column_exists(conn, tbl, col):
                    summary["added_columns"].append(f"{tbl}.{col}")
            summary["dry_run"] = True
        return summary

    # Backup the DB before schema changes
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    tar_path = BACKUP_DIR / f"{MIGRATION_ID}.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(DB_PATH, arcname="memory.db.pre-bitemporal")

    with sqlite3.connect(DB_PATH) as conn:
        # relationships table
        if _add_column_if_missing(conn, "relationships", "valid_from", "INTEGER"):
            summary["added_columns"].append("relationships.valid_from")
        if _add_column_if_missing(conn, "relationships", "valid_to", "INTEGER"):
            summary["added_columns"].append("relationships.valid_to")
        if _add_column_if_missing(conn, "relationships", "superseded_by", "INTEGER"):
            summary["added_columns"].append("relationships.superseded_by")
        # Backfill valid_from from created
        cur = conn.execute(
            "UPDATE relationships SET valid_from = created WHERE valid_from IS NULL"
        )
        summary["backfilled_relationships"] = cur.rowcount

        # entity_mentions table
        if _add_column_if_missing(conn, "entity_mentions", "valid_from", "INTEGER"):
            summary["added_columns"].append("entity_mentions.valid_from")
        if _add_column_if_missing(conn, "entity_mentions", "valid_to", "INTEGER"):
            summary["added_columns"].append("entity_mentions.valid_to")
        cur = conn.execute(
            "UPDATE entity_mentions SET valid_from = ts WHERE valid_from IS NULL"
        )
        summary["backfilled_mentions"] = cur.rowcount

        # observation_corroboration table (may not exist yet on fresh installs)
        try:
            if _add_column_if_missing(conn, "observation_corroboration", "valid_to", "INTEGER"):
                summary["added_columns"].append("observation_corroboration.valid_to")
            if _add_column_if_missing(conn, "observation_corroboration", "superseded_by_hash", "TEXT"):
                summary["added_columns"].append("observation_corroboration.superseded_by_hash")
            cur = conn.execute(
                "UPDATE observation_corroboration SET valid_to = NULL WHERE valid_to IS NULL"
            )
            # rowcount here is approximate — valid_to = NULL no-op
        except sqlite3.OperationalError:
            # Table doesn't exist yet (e.g., dedup never invoked) — skip
            summary["corroboration_skipped"] = "table absent"

        # Add indexes for the validity columns
        conn.executescript("""
            CREATE INDEX IF NOT EXISTS idx_rel_validity ON relationships(valid_from, valid_to);
            CREATE INDEX IF NOT EXISTS idx_em_validity ON entity_mentions(valid_from, valid_to);
        """)
        summary["added_columns"].append("indexes: idx_rel_validity, idx_em_validity")

    return summary
