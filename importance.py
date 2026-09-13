"""
importance.py — write-time importance scoring for v3.2 Phase 3.

For every memory written, an LLM rates lasting importance (1-10) based on:
  - Pattern vs one-off — is this a recurring theme or single moment?
  - Decision-informing vs record-keeping
  - Stability — philosophy/values (stable) vs config/state (volatile)

Scores accumulate per memory type. When `accumulated_score >= threshold` for
a type, fires a `reflection` event for the sleep agent (Phase 4) to consume.
Threshold default = 150 (Park et al. Generative Agents heuristic, scaled
for personal-memory write volume).

Routing: uses `importance_scoring` task type in tier_router → prefers
d'Artagnan local model, falls back to Haiku on health-check fail.

Content-hash caching: if the same (filename, content_hash) was already
scored, skip the LLM call entirely. Cosmetic edits won't burn tokens.

Failover at every step: scoring failure logs but never breaks the write
path. Worst case: a memory has no importance score yet — fine.
"""
from __future__ import annotations

import hashlib
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

MEMORY_DIR = _paths.MEMORY_DIR
META_DIR = MEMORY_DIR / "_meta"
TELEMETRY_PATH = META_DIR / "v3_2_telemetry.jsonl"

# Categories tracked in reflection_state — these match memory `type` field.
# Other types (e.g. 'reference', 'procedural') don't accumulate into reflection.
TRACKED_CATEGORIES = ("self", "feedback", "project", "user")
DEFAULT_THRESHOLD = 150
BODY_MAX_CHARS = 2000


SCORE_PROMPT = """Rate the lasting importance (1-10) of this memory for the user and Claude's collaboration.

Consider:
- Is this a recurring pattern or a one-off observation?
- Does this inform future decisions, or just record a single moment?
- Is the content stable (values, philosophy, architecture) or volatile (state, config, progress)?

A 10 is foundational — the user's core values, identity, key partnership principles.
A 1 is ephemeral — temporary state, one-time config, transient progress note.

Memory title: {name}
Memory description: {description}
Memory type: {memtype}
Memory body:
---
{body}
---

Output format: a single JSON object with two keys:
  "score": integer 1-10 (your rating)
  "reason": ONE short original sentence (do NOT copy this prompt text — describe THIS specific memory's content and why it earned that score)

Output ONLY the JSON object. No markdown, no preamble, no explanation outside the JSON."""


def _log_telemetry(record: dict) -> None:
    try:
        META_DIR.mkdir(parents=True, exist_ok=True)
        record.setdefault("ts", int(time.time()))
        record.setdefault("component", "importance")
        with TELEMETRY_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception:
        pass


def _content_hash(name: str, description: str, body: str) -> str:
    s = (name + "\n" + description + "\n" + body).encode("utf-8")
    return hashlib.md5(s).hexdigest()[:16]


def _get_cached_score(filename: str, content_hash: str) -> Optional[dict]:
    """If we've scored this exact content before, return it. Else None."""
    try:
        from memory_engine import db
    except Exception:
        return None
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT score, reasoning, scored_at, model_used FROM importance_scores "
                "WHERE filename=? AND content_hash=?",
                (filename, content_hash),
            ).fetchone()
        if not row:
            return None
        return {"score": row[0], "reason": row[1], "scored_at": row[2], "model": row[3], "cached": True}
    except Exception:
        return None


def _store_score(filename: str, content_hash: str, score: int, reason: str, model: str) -> None:
    try:
        from memory_engine import db
    except Exception:
        return
    try:
        with db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO importance_scores(filename, content_hash, score, reasoning, scored_at, model_used) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (filename, content_hash, int(score), reason[:500], int(time.time()), model),
            )
    except Exception:
        pass


def _parse_score_response(text: str) -> Optional[dict]:
    """Extract {score, reason} from the LLM's response. Tolerant of cruft."""
    if not text:
        return None
    # Find the first {...} that looks like JSON
    match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if not match:
        return None
    raw = match.group(0)
    try:
        data = json.loads(raw)
    except Exception:
        # Try a permissive single-quote conversion
        try:
            data = json.loads(raw.replace("'", '"'))
        except Exception:
            return None
    if not isinstance(data, dict):
        return None
    s = data.get("score")
    if s is None:
        return None
    try:
        s_int = int(s)
    except Exception:
        return None
    if not 1 <= s_int <= 10:
        return None
    return {"score": s_int, "reason": str(data.get("reason", ""))[:500]}


