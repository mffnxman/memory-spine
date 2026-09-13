"""
dedup.py — canonical content hashing for memories + observations.

Replaces ad-hoc string comparison with a deterministic hash over the semantic
core (subject + predicate + object + type) that EXCLUDES provenance metadata.
This means the same observation captured in two different sessions, by two
different agents, with two different timestamps, hashes identically.

Used by:
  - conflict.py: fast-path exact-match check before cosine fallback
  - kg.add_mention: dedupe identical mentions, increment corroboration_count
  - migrations: backfill corroboration on existing duplicate mentions

Hash format: `sha256:<32-hex>` truncated for log readability.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))


WHITESPACE_RE = re.compile(r"\s+")
QUOTE_RE = re.compile(r"[\"'`‘’“”]")


def normalize(text: str) -> str:
    """Lowercase, collapse whitespace, strip quotes/punctuation noise."""
    if not text:
        return ""
    text = text.lower().strip()
    text = QUOTE_RE.sub("", text)
    text = WHITESPACE_RE.sub(" ", text)
    return text


def compute_dedup_hash(obs: dict) -> str:
    """Canonical hash over semantic core. Excludes provenance/timestamp/session."""
    core = {
        "subject": normalize(str(obs.get("subject") or obs.get("name") or "")),
        "predicate": normalize(str(obs.get("predicate") or obs.get("type") or "")),
        "object": normalize(str(obs.get("object") or obs.get("value") or obs.get("description") or "")),
        "kind": normalize(str(obs.get("kind") or obs.get("entity_type") or "")),
    }
    canonical = json.dumps(core, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def compute_memory_hash(name: str, description: str, body: str, mem_type: str = "") -> str:
    """Hash a full memory file at semantic-core level."""
    return compute_dedup_hash({
        "subject": name,
        "predicate": mem_type,
        "object": (description + "\n" + body)[:4000],  # truncate ultra-long bodies
        "kind": "memory",
    })


# ─── Corroboration tracking ─────────────────────────────────────────────────
def _ensure_corroboration_schema(conn: sqlite3.Connection) -> None:
    """Add corroboration_count + dedup_hash columns to entity_mentions if missing."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS observation_corroboration (
            dedup_hash TEXT PRIMARY KEY,
            subject TEXT,
            predicate TEXT,
            object TEXT,
            kind TEXT,
            corroboration_count INTEGER DEFAULT 1,
            first_seen INTEGER NOT NULL,
            last_seen INTEGER NOT NULL,
            sources TEXT DEFAULT '[]'
        );
        CREATE INDEX IF NOT EXISTS idx_corrob_subj ON observation_corroboration(subject);
    """)


def record_observation(obs: dict, source: str | None = None) -> tuple[str, int]:
    """Record an observation and return (dedup_hash, corroboration_count).

    On duplicate dedup_hash: increment count, append source to sources, update last_seen.
    On new dedup_hash: insert with count=1.
    """
    try:
        from memory_engine import db as _base_db
    except Exception:
        return ("", 0)

    h = compute_dedup_hash(obs)
    now = int(time.time())
    src = source or "unknown"

    with _base_db() as conn:
        _ensure_corroboration_schema(conn)
        row = conn.execute(
            "SELECT corroboration_count, sources FROM observation_corroboration WHERE dedup_hash=?",
            (h,),
        ).fetchone()
        if row:
            count, sources_json = row[0], row[1]
            try:
                sources = json.loads(sources_json) if sources_json else []
            except Exception:
                sources = []
            if src not in sources:
                sources.append(src)
            new_count = count + 1
            conn.execute(
                "UPDATE observation_corroboration SET corroboration_count=?, last_seen=?, sources=? WHERE dedup_hash=?",
                (new_count, now, json.dumps(sources), h),
            )
            return (h, new_count)
        else:
            conn.execute(
                "INSERT INTO observation_corroboration(dedup_hash, subject, predicate, object, kind, corroboration_count, first_seen, last_seen, sources) "
                "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (
                    h,
                    normalize(str(obs.get("subject") or "")),
                    normalize(str(obs.get("predicate") or "")),
                    normalize(str(obs.get("object") or ""))[:500],
                    normalize(str(obs.get("kind") or "")),
                    now,
                    now,
                    json.dumps([src]),
                ),
            )
            return (h, 1)


def find_duplicates(min_count: int = 2) -> list[dict]:
    """List observations with corroboration_count >= min_count."""
    try:
        from memory_engine import db as _base_db
    except Exception:
        return []
    out = []
    with _base_db() as conn:
        _ensure_corroboration_schema(conn)
        for row in conn.execute(
            "SELECT dedup_hash, subject, predicate, object, kind, corroboration_count, sources "
            "FROM observation_corroboration WHERE corroboration_count >= ? "
            "ORDER BY corroboration_count DESC",
            (min_count,),
        ):
            out.append({
                "hash": row[0],
                "subject": row[1],
                "predicate": row[2],
                "object": row[3],
                "kind": row[4],
                "count": row[5],
                "sources": json.loads(row[6]) if row[6] else [],
            })
    return out


# ─── CLI ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="dedup module CLI")
    sub = ap.add_subparsers(dest="cmd")
    p_hash = sub.add_parser("hash", help="Compute dedup hash from JSON observation on stdin")
    p_test = sub.add_parser("test", help="Run smoke tests")
    sub.add_parser("dupes", help="List corroborated observations")
    args = ap.parse_args()

    if args.cmd == "hash":
        obs = json.loads(sys.stdin.read())
        print(compute_dedup_hash(obs))
    elif args.cmd == "test":
        # Same observation, different metadata → same hash
        a = {"subject": "Alex", "predicate": "values", "object": "white hat", "kind": "feedback"}
        b = {"subject": "  ALEX  ", "predicate": "Values", "object": '"White Hat"', "kind": "FEEDBACK"}
        c = {"subject": "Alex", "predicate": "values", "object": "black hat", "kind": "feedback"}
        ha, hb, hc = compute_dedup_hash(a), compute_dedup_hash(b), compute_dedup_hash(c)
        print(f"a == b ?  {ha == hb}   (canonicalization)")
        print(f"a != c ?  {ha != hc}   (semantic distinction)")
        assert ha == hb, "Canonicalization failed"
        assert ha != hc, "Semantic distinction failed"

        # Corroboration round-trip
        h1, c1 = record_observation(a, source="claude_code:session-1")
        h2, c2 = record_observation(b, source="dartagnan:session-2")
        h3, c3 = record_observation(a, source="claude_code:session-1")  # same source -> still increments
        print(f"corrob first={c1}  second={c2}  third={c3}")
        assert c2 > c1, "Corroboration not incrementing"
        print("All smoke tests passed.")
    elif args.cmd == "dupes":
        for d in find_duplicates(min_count=2):
            print(json.dumps(d, ensure_ascii=False))
    else:
        ap.print_help()
