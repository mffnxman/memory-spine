"""candidate_dedup.py — semantic dedup against PENDING candidates.

The 38x lesson (2026-07-30 drain): every synthesis lane deduped its output
against existing durable memories only. Anything sitting in a candidate queue
awaiting approval was invisible to the dedup check, so the sleep cycle
re-derived the same iron-law insight 38 times over three weeks — one new file
per cycle, shouting into a queue nobody read.

This module gives all lanes one shared view of what is already pending:

  - pending_candidate_paths()       — every .md still awaiting review
  - max_similarity_to_pending(vec)  — closest pending candidate by cosine
  - max_similarity_to_existing(vec) — closest durable memory by cosine
  - record_corroboration(path)      — bump a counter on a pending candidate
                                      instead of writing a duplicate; the
                                      re-derivation IS importance signal and
                                      the reviewer should see it

Embeddings are cached in _meta/candidate_embed_cache.json keyed by
(path, mtime) so repeat runs don't re-embed unchanged files. Everything is
fail-soft: on any error the caller sees "no duplicate" and proceeds, which
degrades to the old (duplicate-producing but safe) behavior.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path
import sys as _sys  # noqa: E402
_sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths as _paths  # noqa: E402
from typing import Optional

MEMORY_DIR = _paths.MEMORY_DIR
META_DIR = MEMORY_DIR / "_meta"
PROMOTION_ROOT = META_DIR / "promotion_candidates"
REFLECTION_DIR = META_DIR / "reflection_candidates"
CACHE_PATH = META_DIR / "candidate_embed_cache.json"

# Directory parts that mean "no longer pending": drained/resolved queues,
# failed-parse stashes, and tooling debris.
_RESOLVED_PARTS = {"_promoted", "_failed", "accepted", "rejected", ".pytest_cache"}

# A new cluster/insight whose cosine vs a pending candidate clears this is a
# re-derivation, not a new insight. Slightly below auto_promote's 0.88
# existing-memory threshold: the pending pool is tiny (a handful of files, no
# broad soak-everything memories like decisions.md), and the cost asymmetry
# favors skipping — a missed dup is one more file for the human drain, a dup
# that slips through is the 38x failure mode again.
PENDING_DUP_THRESHOLD = 0.85

# Mirrors auto_promote.SIMILAR_EXISTING_THRESHOLD — "this theme is already a
# durable memory". Kept in sync by hand; see the rationale comment there.
EXISTING_COVERED_THRESHOLD = 0.88

# Provenance overlap: if this fraction of a pending candidate's cited
# source_episodes reappear in a new cluster, it's the same cluster re-derived.
# This is the PRIMARY dedup for the auto_promote lane — measured 2026-07-30,
# an epilogue-register centroid tops out ~0.84 cosine against its own drafted
# candidate body (cross-register gap), so the 0.85 embedding check alone
# never fires there. Citations don't have that problem: exact filenames.
OVERLAP_DUP_THRESHOLD = 0.5

# bge-small handles 512 tokens; same truncation auto_promote uses.
_EMBED_CHARS = 2500


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def pending_candidate_paths() -> list[Path]:
    """Every candidate .md still awaiting human review, across all lanes:
    promotion_candidates/ (auto_promote root, observer/, genesis/) and
    reflection_candidates/ (consolidate_worker reflection lane)."""
    out: list[Path] = []
    for root in (PROMOTION_ROOT, REFLECTION_DIR):
        if not root.exists():
            continue
        for p in root.rglob("*.md"):
            if _RESOLVED_PARTS.intersection(p.parts):
                continue
            out.append(p)
    return out


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    try:
        tmp = CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        tmp.replace(CACHE_PATH)
    except Exception:
        pass


def pending_candidate_vecs() -> list[tuple[Path, list[float]]]:
    """(path, vector) for every pending candidate. Embeds new/changed files,
    serves the rest from cache, prunes entries for drained files."""
    paths = pending_candidate_paths()
    if not paths:
        # Keep the cache from accumulating ghosts once the queue drains.
        if _load_cache():
            _save_cache({})
        return []

    cache = _load_cache()
    live_keys = set()
    to_embed: list[tuple[Path, str, str]] = []  # (path, key, text)
    result: list[tuple[Path, list[float]]] = []

    for p in paths:
        try:
            key = str(p)
            mtime = p.stat().st_mtime
            live_keys.add(key)
            entry = cache.get(key)
            if entry and entry.get("mtime") == mtime and entry.get("vec"):
                result.append((p, entry["vec"]))
                continue
            text = p.read_text(encoding="utf-8")[:_EMBED_CHARS]
            to_embed.append((p, key, text))
            cache[key] = {"mtime": mtime}  # vec filled below
        except Exception:
            continue

    if to_embed:
        try:
            from memory_engine import embed_batch

            vecs = embed_batch([t for _, _, t in to_embed])
        except Exception:
            vecs = None
        if vecs is not None:
            for (p, key, _), v in zip(to_embed, vecs):
                if v is None:
                    cache.pop(key, None)
                    continue
                # embed_batch yields numpy.float32 elements — coerce to plain
                # floats or json.dumps silently kills the cache write.
                fv = [float(x) for x in v]
                cache[key]["vec"] = fv
                result.append((p, fv))
        else:
            for _, key, _ in to_embed:
                cache.pop(key, None)

    # Prune drained/renamed files, then persist.
    for key in [k for k in cache if k not in live_keys]:
        cache.pop(key, None)
    _save_cache(cache)
    return result


def max_similarity_to_pending(vec: list[float]) -> tuple[float, Optional[Path]]:
    """How close is this vector to a candidate already awaiting review?"""
    best: tuple[float, Optional[Path]] = (0.0, None)
    try:
        for p, v in pending_candidate_vecs():
            sim = _cosine(vec, v)
            if sim > best[0]:
                best = (sim, p)
    except Exception:
        return (0.0, None)
    return best


def max_similarity_to_existing(vec: list[float]) -> tuple[float, str]:
    """How close is this vector to an existing durable memory? Skips
    auto-generated content (digests, handoffs) — same rule as
    auto_promote._max_similarity_to_existing, shared here so the reflection
    lane can use it too."""
    try:
        from memory_engine import db, _blob_to_vec
    except Exception:
        return (0.0, "")
    best = (0.0, "")
    try:
        with db() as conn:
            for row in conn.execute("SELECT filename, vector FROM embeddings"):
                fn = row[0]
                if fn.startswith("digest_") or fn.startswith("session_handoff_"):
                    continue
                sim = _cosine(vec, _blob_to_vec(row[1]))
                if sim > best[0]:
                    best = (sim, fn)
    except Exception:
        return (0.0, "")
    return best


def _cited_episodes(path: Path) -> set[str]:
    """Filenames listed in a candidate's `source_episodes:` frontmatter line
    (auto_promote writes these; reflection/observer candidates don't)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
        m = re.search(r"^source_episodes:\s*(.+)$", text, re.MULTILINE)
        if not m:
            return set()
        return {s.strip() for s in m.group(1).split(",") if s.strip()}
    except Exception:
        return set()


def cluster_overlaps_pending(
    member_filenames: list[str],
) -> tuple[float, Optional[Path]]:
    """Best provenance overlap between a cluster's member episodes and any
    pending candidate's citations: |cited ∩ members| / |cited|."""
    best: tuple[float, Optional[Path]] = (0.0, None)
    try:
        members = set(member_filenames)
        for p in pending_candidate_paths():
            cited = _cited_episodes(p)
            if not cited:
                continue
            frac = len(cited & members) / len(cited)
            if frac > best[0]:
                best = (frac, p)
    except Exception:
        return (0.0, None)
    return best


def record_corroboration(path: Path) -> bool:
    """A synthesis lane re-derived a pending candidate's theme. Instead of a
    duplicate file, bump `corroborations:` in its frontmatter and stamp
    `last_corroborated:` — the reviewer sees "the brain keeps arriving here"
    as a number on one file, not as 38 files. Returns True on success."""
    try:
        text = Path(path).read_text(encoding="utf-8")
        if not text.startswith("---"):
            return False
        today = datetime.now().strftime("%Y-%m-%d")

        m = re.search(r"^corroborations:\s*(\d+)\s*$", text, re.MULTILINE)
        if m:
            n = int(m.group(1)) + 1
            text = text[: m.start()] + f"corroborations: {n}" + text[m.end() :]
        else:
            # Insert just before the closing --- of the frontmatter block.
            end = text.find("\n---", 3)
            if end == -1:
                return False
            text = text[:end] + "\ncorroborations: 1" + text[end:]

        m = re.search(r"^last_corroborated:\s*\S+\s*$", text, re.MULTILINE)
        if m:
            text = text[: m.start()] + f"last_corroborated: {today}" + text[m.end() :]
        else:
            end = text.find("\n---", 3)
            text = text[:end] + f"\nlast_corroborated: {today}" + text[end:]

        Path(path).write_text(text, encoding="utf-8")
        return True
    except Exception:
        return False
