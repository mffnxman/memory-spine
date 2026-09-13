"""
kg_ppr.py — Personalized PageRank over the knowledge graph (v3.2 Phase 2).

Standalone module that makes the KG actually pull weight in retrieval.

Algorithm (HippoRAG core, adapted for personal-memory scale):
  1. Extract query entities by matching the query against SEEDS + aliases
     from kg_bootstrap (canonical entity vocabulary).
  2. Build a column-stochastic adjacency matrix from `relationships`
     (treated as undirected for PPR — typical for retrieval graphs).
     Edge weight = confidence × recency_decay.
  3. Power-iterate PPR: r = (1-α)·M·r + α·p,  α=0.15,  iters=30.
     Seeds = uniform distribution over query-matched entities.
  4. Map top entities → memory files via the `entity_mentions` back-pointers.
     A memory's score = max(score of entities it mentions).

Scale: with N=42 entities, M=43 edges, the full PPR pass is sub-millisecond
in dense numpy. No sparse machinery needed. Revisit at N>500.

Failover: if no query entities match, returns []. Caller (RRF fusion) is
expected to handle that as "no signal from PPR" and rely on other retrievers.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
import paths as _paths  # noqa: E402

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ALPHA = 0.15                # teleport probability (standard PPR)
ITERS = 30                  # power iterations (overkill for N=42, safe for growth)
RECENCY_HALF_LIFE_DAYS = 180  # edge weight decay; matches retrieval recency model
MIN_SCORE_THRESHOLD = 1e-4    # don't bother surfacing entities below this

MEMORY_DIR = _paths.MEMORY_DIR
TELEMETRY_PATH = MEMORY_DIR / "_meta" / "v3_2_telemetry.jsonl"


def _log_telemetry(record: dict) -> None:
    try:
        TELEMETRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        record.setdefault("ts", int(time.time()))
        record.setdefault("component", "kg_ppr")
        with TELEMETRY_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception:
        pass


# Generic entities that aren't useful as PPR seeds. The user is the universal
# subject of nearly every memory — seeding on them collapses PPR into a uniform
# distribution over user-mentioning memories (which is basically all of them).
# Filter these from query-side seeds; the discriminative entities are the
# specific ones (a company, a project, a value).
GENERIC_SEED_BLOCKLIST = {_paths.USER_NAME.lower(), "claude"}

MIN_KEY_LEN = 3   # don't match 1-2 character entity names


def extract_query_entities(query: str) -> list[int]:
    """Match query text against ALL entities + aliases in the KG. Returns
    matched entity IDs.

    Uses the same word-boundary regex pattern as memory_write_postprocess
    so query-side matching mirrors write-side extraction. Longest-key-first
    to prevent "Claude" eating "Claude Code". Filters out generic seeds that
    would collapse PPR into hub-dominated uniformity."""
    try:
        from kg import db
    except Exception:
        return []

    query_lower = query.lower()

    # Build a lookup of every entity name + alias from the KG (not just
    # SEEDS — auto-extracted concepts must also be matchable).
    lookup: dict[str, int] = {}  # lowercased key → entity_id
    try:
        with db() as conn:
            for row in conn.execute("SELECT id, name, aliases FROM entities"):
                eid, name, aliases_json = row
                if not name:
                    continue
                lookup[name.lower()] = eid
                try:
                    aliases = json.loads(aliases_json or "[]")
                except Exception:
                    aliases = []
                for a in aliases:
                    if a and isinstance(a, str):
                        lookup[a.lower()] = eid
    except Exception:
        return []

    matched_ids: set[int] = set()
    # Longest-first so multi-word names get matched before sub-words.
    for key in sorted(lookup.keys(), key=len, reverse=True):
        if len(key) < MIN_KEY_LEN:
            continue
        if key in GENERIC_SEED_BLOCKLIST:
            continue
        if re.search(rf"\b{re.escape(key)}\b", query_lower):
            matched_ids.add(lookup[key])

    return list(matched_ids)


def _build_adjacency() -> "tuple":
    """Build the adjacency matrix as a 2D list, plus index↔id maps.

    Returns (M, id_to_idx, idx_to_id, n).
    M is column-stochastic (each column sums to 1.0 — power iteration friendly).
    Treats relationships as undirected — symmetrized — because retrieval-PPR
    cares about reachability not causal direction.

    Implemented in pure Python rather than numpy to avoid a hard dep. At
    N=42 the perf difference is invisible. Revisit if N grows past ~500.
    """
    try:
        from kg import db
    except Exception as e:
        return [], {}, {}, 0

    now_ts = int(time.time())
    with db() as conn:
        ent_rows = conn.execute("SELECT id FROM entities").fetchall()
        ids = [r[0] for r in ent_rows]
        id_to_idx = {eid: i for i, eid in enumerate(ids)}
        idx_to_id = {i: eid for eid, i in id_to_idx.items()}
        n = len(ids)
        if n == 0:
            return [], {}, {}, 0

        # Edge accumulator as dict-of-dict (sparse to dense conversion below).
        W: dict[int, dict[int, float]] = {i: {} for i in range(n)}

        # Pull edges, attach recency-decayed confidence as weight.
        for row in conn.execute("""
            SELECT from_id, to_id, confidence, created
            FROM relationships
            WHERE (valid_to IS NULL OR valid_to = 0)
              AND (superseded_by IS NULL OR superseded_by = 0)
        """):
            from_id, to_id, conf, created = row
            if from_id not in id_to_idx or to_id not in id_to_idx:
                continue
            fi = id_to_idx[from_id]
            ti = id_to_idx[to_id]
            if fi == ti:
                continue
            age_days = max(0, (now_ts - (created or now_ts)) / 86400)
            decay = 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)
            w = max(0.05, (conf or 1.0) * decay)  # floor weight so we never erase old edges
            # Symmetrize — undirected for retrieval purposes.
            W[fi][ti] = W[fi].get(ti, 0.0) + w
            W[ti][fi] = W[ti].get(fi, 0.0) + w

    # Build dense matrix as list-of-lists, column-stochastic.
    M = [[0.0] * n for _ in range(n)]
    for col in range(n):
        col_total = 0.0
        for row in range(n):
            v = W[col].get(row, 0.0)
            M[row][col] = v
            col_total += v
        if col_total > 0:
            for row in range(n):
                M[row][col] /= col_total
        # Dangling column (isolated node) — distribute teleport later;
        # for now leave at 0. PPR teleport handles disconnected nodes naturally.

    return M, id_to_idx, idx_to_id, n


def _ppr(M, seeds_idx: list[int], n: int, alpha: float = ALPHA, iters: int = ITERS) -> list[float]:
    """Power-iteration PPR. Returns a length-n score vector."""
    if n == 0 or not seeds_idx:
        return [0.0] * n
    # Personalization vector: uniform over seeds.
    p = [0.0] * n
    w = 1.0 / len(seeds_idx)
    for s in seeds_idx:
        if 0 <= s < n:
            p[s] = w
    r = p.copy()
    for _ in range(iters):
        new_r = [alpha * p[i] for i in range(n)]
        for col in range(n):
            if r[col] == 0:
                continue
            for row in range(n):
                m_val = M[row][col]
                if m_val:
                    new_r[row] += (1 - alpha) * m_val * r[col]
        r = new_r
    return r


def search(query: str, top_k: int = 10) -> list[dict]:
    """Full retrieval path: query → seeds → PPR → top entities → memories.

    Returns list of dicts with keys: filename, ppr_score, via_entities.
    Empty list if no query entities matched (caller should treat as no-signal).
    """
    t0 = time.time()
    seeds = extract_query_entities(query)
    if not seeds:
        _log_telemetry({"event": "ppr_no_seeds", "query_len": len(query)})
        return []

    M, id_to_idx, idx_to_id, n = _build_adjacency()
    if n == 0:
        _log_telemetry({"event": "ppr_empty_graph"})
        return []

    seed_idxs = [id_to_idx[s] for s in seeds if s in id_to_idx]
    scores = _ppr(M, seed_idxs, n)

    # Sort entities by PPR score.
    ranked_entities = sorted(
        ((idx_to_id[i], s) for i, s in enumerate(scores) if s > MIN_SCORE_THRESHOLD),
        key=lambda x: -x[1],
    )

    # Map entities → memory files via entity_mentions.
    # A memory's score = sum(score of distinct entities it mentions).
    # Sum (not max) is key: a memory that mentions multiple high-PPR
    # entities should outrank one that mentions only the highest single
    # entity. This is what differentiates targeted retrieval from the
    # "everyone mentions the user" hub problem.
    memory_scores: dict[str, dict] = {}
    try:
        from kg import db
        # Pull mentions for all ranked entities in one shot.
        eid_list = [eid for eid, _ in ranked_entities[:50]]
        if not eid_list:
            return []
        with db() as conn:
            placeholders = ",".join("?" * len(eid_list))
            rows = conn.execute(
                f"SELECT entity_id, memory_filename FROM entity_mentions WHERE entity_id IN ({placeholders})",
                eid_list,
            ).fetchall()
        entity_score_lookup = dict(ranked_entities)
        # Track which entities contributed to each memory for telemetry/debug.
        contributing: dict[str, list[int]] = {}
        for eid, fn in rows:
            s = entity_score_lookup.get(eid, 0.0)
            if s <= 0:
                continue
            if fn not in memory_scores:
                memory_scores[fn] = {"filename": fn, "ppr_score": 0.0, "via_entities": []}
                contributing[fn] = []
            # Avoid double-counting if same (entity, memory) appears twice.
            if eid not in contributing[fn]:
                contributing[fn].append(eid)
                memory_scores[fn]["ppr_score"] += s
                memory_scores[fn]["via_entities"].append(eid)
    except Exception as e:
        _log_telemetry({"event": "ppr_mention_lookup_failed", "error": str(e)[:200]})
        return []

    ranked = sorted(memory_scores.values(), key=lambda x: -x["ppr_score"])[:top_k]

    elapsed_ms = round((time.time() - t0) * 1000, 1)
    _log_telemetry({
        "event": "ppr_ok",
        "n_seeds": len(seed_idxs),
        "n_entities": n,
        "n_memories_returned": len(ranked),
        "elapsed_ms": elapsed_ms,
    })

    return ranked


def main():
    """CLI for inspection.

    Usage:
      python kg_ppr.py stats              # graph snapshot
      python kg_ppr.py seeds <query>      # which entities does this query match?
      python kg_ppr.py search <query>     # full PPR search, top 10
    """
    if len(sys.argv) < 2:
        print("Usage: python kg_ppr.py stats | seeds <query> | search <query>")
        return

    cmd = sys.argv[1]

    if cmd == "stats":
        try:
            from kg import graph_stats
        except Exception as e:
            print(f"ERR: {e}")
            return
        print(json.dumps(graph_stats(), indent=2))
        return

    if cmd == "seeds":
        if len(sys.argv) < 3:
            print("Usage: python kg_ppr.py seeds <query>")
            return
        query = " ".join(sys.argv[2:])
        try:
            from kg import db
        except Exception as e:
            print(f"ERR: {e}")
            return
        seeds = extract_query_entities(query)
        if not seeds:
            print(f"No query entities matched: '{query}'")
            return
        with db() as conn:
            for eid in seeds:
                row = conn.execute("SELECT name, type FROM entities WHERE id=?", (eid,)).fetchone()
                print(f"  [{row[1]:8s}] {row[0]}  (id={eid})")
        return

    if cmd == "search":
        if len(sys.argv) < 3:
            print("Usage: python kg_ppr.py search <query>")
            return
        query = " ".join(sys.argv[2:])
        results = search(query, top_k=10)
        if not results:
            print("(no PPR results — no query entities matched, or empty graph)")
            return
        print(f"Top {len(results)} memories by PPR for: '{query}'")
        for i, r in enumerate(results, 1):
            print(f"  {i:2d}. {r['filename']:50s}  ppr={r['ppr_score']:.5f}")
        return

    print(f"Unknown command: {cmd}")


if __name__ == "__main__":
    main()
