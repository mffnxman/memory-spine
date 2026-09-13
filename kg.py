"""
kg.py — knowledge graph layer for the memory system.

Lives in the same SQLite sidecar (_meta/memory.db). Pure stdlib — no Kuzu/Neo4j
dependency. For ~40-200 memories, recursive CTEs handle multi-hop traversal
plenty fast.

Schema:
  entities          — nodes (people, projects, tools, concepts, files)
  relationships     — typed edges with confidence + source memory
  entity_mentions   — back-pointer from entity to memory file

Public API:
  upsert_entity(name, type, **props) -> entity_id
  upsert_relationship(from_name, to_name, rel_type, **props)
  query_neighbors(entity_name, hops=1, rel_types=None)
  query_path(from_name, to_name, max_hops=3)
  list_entities(type=None)
  graph_stats()

Bootstrap helpers in extract_entities.py.
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
from memory_engine import db as _base_db, MEMORY_DIR

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ─── Entity types (open vocabulary, but these are the canonical ones) ────────
ENTITY_TYPES = {
    "person",      # the user, teammates, collaborators
    "project",     # side projects, bots, repos
    "tool",        # scripts, libraries, MCP servers
    "concept",     # build-not-buy, ride-the-middle, substrate-agnostic
    "file",        # specific files referenced
    "company",     # employers, vendors, platforms
    "skill",       # /recall, /epilogue, etc.
    "value",       # white-hat, principle-over-curiosity
}

# Common relationship types (also open vocabulary)
REL_TYPES = {
    "manages", "uses", "builds", "prefers", "values", "works_at",
    "wrote_by", "depends_on", "related_to", "implements", "contains",
    "is_a", "applies_to", "supersedes", "owned_by",
}


def _ensure_bitemporal_columns(conn: sqlite3.Connection) -> None:
    """Self-heal the v14 bitemporal validity columns. kg.db()'s CREATE TABLE
    omits valid_from/valid_to/superseded_by, but upsert_relationship INSERTs
    valid_from — on a fresh brain (before migration 'e' runs) every edge write
    would raise 'no such column' and be swallowed by the write hook, leaving an
    edgeless graph. Idempotent (mirrors migration 2026_05_26_e)."""
    for table, col, ddl in [
        ("relationships", "valid_from", "INTEGER"),
        ("relationships", "valid_to", "INTEGER"),
        ("relationships", "superseded_by", "INTEGER"),
        ("entity_mentions", "valid_from", "INTEGER"),
        ("entity_mentions", "valid_to", "INTEGER"),
    ]:
        try:
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        except Exception:
            pass
    try:
        conn.execute("UPDATE relationships SET valid_from = created WHERE valid_from IS NULL")
        conn.execute("UPDATE entity_mentions SET valid_from = ts WHERE valid_from IS NULL")
    except Exception:
        pass


def db() -> sqlite3.Connection:
    """Get connection — extends the base memory.db with KG tables."""
    conn = _base_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            aliases TEXT DEFAULT '[]',
            properties TEXT DEFAULT '{}',
            first_seen INTEGER NOT NULL,
            last_seen INTEGER NOT NULL,
            confidence REAL DEFAULT 1.0,
            UNIQUE(name, type)
        );
        CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
        CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);

        CREATE TABLE IF NOT EXISTS relationships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_id INTEGER NOT NULL,
            to_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            properties TEXT DEFAULT '{}',
            confidence REAL DEFAULT 1.0,
            source_memory TEXT,
            created INTEGER NOT NULL,
            UNIQUE(from_id, to_id, type),
            FOREIGN KEY(from_id) REFERENCES entities(id) ON DELETE CASCADE,
            FOREIGN KEY(to_id) REFERENCES entities(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_rel_from ON relationships(from_id);
        CREATE INDEX IF NOT EXISTS idx_rel_to ON relationships(to_id);
        CREATE INDEX IF NOT EXISTS idx_rel_type ON relationships(type);

        CREATE TABLE IF NOT EXISTS entity_mentions (
            entity_id INTEGER NOT NULL,
            memory_filename TEXT NOT NULL,
            excerpt TEXT,
            ts INTEGER NOT NULL,
            UNIQUE(entity_id, memory_filename),
            FOREIGN KEY(entity_id) REFERENCES entities(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_em_entity ON entity_mentions(entity_id);
        CREATE INDEX IF NOT EXISTS idx_em_file ON entity_mentions(memory_filename);
    """)
    _ensure_bitemporal_columns(conn)
    return conn


