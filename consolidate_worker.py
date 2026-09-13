"""
consolidate_worker.py — sleep-time consolidation pass.

Handler for `kind="consolidate"` events from the v13 outbox. Runs slow,
LLM-routed work that shouldn't block the hot path:

  1. Beyond-hash dedup: cluster near-duplicate observations using cosine
     similarity on existing BGE embeddings, mark new corroborations
  2. Re-score memory weights based on access patterns over last 7d
  3. Identify stale FUNCTIONAL relationships that should be reviewed for
     potential supersession (heuristic: relationship hasn't been re-asserted
     in 60+ days, multiple parallel facts exist)
  4. Telemetry to _meta/consolidate_log.jsonl

This is read-mostly + carefully-write. Memory files are NEVER deleted; only
frontmatter is updated. SQLite KG tables get supersession marks, never row
deletes. The whole pass is idempotent.

Triggered:
  - At SessionEnd (1 job per session)
  - Manual: `python consolidate_worker.py run`
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
import paths as _paths  # noqa: E402

META_DIR = _paths.META_DIR
LOG_PATH = META_DIR / "consolidate_log.jsonl"

# Cosine similarity threshold for "near-duplicate" — high so we don't merge
# things that are merely related
NEAR_DUP_THRESHOLD = 0.92

# v14: worker-side coalesce guard (recon H5). The producer's 30-min cap
# (session_end._consolidate_due) can be bypassed by clock skew / backup restore /
# other enqueue paths, and the worker had NO guard — so a backlog of distinct
# consolidate jobs each ran the full heavy pass (the 5/29 runaway that fed the
# multi-week freeze; 207 bulk-cancelled jobs). Collapse them: skip the heavy pass
# if one completed within this window. A `force` payload flag bypasses it.
CONSOLIDATE_COALESCE_SEC = 1800  # 30 min
# A freshly-written marker's mtime can sit a hair AHEAD of time.time() (timer
# granularity, esp. on Windows), so allow a small negative delta as "just done".
# A grossly-future mtime (backup restore / clock jump) is NOT recent → run, so the
# guard can never wedge consolidation off permanently.
_CLOCK_SKEW_TOLERANCE_SEC = 60
_LAST_CONSOLIDATE_MARKER = META_DIR / ".last_worker_consolidate"


def _consolidate_recently_done(now: float) -> bool:
    """True if a worker consolidate completed within CONSOLIDATE_COALESCE_SEC.
    Missing/unreadable marker or a grossly-future mtime → not recent (never wedge:
    when in doubt, allow the pass)."""
    try:
        delta = now - _LAST_CONSOLIDATE_MARKER.stat().st_mtime
    except Exception:
        return False
    return -_CLOCK_SKEW_TOLERANCE_SEC <= delta < CONSOLIDATE_COALESCE_SEC


def _mark_consolidate_done() -> None:
    """Stamp the completion marker (mtime = now) so the next job within the
    coalesce window collapses to a no-op. Fail-soft."""
    try:
        META_DIR.mkdir(parents=True, exist_ok=True)
        _LAST_CONSOLIDATE_MARKER.write_text(str(int(time.time())))
    except Exception:
        pass


def _log(record: dict) -> None:
    try:
        META_DIR.mkdir(parents=True, exist_ok=True)
        record.setdefault(
            "ts", datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception:
        pass


def _near_duplicate_pass() -> dict:
    """Find memory pairs with cosine >= NEAR_DUP_THRESHOLD on their embeddings.

    For each pair: record a corroboration entry so future retrieval surfaces
    the stronger evidence. Doesn't merge or delete — just registers the link.
    """
    try:
        from memory_engine import list_memories, db
        from dedup import record_observation
        import math
    except Exception as e:
        return {"error": f"import: {e}"}

    counts = {"pairs_checked": 0, "near_dupes": 0, "corroborations_added": 0}
    try:
        mems = list_memories()
    except Exception as e:
        return {"error": f"list_memories: {e}"}

    # Get embeddings for all memories. memory_engine stores them as float32 BLOBs.
    embeddings = {}
    with db() as conn:
        for row in conn.execute("SELECT filename, dim, vector FROM embeddings"):
            try:
                fn, dim, vec_blob = row[0], row[1], row[2]
                import struct

                vec = list(struct.unpack(f"{dim}f", vec_blob))
                embeddings[fn] = vec
            except Exception:
                continue

    def _cosine(a, b):
        if not a or not b or len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        if na == 0 or nb == 0:
            return 0.0
        return dot / (na * nb)

    # O(N^2) pairwise comparison — fine at N=56, revisit at N=500
    filenames = list(embeddings.keys())
    name_by_fn = {m.filename: m.name for m in mems}
    type_by_fn = {m.filename: m.type for m in mems}
    desc_by_fn = {m.filename: m.description for m in mems}
    related_by_fn = {m.filename: set(m.related) for m in mems}

    conflict_pairs = []
    for i in range(len(filenames)):
        for j in range(i + 1, len(filenames)):
            counts["pairs_checked"] += 1
            sim = _cosine(embeddings[filenames[i]], embeddings[filenames[j]])
            if sim >= NEAR_DUP_THRESHOLD:
                counts["near_dupes"] += 1
                # Register cross-corroboration
                fn_a, fn_b = filenames[i], filenames[j]
                obs = {
                    "subject": name_by_fn.get(fn_a, fn_a),
                    "predicate": "near_duplicate_of",
                    "object": name_by_fn.get(fn_b, fn_b),
                    "kind": "consolidation",
                }
                try:
                    record_observation(obs, source=f"consolidate:cosine_{sim:.3f}")
                    counts["corroborations_added"] += 1
                except Exception:
                    pass
                # v15 (2026-07-09): near-dups that AREN'T intentional siblings
                # are tension pairs — record them in the conflicts table (which
                # had 0 rows ever) so they surface at boot for resolution.
                # Intentional: pairs already related: to each other, and
                # window-summary families (digests / session handoffs).
                intentional = (
                    fn_b in related_by_fn.get(fn_a, set())
                    or fn_a in related_by_fn.get(fn_b, set())
                    or (fn_a.startswith("digest_") and fn_b.startswith("digest_"))
                    or (
                        fn_a.startswith("session_handoff_")
                        and fn_b.startswith("session_handoff_")
                    )
                )
                if not intentional:
                    conflict_pairs.append((fn_a, fn_b, sim))

    try:
        from conflict_recorder import record_near_dups

        counts["conflicts"] = record_near_dups(conflict_pairs)
    except Exception as e:
        counts["conflicts"] = {"error": str(e)[:120]}

    return counts


def _weight_rerank_pass() -> dict:
    """Look at access frequency over last 7 days. Suggest weight changes
    for memories that are heavily accessed (promote to 'high') or never
    touched (suggest 'low'). Doesn't actually mutate files — produces a
    report at _meta/weight_suggestions.jsonl that you can review.
    """
    try:
        from memory_engine import list_memories, db
    except Exception as e:
        return {"error": f"import: {e}"}

    cutoff = int(time.time()) - 7 * 86400
    suggestions = []
    try:
        mems = list_memories()
    except Exception as e:
        return {"error": f"list_memories: {e}"}

    with db() as conn:
        for m in mems:
            row = conn.execute(
                "SELECT COUNT(*) FROM access WHERE filename=? AND ts>=?",
                (m.filename, cutoff),
            ).fetchone()
            n = row[0] if row else 0
            current = (m.weight or "").lower()
            suggested = None
            if n >= 5 and current != "high":
                suggested = "high"
            elif n == 0 and current == "" and m.age_days > 30:
                suggested = "low"
            if suggested:
                suggestions.append(
                    {
                        "filename": m.filename,
                        "current": current or "(none)",
                        "suggested": suggested,
                        "accesses_7d": n,
                    }
                )

    out_path = META_DIR / "weight_suggestions.jsonl"
    try:
        META_DIR.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as f:
            for s in suggestions:
                f.write(json.dumps(s) + "\n")
    except Exception:
        pass

    return {"suggestions": len(suggestions), "report": str(out_path)}


def _expiry_pass() -> dict:
    """Truth-maintenance: flag memories past their `expires:` date, plus
    type:reference memories that never got an expiry backfilled and have gone
    stale by age. Report-only (trust-gate pattern) — writes
    _meta/expiry_review.jsonl for /consolidate to surface as
    archive-or-refresh candidates. Never mutates memory files."""
    REFERENCE_STALE_DAYS = 90
    try:
        from memory_engine import list_memories
    except Exception as e:
        return {"error": f"import: {e}"}

    try:
        mems = list_memories()
    except Exception as e:
        return {"error": f"list_memories: {e}"}

    flagged = []
    for m in mems:
        if m.is_expired:
            flagged.append(
                {
                    "filename": m.filename,
                    "name": m.name,
                    "type": m.type or "",
                    "expires": m.expires,
                    "reason": f"past expires: {m.expires}",
                    "action": "refresh-or-archive",
                }
            )
        elif (
            (m.type or "") == "reference"
            and not m.expires
            and m.age_days > REFERENCE_STALE_DAYS
        ):
            flagged.append(
                {
                    "filename": m.filename,
                    "name": m.name,
                    "type": m.type,
                    "expires": None,
                    "reason": f"reference with no expires:, untouched {m.age_days}d",
                    "action": "backfill-expires",
                }
            )

    out_path = META_DIR / "expiry_review.jsonl"
    try:
        META_DIR.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as f:
            for row in flagged:
                f.write(json.dumps(row) + "\n")
    except Exception:
        pass

    return {"flagged": len(flagged), "report": str(out_path)}


DRAFT_TRIAGE_AGE_DAYS = 7


def _draft_triage_pass() -> dict:
    """Epilogue draft triage (truth maintenance): drafts are prep, not canon.
    weekly_digest.py already distills each week's epilogues (drafts included)
    into that week's digest chapter. So once a draft is older than
    DRAFT_TRIAGE_AGE_DAYS:
      - week digest exists  -> archive the draft to _meta/epilogues/archived/
                               (its signal lives in the digest; raw text stays
                               recoverable in the archive)
      - no digest for week  -> leave the draft, report the week as needing a
                               digest backfill (LLM work — not done here)
    Dates come from the draft filename, not mtime (mtime is restore-fragile)."""
    import re as _re

    date_re = _re.compile(r"draft-(\d{4})-(\d{2})-(\d{2})")
    epilogues_dir = META_DIR / "epilogues"
    digests_dir = META_DIR / "digests"
    archived_dir = epilogues_dir / "archived"
    counts = {"archived": 0, "kept_recent": 0}
    needs_digest: dict[str, int] = {}
    now = datetime.now()

    for p in sorted(epilogues_dir.glob("draft-*.md")):
        m = date_re.search(p.name)
        if not m:
            continue
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if (now - d).days <= DRAFT_TRIAGE_AGE_DAYS:
            counts["kept_recent"] += 1
            continue
        iso = d.isocalendar()
        week_str = f"{iso[0]}-W{iso[1]:02d}"
        if not (digests_dir / f"{week_str}.md").exists():
            needs_digest[week_str] = needs_digest.get(week_str, 0) + 1
            continue
        try:
            archived_dir.mkdir(parents=True, exist_ok=True)
            target = archived_dir / p.name
            if target.exists():
                target = archived_dir / (p.stem + "-dup" + p.suffix)
            p.rename(target)
            counts["archived"] += 1
        except Exception:
            pass  # a stuck file shouldn't block the rest

    if needs_digest:
        counts["weeks_needing_digest_backfill"] = needs_digest
    return counts


def _stale_functional_pass() -> dict:
    """Find functional relationships that haven't been re-asserted in 60+ days
    AND have parallel active versions — candidates for human review."""
    try:
        from memory_engine import db
        from bitemporal import FUNCTIONAL_RELS
    except Exception as e:
        return {"error": f"import: {e}"}

    stale_threshold = int(time.time()) - 60 * 86400
    suspects = []
    with db() as conn:
        for rel_type in FUNCTIONAL_RELS:
            rows = conn.execute(
                "SELECT from_id, COUNT(*) FROM relationships "
                "WHERE type = ? AND valid_to IS NULL AND superseded_by IS NULL "
                "AND created < ? "
                "GROUP BY from_id HAVING COUNT(*) > 1",
                (rel_type, stale_threshold),
            ).fetchall()
            for from_id, n in rows:
                ent = conn.execute(
                    "SELECT name FROM entities WHERE id=?", (from_id,)
                ).fetchone()
                if ent:
                    suspects.append(
                        {
                            "subject": ent[0],
                            "rel_type": rel_type,
                            "parallel_active_count": n,
                        }
                    )

    return {"stale_functional_suspects": len(suspects), "details": suspects[:20]}


# ─── v3.2 Phase 4: synthesis extension passes ─────────────────────────────

REFLECTION_CANDIDATES_DIR = META_DIR / "reflection_candidates"
REWRITE_CANDIDATES_DIR = META_DIR / "rewrite_candidates"


def _persist_failed_synthesis(raw_text: str, kind: str = "reflection") -> Path:
    """Save an unparseable LLM synthesis response for human salvage.

    On a JSON parse failure the importance accumulator for that window was
    already reset, so the distilled insight is otherwise lost with no record.
    Writing the raw text to _failed/ keeps it recoverable. Never raises.
    """
    failed_dir = REFLECTION_CANDIDATES_DIR / "_failed"
    try:
        failed_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{int(time.time())}-{kind}-{abs(hash(raw_text)) % 100000}.txt"
        path = failed_dir / fname
        path.write_text(raw_text, encoding="utf-8")
        return path
    except Exception:
        return failed_dir / "unwritten.txt"


# Tuning knobs.
TOP_K_MEMORIES_FOR_REFLECTION = (
    30  # how many memories of the category feed the synthesis
)
REFLECTION_MIN_INSIGHTS = 1
REFLECTION_MAX_INSIGHTS = 3
PREFETCH_TOP_K = 3  # memories per open thread
REWRITE_BODY_MIN_CHARS = 2000  # only compress if body is this long
REWRITE_MIN_AGE_DAYS = 60  # only compress if untouched this long


REFLECTION_SYNTHESIS_PROMPT = """You are reading {n_memories} memories about the partnership between the user and Claude, in the category "{category}".