def score_memory(name: str, description: str, body: str, memtype: str, filename: str) -> Optional[dict]:
    """Score a memory's importance. Returns dict {score, reason, model, cached} or None on failure.

    Failover chain:
      1. Content-hash cache — if scored before with same content, return cached.
      2. Route via tier_router (prefers d'Artagnan local, falls back to Haiku).
      3. Parse LLM response into {score, reason}.
      4. Store in importance_scores table.
      5. On any error, return None — caller must treat as "no score yet."
    """
    if not filename:
        return None

    ch = _content_hash(name or "", description or "", body or "")

    # Cache check.
    cached = _get_cached_score(filename, ch)
    if cached:
        _log_telemetry({"event": "score_cache_hit", "filename": filename})
        return cached

    # Truncate body for prompt — anything past 2000 chars is noise for scoring purposes.
    body_for_prompt = (body or "")[:BODY_MAX_CHARS]
    prompt = SCORE_PROMPT.format(
        name=name or "(no name)",
        description=description or "(no description)",
        memtype=memtype or "(untyped)",
        body=body_for_prompt,
    )

    # Route + call. Use tier_router via provider abstraction.
    try:
        from tier_router import route, log_use
        from providers import get_provider
    except Exception as e:
        _log_telemetry({"event": "score_route_import_failed", "error": str(e)[:200]})
        return None

    try:
        provider_name, model, params = route("importance_scoring", input_tokens=len(prompt) // 4)
    except Exception as e:
        _log_telemetry({"event": "score_route_failed", "error": str(e)[:200]})
        return None

    try:
        provider = get_provider(provider_name)
    except Exception as e:
        _log_telemetry({"event": "score_provider_unavailable", "provider": provider_name, "error": str(e)[:200]})
        return None

    if provider is None:
        return None

    try:
        t0 = time.time()
        resp = provider.generate(prompt, model=model, max_tokens=params.get("max_tokens", 150))
        elapsed = round(time.time() - t0, 2)
    except Exception as e:
        _log_telemetry({
            "event": "score_generate_failed",
            "provider": provider_name,
            "model": model,
            "error": str(e)[:200],
        })
        return None

    parsed = _parse_score_response(resp.text)
    if not parsed:
        _log_telemetry({
            "event": "score_parse_failed",
            "raw_excerpt": (resp.text or "")[:200],
            "model": model,
        })
        return None

    _store_score(filename, ch, parsed["score"], parsed["reason"], f"{provider_name}:{model}")

    try:
        log_use("importance_scoring", model, tokens_in=resp.tokens_in or 0, tokens_out=resp.tokens_out or 0)
    except Exception:
        pass

    _log_telemetry({
        "event": "score_ok",
        "filename": filename,
        "score": parsed["score"],
        "model": f"{provider_name}:{model}",
        "elapsed_sec": elapsed,
    })

    return {**parsed, "model": f"{provider_name}:{model}", "cached": False}


def accumulate(category: str, score: int) -> dict:
    """Add a score to the category's running sum. If threshold tripped,
    emits a `reflection` event (consumed by Phase 4 sleep agent) and resets
    the accumulator. Returns dict describing what happened."""
    if category not in TRACKED_CATEGORIES:
        return {"skipped": True, "reason": f"untracked category: {category}"}

    try:
        from memory_engine import db
    except Exception as e:
        return {"error": f"db import: {e}"}

    now_ts = int(time.time())
    summary = {"category": category, "added": score, "threshold_tripped": False}

    try:
        with db() as conn:
            row = conn.execute(
                "SELECT accumulated_score, threshold FROM reflection_state WHERE category=?",
                (category,),
            ).fetchone()
            if not row:
                # Auto-create — migration should have seeded this but be defensive.
                conn.execute(
                    "INSERT INTO reflection_state(category, accumulated_score, threshold, last_score_added_ts) "
                    "VALUES (?, ?, ?, ?)",
                    (category, score, DEFAULT_THRESHOLD, now_ts),
                )
                new_total = score
                threshold = DEFAULT_THRESHOLD
            else:
                new_total = (row[0] or 0) + score
                threshold = row[1] or DEFAULT_THRESHOLD
                conn.execute(
                    "UPDATE reflection_state SET accumulated_score=?, last_score_added_ts=? WHERE category=?",
                    (new_total, now_ts, category),
                )

            summary["accumulated"] = new_total
            summary["threshold"] = threshold

            if new_total >= threshold:
                # Reset and emit reflection event for the sleep agent.
                conn.execute(
                    "UPDATE reflection_state SET accumulated_score=0, last_reflection_ts=? WHERE category=?",
                    (now_ts, category),
                )
                summary["threshold_tripped"] = True
                summary["new_total"] = 0
                try:
                    import event_bus
                    event_bus.emit_event(
                        "reflection",
                        {"category": category, "accumulated": new_total, "triggered_at": now_ts},
                    )
                    summary["event_emitted"] = True
                except Exception as e:
                    summary["event_emission_failed"] = str(e)[:200]

    except Exception as e:
        return {"error": str(e)[:200], "category": category}

    return summary


def get_score_distribution() -> dict:
    """Telemetry — current score distribution. For audit / sanity check."""
    try:
        from memory_engine import db
    except Exception:
        return {}
    with db() as conn:
        rows = conn.execute(
            "SELECT score, COUNT(*) FROM importance_scores GROUP BY score ORDER BY score"
        ).fetchall()
        by_type = conn.execute("""
            SELECT
              CASE
                WHEN filename LIKE 'self_%' THEN 'self'
                WHEN filename LIKE 'feedback_%' THEN 'feedback'
                WHEN filename LIKE 'user_%' THEN 'user'
                ELSE 'other'
              END as cat,
              AVG(score), COUNT(*)
            FROM importance_scores
            GROUP BY cat
        """).fetchall()
    return {
        "by_score": {r[0]: r[1] for r in rows},
        "by_type_avg": {r[0]: {"avg": round(r[1], 2), "n": r[2]} for r in by_type},
    }


def get_reflection_state() -> list[dict]:
    try:
        from memory_engine import db
    except Exception:
        return []
    with db() as conn:
        rows = conn.execute(
            "SELECT category, accumulated_score, threshold, last_reflection_ts, last_score_added_ts "
            "FROM reflection_state ORDER BY category"
        ).fetchall()
    return [
        {
            "category": r[0],
            "accumulated": r[1],
            "threshold": r[2],
            "last_reflection_ts": r[3],
            "last_score_added_ts": r[4],
        }
        for r in rows
    ]


def main():
    """CLI for inspection.

    Usage:
      python importance.py state              # reflection_state snapshot
      python importance.py dist               # score distribution
      python importance.py score <filename>   # score one memory now
      python importance.py backfill           # score all existing memories
    """
    if len(sys.argv) < 2:
        print("Usage: python importance.py state | dist | score <filename> | backfill")
        return

    cmd = sys.argv[1]

    if cmd == "state":
        print(json.dumps(get_reflection_state(), indent=2))
        return

    if cmd == "dist":
        print(json.dumps(get_score_distribution(), indent=2))
        return

    if cmd == "score":
        if len(sys.argv) < 3:
            print("Usage: python importance.py score <filename>")
            return
        filename = sys.argv[2]
        try:
            from memory_engine import load_memory, MEMORY_DIR
        except Exception as e:
            print(f"ERR: {e}")
            return
        path = MEMORY_DIR / filename
        if not path.exists():
            print(f"ERR: not found: {path}")
            return
        m = load_memory(path)
        result = score_memory(m.name, m.description, m.body, m.type, filename)
        print(json.dumps(result or {"error": "scoring failed (see telemetry)"}, indent=2))
        return

    if cmd == "backfill":
        try:
            from memory_engine import list_memories
        except Exception as e:
            print(f"ERR: {e}")
            return
        mems = list_memories()
        counts = {"scored": 0, "cached": 0, "failed": 0, "skipped": 0}
        for m in mems:
            if m.filename == "MEMORY.md":
                counts["skipped"] += 1
                continue
            result = score_memory(m.name, m.description, m.body, m.type, m.filename)
            if result is None:
                counts["failed"] += 1
                continue
            if result.get("cached"):
                counts["cached"] += 1
            else:
                counts["scored"] += 1
            print(f"  [{result['score']:2d}] {m.filename}  — {result.get('reason', '')[:80]}")
        print(f"\n{json.dumps(counts)}")
        return

    print(f"Unknown command: {cmd}")


if __name__ == "__main__":
    main()