# ─── Entity ops ──────────────────────────────────────────────────────────────
def upsert_entity(name: str, etype: str, aliases: Optional[list] = None,
                   properties: Optional[dict] = None, confidence: float = 1.0) -> int:
    """Insert or update entity. Returns entity id."""
    now = int(time.time())
    aliases_json = json.dumps(aliases or [])
    props_json = json.dumps(properties or {})
    with db() as conn:
        cur = conn.execute("SELECT id FROM entities WHERE name=? AND type=?", (name, etype))
        row = cur.fetchone()
        if row:
            eid = row[0]
            conn.execute(
                "UPDATE entities SET last_seen=?, confidence=MAX(confidence, ?) WHERE id=?",
                (now, confidence, eid),
            )
            return eid
        cur = conn.execute(
            "INSERT INTO entities(name, type, aliases, properties, first_seen, last_seen, confidence) "
            "VALUES (?,?,?,?,?,?,?)",
            (name, etype, aliases_json, props_json, now, now, confidence),
        )
        return cur.lastrowid


def get_entity_id(name: str, etype: Optional[str] = None) -> Optional[int]:
    """Lookup entity id by name (and optionally type). Case-insensitive."""
    with db() as conn:
        if etype:
            row = conn.execute(
                "SELECT id FROM entities WHERE LOWER(name)=LOWER(?) AND type=?", (name, etype)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT id FROM entities WHERE LOWER(name)=LOWER(?)", (name,)
            ).fetchone()
        return row[0] if row else None


def upsert_relationship(from_name: str, to_name: str, rel_type: str,
                        from_type: Optional[str] = None, to_type: Optional[str] = None,
                        properties: Optional[dict] = None, confidence: float = 1.0,
                        source_memory: Optional[str] = None) -> Optional[int]:
    """Create or update a relationship with bi-temporal validity (v14).

    Behavior:
      - If (from, to, type) already exists: update confidence + source_memory (re-assertion)
      - If type is FUNCTIONAL (lives_in, works_at, etc) and a row exists with
        the same (from, type) but a DIFFERENT to: mark old row superseded,
        insert new row, link them via superseded_by
      - If type is ADDITIVE: insert new row, leave parallel rows alone

    Auto-creates entities if needed (as 'concept').
    """
    if not from_name or not to_name or from_name.lower() == to_name.lower():
        return None
    from_id = get_entity_id(from_name, from_type) or upsert_entity(from_name, from_type or "concept")
    to_id = get_entity_id(to_name, to_type) or upsert_entity(to_name, to_type or "concept")
    now = int(time.time())
    props_json = json.dumps(properties or {})
    with db() as conn:
        # Re-assertion path: same (from, to, type) — just refresh
        cur = conn.execute(
            "SELECT id FROM relationships WHERE from_id=? AND to_id=? AND type=?",
            (from_id, to_id, rel_type),
        )
        row = cur.fetchone()
        if row:
            rid = row[0]
            conn.execute(
                "UPDATE relationships SET confidence=MAX(confidence, ?), source_memory=COALESCE(?, source_memory) WHERE id=?",
                (confidence, source_memory, rid),
            )
            return rid

        # v14: check for functional contradictions BEFORE inserting
        superseded_ids = []
        try:
            from bitemporal import find_superseding_candidates, mark_superseded
            superseded_ids = find_superseding_candidates(conn, from_id, rel_type, to_id)
        except Exception:
            pass  # fail-soft: if bitemporal logic breaks, fall back to plain insert

        cur = conn.execute(
            "INSERT INTO relationships(from_id, to_id, type, properties, confidence, source_memory, created, valid_from) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (from_id, to_id, rel_type, props_json, confidence, source_memory, now, now),
        )
        new_rid = cur.lastrowid
        for old_id in superseded_ids:
            try:
                mark_superseded(conn, old_id, new_rid, ts=now)
            except Exception:
                pass
        return new_rid


def add_mention(entity_name: str, memory_filename: str, excerpt: str = "") -> None:
    eid = get_entity_id(entity_name)
    if not eid:
        return
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO entity_mentions(entity_id, memory_filename, excerpt, ts) VALUES (?,?,?,?)",
            (eid, memory_filename, excerpt[:300], int(time.time())),
        )


# ─── Query API ───────────────────────────────────────────────────────────────
@dataclass
class Edge:
    from_name: str
    from_type: str
    to_name: str
    to_type: str
    rel_type: str
    confidence: float
    source_memory: Optional[str] = None