These memories accumulated enough importance signal to trigger reflection — meaning the user and Claude have written a lot of substantive material in this area since the last reflection.

Your job: identify {min_insights}-{max_insights} HIGH-LEVEL patterns that emerge across these memories — patterns that would be worth capturing as a NEW memory because they aren't already captured at this level of abstraction.

Avoid:
- Restating individual memories
- Surface-level summaries
- Insights already covered by an existing memory in the input

For each insight, propose:
  - title: short, evocative (5-8 words)
  - description: one-line summary for retrieval indexing
  - body: 2-4 paragraphs in the user's voice register — casual-direct, peer-to-peer, no corporate tone, no marketing speak. Use lowercase opener. Write as if explaining to the next Claude session.

Memories (most important first):

{memory_excerpts}

Output format: ONE JSON object with key "insights" mapped to a list of objects, each with keys "title", "description", "body". Output ONLY the JSON, no preamble, no markdown fences."""


REWRITE_PROMPT = """Rewrite this memory more concisely WITHOUT LOSING MEANING.

Constraints:
- Preserve all factual content
- Preserve names, dates, paths, technical details exactly
- Preserve the user's voice register (casual-direct, peer-to-peer)
- Cut redundancy, repetition, filler
- Target ~50% of the original length

Output the rewritten body ONLY. No preamble, no explanation. The frontmatter will be preserved automatically — do not include it.

