"""
observer_query.py - search interface over observations.db.

Phase 4 of plan_homemade_observer_v1. Called from prefetch.py when v3.2
retrieval scores low and the observer_fallback_enabled flag is on.

Surface:
    search(query, limit=3) -> list[dict]
        each dict: {
            session_id, ts, tool_name, excerpt, file_paths, score
        }

Ranking strategy (v1, simple):
    - FTS5 MATCH for relevance filter
    - ORDER BY ts DESC (recent observations score higher)
    - take limit rows
Future: blend BM25 score with recency decay.

Safe FTS5 querying:
    - special chars in user prompts ('.', '/', '-', '"', quotes) break FTS5
    - we extract clean tokens (alpha + digits) and phrase-quote anything iffy
    - empty / unusable query returns []

Hook contract: imported by prefetch.py, never called directly from a hook.
Standalone test:
    python observer_query.py "phase 0"
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    from observer_lib import ensure_db, get_connection, log_error
except Exception:
    sys.exit(0)

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


# FTS5 reserved tokens we never let through bare
FTS5_OPS = {"AND", "OR", "NOT", "NEAR"}


def _safe_fts5_term(term):
    """Wrap a term for safe FTS5 use.

    - quote anything with non-alnum chars
    - never let bare AND/OR/NOT/NEAR through (FTS5 operators)
    - escape embedded double quotes by doubling them
    """
    if not term:
        return None
    t = term.strip()
    if not t:
        return None
    if t.upper() in FTS5_OPS:
        return '"' + t + '"'
    # if it has any non-alphanumeric, phrase-quote it
    if not re.fullmatch(r"[A-Za-z0-9_]+", t):
        return '"' + t.replace('"', '""') + '"'
    return t


def _build_fts_query(prompt, max_terms=6):
    """Pull useful tokens from a prompt and OR them.

    OR (rather than AND) so partial matches still surface — the score filter
    in prefetch.py decides what's actually worth injecting.
    """
    if not prompt:
        return None
    # Extract alphanumeric tokens (length >= 3), ignore very common words
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", prompt.lower())
    STOP = {
        "the",
        "and",
        "for",
        "you",
        "are",
        "but",
        "not",
        "have",
        "this",
        "that",
        "what",
        "with",
        "from",
        "was",
        "were",
        "can",
        "would",
        "should",
        "could",
        "would",
        "will",
        "your",
        "did",
        "want",
        "let",
        "let's",
        "going",
        "now",
        "just",
        "really",
        "still",
        "also",
    }
    tokens = [t for t in tokens if t not in STOP]
    if not tokens:
        return None
    # dedupe preserving order
    seen = set()
    uniq = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
        if len(uniq) >= max_terms:
            break
    safe = [_safe_fts5_term(t) for t in uniq]
    safe = [s for s in safe if s]
    if not safe:
        return None
    return " OR ".join(safe)


def search(query, limit=3):
    """Return up to `limit` observation hits matching query, recent-first.

    Each hit dict has: session_id, ts, tool_name, excerpt, file_paths, score.
    Never raises — returns [] on any failure (fail-open into prefetch).
    """
    try:
        ensure_db()
        fts = _build_fts_query(query)
        if not fts:
            return []
        with get_connection() as conn:
            rows = conn.execute(
                """SELECT o.id, o.session_id, o.ts, o.tool_name,
                          o.tool_input_excerpt, o.tool_output_excerpt,
                          o.cmd_excerpt, o.file_paths
                   FROM observations_fts f
                   JOIN observations o ON o.id = f.rowid
                   WHERE observations_fts MATCH ?
                   ORDER BY o.ts DESC
                   LIMIT ?""",
                (fts, limit),
            ).fetchall()
        hits = []
        for r in rows:
            # Build a short excerpt — prefer cmd_excerpt, fall back to input/output
            excerpt = (
                r["cmd_excerpt"]
                or r["tool_input_excerpt"]
                or r["tool_output_excerpt"]
                or ""
            )
            excerpt = excerpt[:300]
            file_paths = []
            if r["file_paths"]:
                try:
                    file_paths = json.loads(r["file_paths"])
                except Exception:
                    pass
            hits.append(
                {
                    "id": r["id"],
                    "session_id": r["session_id"],
                    "ts": r["ts"],
                    "tool_name": r["tool_name"],
                    "excerpt": excerpt,
                    "file_paths": file_paths,
                    "score": 0.5,  # fixed trust weight per plan
                }
            )
        # v15: also search LLM-compressed session summaries (summaries_fts,
        # written by compress_sessions.py at sleep time) — semantic hits the
        # raw excerpts can't match. Defensive: table may not exist yet.
        try:
            with get_connection() as conn:
                srows = conn.execute(
                    """SELECT f.session_id, f.summary_text, s.started_at
                       FROM summaries_fts f
                       JOIN sessions s ON s.session_id = f.session_id
                       WHERE summaries_fts MATCH ?
                       ORDER BY s.started_at DESC LIMIT ?""",
                    (fts, limit),
                ).fetchall()
            for r in srows:
                hits.append(
                    {
                        "id": None,
                        "session_id": r[0],
                        "ts": r[2],
                        "tool_name": "session_summary",
                        "excerpt": (r[1] or "")[:300],
                        "file_paths": [],
                        "score": 0.55,  # compressed summaries slightly outrank raw
                    }
                )
            hits.sort(key=lambda h: -(h["ts"] or 0))
            hits = hits[:limit]
        except Exception:
            pass
        return hits
    except Exception as e:
        try:
            log_error("observer_query.search: " + str(e))
        except Exception:
            pass
        return []


def mark_referenced(observation_ids):
    """Bump referenced_count + last_referenced_at for surfaced observations.

    Called by prefetch.py after surfacing — fuels future auto-promotion logic.
    Never raises.
    """
    if not observation_ids:
        return
    try:
        import time

        now = int(time.time())
        with get_connection() as conn:
            for oid in observation_ids:
                conn.execute(
                    """UPDATE observations
                       SET referenced_count = referenced_count + 1,
                           last_referenced_at = ?
                       WHERE id = ?""",
                    (now, oid),
                )
            conn.commit()
    except Exception:
        pass


def format_for_prefetch(hits):
    """Render hit list as additionalContext lines for prefetch.py to append."""
    if not hits:
        return ""
    lines = ["[observation] additional signals from recent activity:"]
    for h in hits:
        bits = []
        if h["tool_name"]:
            bits.append(h["tool_name"])
        if h["file_paths"]:
            # show first 2 file paths
            paths_str = ", ".join(
                p.replace("\\", "/").rsplit("/", 1)[-1] for p in h["file_paths"][:2]
            )
            bits.append("files: " + paths_str)
        if h["excerpt"]:
            ex = h["excerpt"][:120].replace("\n", " ")
            bits.append(ex)
        line = "  - " + " | ".join(b for b in bits if b)
        lines.append(line)
    return "\n".join(lines)


# ---- standalone test ----
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python observer_query.py <query>")
        sys.exit(0)
    q = " ".join(sys.argv[1:])
    print("query:", q)
    print("fts5:", _build_fts_query(q))
    hits = search(q, limit=5)
    print("hits:", len(hits))
    for h in hits:
        print("  -", h["tool_name"], "|", h["excerpt"][:80])
    print("---")
    print("formatted for prefetch:")
    print(format_for_prefetch(hits))
