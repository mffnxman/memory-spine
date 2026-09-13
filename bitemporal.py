"""
bitemporal.py — bi-temporal validity primitives for KG relationships + observations.

Steals the Graphiti/Zep pattern: every fact has a validity interval
(valid_from, valid_to). Contradictions don't delete; they mark old facts
superseded and link to the new one. Retrieval filters by validity at
query time, with an optional --as-of for historical queries.

Functional relationship types (one valid object at a time):
  - lives_in, works_at, uses_primary, current_status, owns
Additive types (many can coexist):
  - mentioned_in, corroborates, related_to, applies_to

This module is the pure logic. Schema migration + integration in
upsert_relationship lives in migrations/2026_05_26_e_bitemporal_relationships.py
and the patched kg.py.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))


# Relationship types that have ONE valid object at a time. Writing a new fact
# with this rel_type + different to_id supersedes the old fact.
FUNCTIONAL_RELS = {
    "lives_in",
    "works_at",
    "uses_primary",
    "current_status",
    "current_role",
    "current_focus",
    "owns_singleton",
    "is_a",        # if reclassified, supersede prior class
}

# Relationship types where multiple parallel facts are legitimate.
# Listed for documentation; absence from FUNCTIONAL_RELS is the actual gate.
ADDITIVE_RELS = {
    "mentioned_in",
    "corroborates",
    "related_to",
    "applies_to",
    "uses",        # uses multiple things is fine
    "owns",        # owns multiple things is fine
    "knows",
    "collaborates_with",
}


def is_functional(rel_type: str) -> bool:
    return rel_type.lower() in FUNCTIONAL_RELS


def find_superseding_candidates(conn: sqlite3.Connection, from_id: int,
                                rel_type: str, new_to_id: int) -> list[int]:
    """Return relationship row IDs that THIS new fact would supersede.

    Only returns rows when rel_type is functional AND existing target differs.
    Existing rows that are already superseded are not returned.
    """
    if not is_functional(rel_type):
        return []
    rows = conn.execute(
        "SELECT id FROM relationships "
        "WHERE from_id = ? AND type = ? AND to_id != ? "
        "AND (valid_to IS NULL) AND (superseded_by IS NULL)",
        (from_id, rel_type, new_to_id),
    ).fetchall()
    return [r[0] for r in rows]


def mark_superseded(conn: sqlite3.Connection, old_rel_id: int,
                    new_rel_id: int, ts: Optional[int] = None) -> None:
    """Set valid_to + superseded_by on an existing relationship row."""
    ts = ts or int(time.time())
    conn.execute(
        "UPDATE relationships SET valid_to = ?, superseded_by = ? WHERE id = ?",
        (ts, new_rel_id, old_rel_id),
    )


def active_filter_sql(table_alias: str = "r", as_of_param: str = ":as_of") -> str:
    """SQL fragment for filtering to currently-valid rows.

    Use as: f"WHERE {active_filter_sql()} AND ..."
    Pass as_of in the query params as an int unix ts. None = now.
    """
    return (
        f"({table_alias}.valid_from <= COALESCE({as_of_param}, strftime('%s','now')) "
        f"AND ({table_alias}.valid_to IS NULL OR {table_alias}.valid_to > COALESCE({as_of_param}, strftime('%s','now'))))"
    )


def supersession_chain(conn: sqlite3.Connection, rel_id: int) -> list[dict]:
    """Walk back through superseded_by pointers. Returns [oldest, ..., current]."""
    chain = []
    current_id = rel_id
    seen = set()
    # First walk forward to current
    while current_id and current_id not in seen:
        seen.add(current_id)
        row = conn.execute(
            "SELECT id, from_id, to_id, type, valid_from, valid_to, superseded_by FROM relationships WHERE id = ?",
            (current_id,),
        ).fetchone()
        if not row:
            break
        chain.append({
            "id": row[0], "from_id": row[1], "to_id": row[2], "type": row[3],
            "valid_from": row[4], "valid_to": row[5], "superseded_by": row[6],
        })
        current_id = row[6]
    return chain


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="bitemporal logic CLI")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("functionals", help="List functional relationship types")
    sub.add_parser("test", help="Smoke tests")
    args = ap.parse_args()

    if args.cmd == "functionals":
        print(json.dumps(sorted(FUNCTIONAL_RELS), indent=2))
    elif args.cmd == "test":
        assert is_functional("lives_in")
        assert not is_functional("mentioned_in")
        assert is_functional("LIVES_IN")  # case-insensitive
        print("Smoke tests passed.")
    else:
        ap.print_help()