Original body:
---
{body}
---"""


def _slug(text: str) -> str:
    """Filename-safe slug from a title."""
    import re as _re

    s = _re.sub(r"[^a-zA-Z0-9_-]+", "_", text.lower()).strip("_")
    return s[:40] or "untitled"


def _reflection_synthesis_pass(category: str) -> dict:
    """LLM-synthesize 1-3 candidate self_*.md drafts from accumulated memories.

    Triggered when Phase 3 fires a `reflection` event for `category`. NEVER
    writes directly to memory dir — drafts go to _meta/reflection_candidates/
    for human approval via /consolidate."""
    counts = {"category": category, "candidates_written": 0, "skipped": False}
    try:
        from memory_engine import list_memories, db
        from providers import get_provider
        from tier_router import route, log_use
    except Exception as e:
        return {"error": f"import: {e}"}

    # Pull memories of this category, ranked by recent importance score desc.
    try:
        mems = list_memories()
    except Exception as e:
        return {"error": f"list_memories: {e}"}

    if category == "self":
        type_filter = lambda m: m.type == "self"
    elif category == "feedback":
        type_filter = lambda m: m.type == "feedback"
    elif category == "user":
        type_filter = lambda m: m.type == "user"
    elif category == "project":
        type_filter = lambda m: m.type == "project"
    else:
        return {"skipped": True, "reason": f"unknown category: {category}"}

    candidates = [m for m in mems if type_filter(m)]
    if len(candidates) < 3:
        counts["skipped"] = True
        counts["reason"] = f"too few memories in category ({len(candidates)} < 3)"
        return counts

    # Rank by latest importance score for this filename (most recent scoring wins).
    score_lookup: dict[str, int] = {}
    try:
        with db() as conn:
            for row in conn.execute(
                "SELECT filename, score FROM importance_scores ORDER BY scored_at DESC"
            ):
                score_lookup.setdefault(row[0], row[1])
    except Exception:
        pass

    candidates.sort(key=lambda m: score_lookup.get(m.filename, 5), reverse=True)
    candidates = candidates[:TOP_K_MEMORIES_FOR_REFLECTION]

    # Build the memory excerpts block.
    excerpts = []
    for m in candidates:
        s = score_lookup.get(m.filename, "?")
        body_preview = (m.body or "")[:600].strip().replace("\n", " ")
        excerpts.append(
            f"[score {s}] {m.name} ({m.filename})\n  desc: {m.description}\n  body: {body_preview}"
        )
    excerpts_block = "\n\n".join(excerpts)

    prompt = REFLECTION_SYNTHESIS_PROMPT.format(
        n_memories=len(candidates),
        category=category,
        min_insights=REFLECTION_MIN_INSIGHTS,
        max_insights=REFLECTION_MAX_INSIGHTS,
        memory_excerpts=excerpts_block,
    )

    # Route — synthesis benefits from a stronger model than fast-4b. Use
    # multi_doc_synthesis tier (Opus) when available, but we're sleep-time
    # so the slow_acceptable flag is fine.
    try:
        provider_name, model, params = route("multi_doc_synthesis")
    except Exception as e:
        return {"error": f"route: {e}"}

    # Synthesis routes to a strong model (Opus/Sonnet). If the cloud provider
    # is unavailable, we used to fall back to dart-brain (30B Q4), but that
    # spills heavily on the 8GB 4070 Laptop (~18GB needed, ~10GB spill to RAM).
    # Cleaner degradation: skip this round. The reflection event stays in the
    # outbox for next pass; synthesis is sleep-time work, losing one cycle is
    # fine. Re-fires next time consolidation runs.
    try:
        provider = get_provider(provider_name)
        if not provider.health_check():
            _log(
                {
                    "event": "reflection_synthesis_skipped_provider_unhealthy",
                    "category": category,
                    "provider": provider_name,
                }
            )
            return {
                "skipped": True,
                "reason": f"{provider_name} unhealthy — try again next cycle",
            }
    except Exception as e:
        return {"error": f"provider: {e}"}

    try:
        t0 = time.time()
        resp = provider.generate(prompt, model=model, max_tokens=4096, temperature=0.5)
        elapsed = round(time.time() - t0, 1)
    except Exception as e:
        return {"error": f"generate: {e}"}

    # Parse the response — accept either bare JSON or fenced JSON.
    import re as _re

    txt = resp.text or ""
    # Strip code fences if any
    txt = _re.sub(r"^```(?:json)?\s*", "", txt.strip())
    txt = _re.sub(r"\s*```$", "", txt)
    try:
        parsed = json.loads(txt)
    except Exception:
        # Try to find the first { ... } that parses
        match = _re.search(r"\{.*\}", txt, _re.DOTALL)
        if not match:
            saved = _persist_failed_synthesis(txt, "reflection")
            return {
                "error": "could not parse LLM output",
                "preview": txt[:200],
                "saved": str(saved),
            }
        try:
            parsed = json.loads(match.group(0))
        except Exception as e:
            saved = _persist_failed_synthesis(txt, "reflection")
            return {
                "error": f"json parse: {e}",
                "preview": txt[:200],
                "saved": str(saved),
            }

    insights = parsed.get("insights", [])
    if not isinstance(insights, list):
        return {"error": "no insights list in response", "preview": txt[:200]}

    REFLECTION_CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    now_ts_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    today = datetime.now().strftime("%Y%m%d")

    # Semantic dedup for each insight. Before 2026-07-30 the only guard here
    # was a same-day filename check, so every sleep cycle on a new day could
    # re-derive an insight already sitting in the queue — that's how one
    # lesson accumulated 38 pending copies over three weeks. Fail-soft: if
    # the dedup stack won't import, write like before (dupes over data loss).
    try:
        from candidate_dedup import (
            EXISTING_COVERED_THRESHOLD,
            PENDING_DUP_THRESHOLD,
            max_similarity_to_existing,
            max_similarity_to_pending,
            record_corroboration,
        )
        from memory_engine import embed_batch as _embed_batch

        _dedup_ready = True
    except Exception:
        _dedup_ready = False
    counts["skipped_already_covered"] = 0
    counts["skipped_pending_duplicate"] = 0

    for ins in insights:
        title = ins.get("title", "")
        desc = ins.get("description", "")
        body = ins.get("body", "")
        if not title or not body:
            continue

        if _dedup_ready:
            ivec = None
            try:
                vecs = _embed_batch([f"{title}\n{desc}\n{body}"[:2500]])
                ivec = vecs[0] if vecs else None
            except Exception:
                ivec = None
            if ivec is not None:
                ex_sim, ex_file = max_similarity_to_existing(ivec)
                if ex_sim >= EXISTING_COVERED_THRESHOLD:
                    counts["skipped_already_covered"] += 1
                    _log(
                        {
                            "event": "reflection_insight_already_covered",
                            "category": category,
                            "title": title[:80],
                            "existing": ex_file,
                            "sim": round(ex_sim, 3),
                        }
                    )
                    continue
                # Called per-insight (not hoisted) on purpose: insight 1's
                # freshly written file must be visible when insight 2 checks.
                p_sim, p_path = max_similarity_to_pending(ivec)
                if p_sim >= PENDING_DUP_THRESHOLD and p_path is not None:
                    counts["skipped_pending_duplicate"] += 1
                    record_corroboration(p_path)
                    _log(
                        {
                            "event": "reflection_insight_pending_duplicate",
                            "category": category,
                            "title": title[:80],
                            "pending": p_path.name,
                            "sim": round(p_sim, 3),
                        }
                    )
                    continue

        slug = _slug(title)
        out_path = REFLECTION_CANDIDATES_DIR / f"{today}_{category}_{slug}.md"
        if out_path.exists():
            # Don't clobber an existing candidate — append a suffix
            i = 2
            while out_path.exists():
                out_path = (
                    REFLECTION_CANDIDATES_DIR / f"{today}_{category}_{slug}_v{i}.md"
                )
                i += 1
        frontmatter = (
            "---\n"
            f"name: {title}\n"
            f"description: {desc}\n"
            f"type: {category}\n"
            "status: draft\n"
            "awaiting_approval: true\n"
            f"source_category: {category}\n"
            f"synthesized_at: {now_ts_iso}\n"
            f"synthesized_by: {provider_name}:{model}\n"
            f"n_source_memories: {len(candidates)}\n"
            "---\n"
        )
        with out_path.open("w", encoding="utf-8") as f:
            f.write(frontmatter + "\n" + body.strip() + "\n")
        counts["candidates_written"] += 1

    try:
        log_use(
            "multi_doc_synthesis",
            model,
            tokens_in=resp.tokens_in or 0,
            tokens_out=resp.tokens_out or 0,
        )
    except Exception:
        pass

    counts["model"] = f"{provider_name}:{model}"
    counts["elapsed_sec"] = elapsed
    counts["candidates_dir"] = str(REFLECTION_CANDIDATES_DIR)
    return counts


def _predictive_prefetch_pass() -> dict:
    """Pre-warm the read_cache with memories likely needed for open threads.

    No LLM call — pure embedding similarity. Read open_threads.md, embed each
    thread description, find top-K most-similar memories, ensure their
    metadata is cached in read_cache.sqlite for fast retrieval next session."""
    counts = {"threads_checked": 0, "memories_prefetched": 0}
    try:
        from memory_engine import list_memories, embed_text, db, _blob_to_vec, _cosine
    except Exception as e:
        return {"error": f"import: {e}"}

    threads_path = META_DIR / "open_threads.md"
    if not threads_path.exists():
        return {"skipped": True, "reason": "no open_threads.md"}

    try:
        content = threads_path.read_text(encoding="utf-8")
    except Exception:
        return {"skipped": True, "reason": "could not read open_threads.md"}

    # Parse open threads — naive: each non-empty line that starts with - or *
    # treat as a thread description. Skip headers (## ...).
    import re as _re

    threads = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _re.match(r"^[-*]\s+(.+)$", line)
        if m:
            text = m.group(1).strip()
            if len(text) > 10:
                threads.append(text)

    if not threads:
        return {"skipped": True, "reason": "no thread descriptions parsed"}

    mems = list_memories()
    by_name = {m.filename: m for m in mems}
    # Load all embeddings once.
    emb_lookup: dict[str, list] = {}
    try:
        with db() as conn:
            for row in conn.execute("SELECT filename, vector FROM embeddings"):
                emb_lookup[row[0]] = _blob_to_vec(row[1])
    except Exception:
        return {"error": "could not load embeddings"}

    prefetched_set: set[str] = set()
    for thread in threads:
        counts["threads_checked"] += 1
        qvec = embed_text(thread)
        if qvec is None:
            continue
        scored = []
        for fn, vec in emb_lookup.items():
            if fn not in by_name:
                continue
            scored.append((fn, _cosine(qvec, vec)))
        scored.sort(key=lambda x: -x[1])
        for fn, _ in scored[:PREFETCH_TOP_K]:
            prefetched_set.add(fn)

    # Touch read_cache.sqlite — populate or refresh entries for these memories.
    # The read_gate uses this as a "warm" set. Simplest impact: update an
    # mtime so the read_gate doesn't have to fresh-load them.
    cache_path = META_DIR / "read_cache.sqlite"
    try:
        import sqlite3

        with sqlite3.connect(cache_path) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS prefetch_warm (
                    filename TEXT PRIMARY KEY,
                    warmed_at INTEGER NOT NULL
                );
            """)
            now_ts = int(time.time())
            for fn in prefetched_set:
                conn.execute(
                    "INSERT OR REPLACE INTO prefetch_warm(filename, warmed_at) VALUES (?, ?)",
                    (fn, now_ts),
                )
    except Exception as e:
        return {"error": f"prefetch cache write: {e}"}

    counts["memories_prefetched"] = len(prefetched_set)
    counts["sample"] = sorted(prefetched_set)[:10]
    return counts