def query_neighbors(entity_name: str, hops: int = 1, rel_types: Optional[list[str]] = None,
                    direction: str = "both") -> list[Edge]:
    """Get all edges reachable from entity within `hops` steps.

    direction: 'out' (entity -> ?), 'in' (? -> entity), 'both'.
    """
    eid = get_entity_id(entity_name)
    if not eid:
        return []

    visited_edges: set[tuple] = set()
    visited_nodes: set[int] = {eid}
    frontier: set[int] = {eid}

    out: list[Edge] = []
    rel_filter = ""
    rel_params: list = []
    if rel_types:
        rel_filter = " AND r.type IN (" + ",".join("?" * len(rel_types)) + ")"
        rel_params = list(rel_types)

    with db() as conn:
        for _ in range(hops):
            new_frontier: set[int] = set()
            ids_param = ",".join("?" * len(frontier))
            sql_parts = []
            params: list = []

            if direction in ("out", "both"):
                sql_parts.append(f"""
                  SELECT r.id, r.from_id, fe.name, fe.type, r.to_id, te.name, te.type, r.type, r.confidence, r.source_memory
                  FROM relationships r
                  JOIN entities fe ON fe.id = r.from_id
                  JOIN entities te ON te.id = r.to_id
                  WHERE r.from_id IN ({ids_param}){rel_filter}
                """)
                params.extend(list(frontier))
                params.extend(rel_params)

            if direction in ("in", "both"):
                sql_parts.append(f"""
                  SELECT r.id, r.from_id, fe.name, fe.type, r.to_id, te.name, te.type, r.type, r.confidence, r.source_memory
                  FROM relationships r
                  JOIN entities fe ON fe.id = r.from_id
                  JOIN entities te ON te.id = r.to_id
                  WHERE r.to_id IN ({ids_param}){rel_filter}
                """)
                params.extend(list(frontier))
                params.extend(rel_params)

            sql = " UNION ".join(sql_parts)
            for row in conn.execute(sql, params):
                rid, fid, fname, ftype, tid, tname, ttype, rt, conf, src = row
                if rid in visited_edges:
                    continue
                visited_edges.add(rid)
                out.append(Edge(fname, ftype, tname, ttype, rt, conf, src))
                if fid not in visited_nodes:
                    visited_nodes.add(fid)
                    new_frontier.add(fid)
                if tid not in visited_nodes:
                    visited_nodes.add(tid)
                    new_frontier.add(tid)
            frontier = new_frontier
            if not frontier:
                break

    return out


def list_entities(etype: Optional[str] = None, limit: int = 100) -> list[dict]:
    with db() as conn:
        if etype:
            rows = conn.execute(
                "SELECT id, name, type, confidence FROM entities WHERE type=? ORDER BY confidence DESC LIMIT ?",
                (etype, limit),
            )
        else:
            rows = conn.execute(
                "SELECT id, name, type, confidence FROM entities ORDER BY confidence DESC LIMIT ?", (limit,)
            )
        return [{"id": r[0], "name": r[1], "type": r[2], "confidence": r[3]} for r in rows]


def graph_stats() -> dict:
    with db() as conn:
        n_ent = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        n_rel = conn.execute("SELECT COUNT(*) FROM relationships").fetchone()[0]
        n_mention = conn.execute("SELECT COUNT(*) FROM entity_mentions").fetchone()[0]
        by_type = dict(conn.execute("SELECT type, COUNT(*) FROM entities GROUP BY type"))
        rel_by_type = dict(conn.execute("SELECT type, COUNT(*) FROM relationships GROUP BY type"))
        # Hub entities (most connected)
        hubs = list(conn.execute("""
            SELECT e.name, e.type, COUNT(*) as deg
            FROM entities e
            JOIN relationships r ON (r.from_id = e.id OR r.to_id = e.id)
            GROUP BY e.id ORDER BY deg DESC LIMIT 10
        """))
    return {
        "entities": n_ent,
        "relationships": n_rel,
        "mentions": n_mention,
        "by_type": by_type,
        "rel_by_type": rel_by_type,
        "top_hubs": [{"name": h[0], "type": h[1], "degree": h[2]} for h in hubs],
    }


def main():
    """CLI for inspection."""
    if len(sys.argv) < 2:
        s = graph_stats()
        print(f"Entities:        {s['entities']}")
        print(f"Relationships:   {s['relationships']}")
        print(f"Mentions:        {s['mentions']}")
        print(f"By type:         {s['by_type']}")
        print(f"Rel by type:     {s['rel_by_type']}")
        print(f"Top hubs:")
        for h in s["top_hubs"]:
            print(f"  - {h['name']} ({h['type']}, degree={h['degree']})")
        return

    cmd = sys.argv[1]
    if cmd == "neighbors":
        name = sys.argv[2]
        hops = int(sys.argv[3]) if len(sys.argv) > 3 else 1
        edges = query_neighbors(name, hops=hops)
        for e in edges:
            print(f"  {e.from_name:30s} --{e.rel_type:>15s}--> {e.to_name}  (conf={e.confidence:.2f})")
    elif cmd == "list":
        etype = sys.argv[2] if len(sys.argv) > 2 else None
        for e in list_entities(etype):
            print(f"  [{e['type']:8s}] {e['name']}")
    else:
        print(f"Unknown: {cmd}")


if __name__ == "__main__":
    main()