def _memory_rewrite_pass() -> dict:
    """Compress stale verbose memories to ~50% length. Drafts only — never
    overwrites the source. Outputs to _meta/rewrite_candidates/ as .diff files
    for human review via /consolidate."""
    counts = {"checked": 0, "candidates_written": 0}
    try:
        from memory_engine import list_memories
        from providers import get_provider
        from tier_router import route, log_use
    except Exception as e:
        return {"error": f"import: {e}"}

    try:
        mems = list_memories()
    except Exception as e:
        return {"error": f"list_memories: {e}"}

    # Find candidates: long body, old, not high-weight, not self_*
    eligible = []
    for m in mems:
        if not m.body or len(m.body) < REWRITE_BODY_MIN_CHARS:
            continue
        if m.age_days < REWRITE_MIN_AGE_DAYS:
            continue
        if (m.weight or "").lower() == "high":
            continue
        if m.type == "self":
            continue
        if m.filename.startswith("digest_"):
            continue  # auto-generated, will refresh on its own
        eligible.append(m)

    if not eligible:
        return {"skipped": True, "reason": "no eligible memories"}

    # Cap pass size — don't rewrite everything in one go
    eligible = eligible[:5]

    try:
        provider_name, model, params = route("summary_short")
        provider = get_provider(provider_name)
        if not provider.health_check():
            provider = get_provider("dartagnan")
            if not provider.health_check():
                return {"error": "no provider healthy"}
            provider_name = "dartagnan"
            model = "dart-e2b"
    except Exception as e:
        return {"error": f"provider: {e}"}

    REWRITE_CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)

    import difflib

    for m in eligible:
        counts["checked"] += 1
        prompt = REWRITE_PROMPT.format(body=m.body)
        try:
            resp = provider.generate(
                prompt, model=model, max_tokens=2048, temperature=0.3
            )
        except Exception:
            continue
        new_body = (resp.text or "").strip()
        if not new_body or len(new_body) >= len(m.body):
            continue  # didn't actually shorten
        # Strip any ``` fences
        import re as _re

        new_body = _re.sub(r"^```[a-z]*\s*", "", new_body)
        new_body = _re.sub(r"\s*```$", "", new_body)
        diff = "".join(
            difflib.unified_diff(
                m.body.splitlines(keepends=True),
                new_body.splitlines(keepends=True),
                fromfile=f"{m.filename} (original)",
                tofile=f"{m.filename} (proposed)",
                n=3,
            )
        )
        if not diff.strip():
            continue
        out_path = REWRITE_CANDIDATES_DIR / f"{m.filename}.diff"
        with out_path.open("w", encoding="utf-8") as f:
            f.write(
                f"# Proposed rewrite — {m.filename}\n"
                f"# Original: {len(m.body)} chars\n"
                f"# Proposed: {len(new_body)} chars ({100 * (1 - len(new_body)/len(m.body)):.0f}% shorter)\n"
                f"# Synthesized by: {provider_name}:{model}\n\n" + diff
            )
        counts["candidates_written"] += 1

    return counts


def process_reflection(payload: dict | None = None) -> dict:
    """Entry point for `reflection` event handled by outbox_worker.
    Payload comes from importance.accumulate() when threshold trips."""
    payload = payload or {}
    category = payload.get("category")
    if not category:
        return {"error": "no category in payload"}
    started = time.time()
    summary = {"category": category}
    summary["synthesis"] = _reflection_synthesis_pass(category)
    # Subconscious triage: after synthesis lands in the queue, dart2 reads the
    # pending pile and annotates recommendations for the human drain. Advice
    # only — never promotes. Fail-soft: dart2 down = untriaged, not broken.
    try:
        from triage_candidates import triage_all

        summary["triage"] = triage_all()
    except Exception as e:
        summary["triage"] = {"error": str(e)[:120]}
    summary["duration_sec"] = round(time.time() - started, 2)
    _log(summary)
    return summary


def _session_id_for_log(payload: dict) -> str:
    """Session id for telemetry. Cron/CLI-enqueued consolidates carry no
    session_id in the payload, which used to log as "?" — fall back to
    observer_lib's resolution chain (env var, session-id file) instead."""
    sid = payload.get("session_id")
    if sid:
        return str(sid)
    try:
        from observer_lib import resolve_session_id

        return resolve_session_id(payload)
    except Exception:
        return f"pid-{os.getpid()}"


def process_consolidate(payload: dict | None = None) -> dict:
    """Main entry point — called by outbox_worker when handling a consolidate event."""
    payload = payload or {}
    started = time.time()
    # H5: collapse a backlog of consolidate jobs to one per coalesce window. A
    # manual `force` (CLI `run`) always proceeds.
    if not payload.get("force") and _consolidate_recently_done(started):
        summary = {
            "session_id": _session_id_for_log(payload),
            "skipped": "coalesced",
            "coalesce_window_sec": CONSOLIDATE_COALESCE_SEC,
        }
        _log(summary)
        return summary
    summary = {"session_id": _session_id_for_log(payload)}
    # BIGBUFF 2.0 P2 (D6-07/D6-02): pre-pass candidate inventory, logged BEFORE
    # any pass touches files. The 8/2 drain survived only as an ad-hoc manifest
    # because the pass crashed at max-turns, and the ~104 May drafts vanished
    # with no record at all. With this line, any disappearance between passes
    # is reconstructible from consolidate_log.jsonl even through a crash.
    try:
        _meta = _paths.META_DIR
        _log(
            {
                "event": "pre_pass_candidate_inventory",
                "reflection_candidates": sorted(
                    p.name for p in (_meta / "reflection_candidates").glob("*.md")
                ),
                "promotion_candidates": sorted(
                    p.name
                    for p in (_meta / "promotion_candidates").glob("**/*.md")
                    if "_promoted" not in p.parts and "rejected" not in p.parts
                ),
                "draft_epilogues": len(list((_meta / "epilogues").glob("draft-*.md"))),
            }
        )
    except Exception:
        pass
    # B7/R1: run all passes inside try/finally so the H5 coalesce marker is
    # ALWAYS stamped — even if an (unwrapped) legacy pass raises. Without this,
    # a pass throw skips the stamp and the outbox re-queues the whole heavy
    # chain (the multi-week freeze pattern).
    try:
        summary["near_dup"] = _near_duplicate_pass()
        summary["weight_rerank"] = _weight_rerank_pass()
        summary["stale_functional"] = _stale_functional_pass()
        summary["expiry"] = _expiry_pass()
        summary["draft_triage"] = _draft_triage_pass()
        # v3.2 Phase 4 — synthesis extension passes (flag-gated).
        try:
            from feature_flags import is_enabled

            if is_enabled("synthesis_passes_enabled"):
                summary["prefetch"] = _predictive_prefetch_pass()
                # rewrite pass runs only when explicitly requested via CLI for
                # now — it's the most expensive of the three (LLM calls per
                # candidate). Will wire to a weekly cadence later.
        except Exception:
            pass
        # v14.2: generate observer->spine promotion CANDIDATES (heuristic, no LLM,
        # idempotent, staging-only — NEVER auto-promotes). Surfaces reused
        # observations for human review instead of re-deriving them every session.
        try:
            from promotion_candidate_generator import run_generation

            summary["observer_candidates"] = run_generation()
        except Exception as e:
            summary["observer_candidates"] = {"error": str(e)[:120]}
        # v2.5 (2026-07-09): auto-accept graduation — pending observer candidates
        # that clear the gates (score, 24h age, unresolved obs) promote straight
        # into memory/ in spine format. Gated by auto_promote_high_confidence_enabled;
        # the user explicitly closed the trust window ("make it so it auto promotes").
        try:
            from promotion_candidate_generator import auto_accept

            summary["observer_auto_accept"] = auto_accept()
        except Exception as e:
            summary["observer_auto_accept"] = {"error": str(e)[:120]}
        # v15 (2026-07-09): semantic compression of ended sessions — the
        # claude-mem-density fix, at sleep time instead of the hot path.
        # Local-first (d'Artagnan) via observation_compression tier; capped
        # per cycle so the backlog amortizes. Fail-soft.
        try:
            import compress_sessions

            summary["compress_sessions"] = compress_sessions.run()
        except Exception as e:
            summary["compress_sessions"] = {"error": str(e)[:120]}
        # v15 (2026-07-09): reconsolidation — hot-but-stale memories (heavy
        # recall, old mtime) get re-synthesized with epilogue evidence that
        # accumulated since. Auto for project/reference only; identity types
        # (self/user/feedback) are flagged for human refresh, never rewritten.
        # LLM-backed, capped at 2/cycle, provenance snapshot before each.
        try:
            import reconsolidate

            summary["reconsolidate"] = reconsolidate.run()
        except Exception as e:
            summary["reconsolidate"] = {"error": str(e)[:120]}
        # v15 (2026-07-09): auto-backlinking — under-linked memories get their
        # top embedding neighbors appended to related: (append-only, capped,
        # threshold-gated). Densifies the spine graph the PPR/viz walk.
        try:
            import backlink

            bl = backlink.run(write=True)
            bl.pop("suggestions", None)
            summary["backlink"] = bl
        except Exception as e:
            summary["backlink"] = {"error": str(e)[:120]}
        # v15 (2026-07-09): Hebbian pass — re-mine co-recall synapses from the
        # access log (deterministic rebuild + synaptic pruning). Feeds the
        # 5th RRF fusion signal in search_hybrid. Cheap (no LLM), fail-soft.
        try:
            import hebbian

            summary["hebbian"] = hebbian.rebuild(valid_files=hebbian.live_files())
        except Exception as e:
            summary["hebbian"] = {"error": str(e)[:120]}
        # v15: hallucination audit — structural provenance check (orphan
        # embeddings, phantom index entries, dead synapses). Telemetry-only.
        try:
            from benchmark_gen import hallucination_audit

            summary["hallucination_audit"] = hallucination_audit()
        except Exception as e:
            summary["hallucination_audit"] = {"error": str(e)[:120]}
        # Epilogue->semantic clustering (auto_promote.py) was designed for a weekly
        # cadence but never scheduled — the 4-week trust telemetry could never
        # accumulate (root cause, 2026-07-09). Runs here now: HIGH-tier clusters
        # auto-promote to memory/ when the flag is ON; MEDIUM stay as candidates.
        try:
            from auto_promote import find_candidates as _epilogue_promote

            summary["auto_promote"] = _epilogue_promote(write_drafts=True)
        except Exception as e:
            summary["auto_promote"] = {"error": str(e)[:120]}
        # v14.2: prune the unbounded provenance snapshot store (keeps oldest per
        # memory). Quiet so it can't pollute a hook's stdout. Fail-soft.
        try:
            from provenance import prune as _prune_prov

            summary["provenance_prune"] = _prune_prov(days=30, quiet=True)
        except Exception as e:
            summary["provenance_prune"] = {"error": str(e)[:120]}
        # procedural (L2) designer pass — mine error->recovery + feedback + epilogue
        # lessons into heuristics. Gated by procedural_extraction_enabled (checked
        # inside run_designer, default OFF). Sleep-time, LLM-backed, fail-soft.
        try:
            import procedural_lib
            from observer_lib import get_connection as _obs_conn_fn
            from pathlib import Path as _P

            _mem_dir = _P(__file__).resolve().parent.parent
            with _obs_conn_fn() as _oc:
                summary["procedural"] = procedural_lib.run_designer(
                    obs_conn=_oc,
                    memory_dir=_mem_dir,
                    epilogue_dir=_mem_dir / "_meta" / "epilogues",
                )
        except Exception as e:
            summary["procedural"] = {"error": str(e)[:120]}
        # v15 (2026-07-09): zero-touch maintenance — orphan-session sweep,
        # VACUUM/ANALYZE, floor-benchmark diff (retrieval-rot detection).
        # Fail-soft; the sentinel surfaces floor regressions at boot.
        try:
            import maintenance

            summary["maintenance"] = maintenance.run()
        except Exception as e:
            summary["maintenance"] = {"error": str(e)[:120]}
        # Spine Genesis — single-session novelty -> promotion CANDIDATE (n=1,
        # candidates-only, gated by genesis_enabled, default OFF). Fail-soft.
        try:
            import genesis

            summary["genesis"] = genesis.run()
        except Exception as e:
            summary["genesis"] = {"error": str(e)[:120]}
        # BIGBUFF 2.0 P2 (D1-02): regenerate MEMORY.md LAST, so everything this
        # cycle wrote becomes index-visible in the same run. The 8/2 sleep cycle
        # created 5 memories that stayed invisible for 3 days because nothing
        # auto-regenerated the index and the genesis pass then self-disabled as
        # index_unhealthy. Fail-soft like every other pass.
        try:
            import regenerate_memory_index as _regen_idx
            from memory_engine import list_memories as _list_mems

            _idx = _regen_idx.INDEX_FILE
            _cur = (
                _idx.read_text(encoding="utf-8")
                if _idx.exists()
                else "# Memory Index\n\n_(curated sections to be filled in by hand)_\n\n"
            )
            _mems = _list_mems()
            _idx.write_text(
                _regen_idx.splice(_cur, _regen_idx.render_index(_mems)),
                encoding="utf-8",
            )
            summary["index_regen"] = {"indexed": len(_mems)}
        except Exception as e:
            summary["index_regen"] = {"error": str(e)[:120]}
    finally:
        _mark_consolidate_done()  # H5/B7: always stamp, even if a pass raised
    summary["duration_sec"] = round(time.time() - started, 2)
    _log(summary)
    return summary


def main():
    ap = argparse.ArgumentParser(description="Sleep-time consolidation worker")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run", help="Run a consolidation pass now")
    sub.add_parser("near-dup", help="Near-duplicate pass only")
    sub.add_parser("weights", help="Weight rerank pass only")
    sub.add_parser("stale", help="Stale functional pass only")
    sub.add_parser("expiry", help="Expiry / truth-maintenance pass only")
    sub.add_parser("draft-triage", help="Epilogue draft triage pass only")
    p_reflect = sub.add_parser("reflect", help="Reflection synthesis pass (Phase 4)")
    p_reflect.add_argument("category", choices=("self", "feedback", "project", "user"))
    sub.add_parser("prefetch", help="Predictive prefetch pass (Phase 4)")
    sub.add_parser("rewrite", help="Memory rewrite pass (Phase 4)")
    args = ap.parse_args()

    if args.cmd == "run":
        print(
            json.dumps(process_consolidate({"force": True}), indent=2)
        )  # manual run always proceeds
    elif args.cmd == "near-dup":
        print(json.dumps(_near_duplicate_pass(), indent=2))
    elif args.cmd == "weights":
        print(json.dumps(_weight_rerank_pass(), indent=2))
    elif args.cmd == "stale":
        print(json.dumps(_stale_functional_pass(), indent=2))
    elif args.cmd == "expiry":
        print(json.dumps(_expiry_pass(), indent=2))
    elif args.cmd == "draft-triage":
        print(json.dumps(_draft_triage_pass(), indent=2))
    elif args.cmd == "reflect":
        print(json.dumps(_reflection_synthesis_pass(args.category), indent=2))
    elif args.cmd == "prefetch":
        print(json.dumps(_predictive_prefetch_pass(), indent=2))
    elif args.cmd == "rewrite":
        print(json.dumps(_memory_rewrite_pass(), indent=2))
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
