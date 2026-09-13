"""
auto_promote.py — episodic → semantic auto-promotion (v3.2 Phase 5).

Closes the CoALA loop. Right now `/consolidate` is fully human-driven: the user
reads epilogue clusters and decides what's worth promoting to a permanent
memory. Auto-promotion adds a signal layer that surfaces strong candidates
with citations — the user still approves, but starts from candidates instead
of a blank page.

Algorithm:
  1. Load all epilogues from _meta/epilogues/ (drafts + finalized)
  2. Embed each via memory_engine
  3. Greedy clustering by cosine similarity (threshold CLUSTER_SIM_THRESHOLD = 0.85)
  4. Filter to clusters meeting minimum criteria:
       - cluster size >= MIN_CLUSTER_SIZE (3)
       - time span >= MIN_TIME_SPAN_DAYS (7)
  5. For each surviving cluster:
       a. Check if existing memory already covers this theme
          (max cosine vs any memory > SIMILAR_EXISTING_THRESHOLD = 0.88 → skip)
       b. LLM-draft a candidate semantic memory (name, description, body)
       c. Compute confidence tier (high / medium / low)
       d. Write to _meta/promotion_candidates/

Confidence tiers (see _confidence_tier — size/span/existing-sim only; a HIGH
cluster of >=5 episodes already implies >=3 distinct citations):
  - HIGH:   cluster size >= 5, span >= 14 days, no existing > 0.5 cosine
  - MEDIUM: cluster size 3-4, OR moderate similarity, OR shorter time span
  - LOW:    below thresholds — telemetry only

Trust gate (v3.2 Phase 5 ship state):
  - High-tier candidates are ELIGIBLE for auto-promote to memory dir
    iff `auto_promote_high_confidence_enabled` flag is ON
  - Flag ships OFF for a 4-week trust-earning window
  - During the window: all candidates flow to _meta/promotion_candidates/
    for human review via /consolidate; telemetry tracks
    "would-have-been-auto-promoted" candidates and the hypothetical approval
    rate
  - After 4 weeks: if hypothetical approval > 80% on high tier, flip flag
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
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
EPILOGUE_DIR = META_DIR / "epilogues"
PROMOTION_CANDIDATES_DIR = META_DIR / "promotion_candidates"
TELEMETRY_PATH = META_DIR / "v3_2_telemetry.jsonl"

# Clustering parameters
# Note: epilogue embeddings all share the user's voice register + core themes,
# so at low thresholds everything collapses into one big cluster. Tight
# clustering threshold (0.85) finds distinct themes. The "already covered"
# threshold is 0.88 — only skip if the cluster's centroid is *very* close
# to an existing memory. At lower thresholds, broad memories like
# decisions.md soak up any cluster about the user and Claude collaboration, even
# when the cluster's emergent pattern is novel.
MIN_CLUSTER_SIZE = 3
MIN_TIME_SPAN_DAYS = 7
CLUSTER_SIM_THRESHOLD = 0.85
SIMILAR_EXISTING_THRESHOLD = 0.88

# High-confidence tier (citation floor is subsumed by HC_MIN_CLUSTER — a cluster
# of >=5 episodes is already >=3 distinct citations, so there is no separate check)
HC_MIN_CLUSTER = 5
HC_MIN_SPAN_DAYS = 14
HC_MAX_EXISTING_SIM = 0.50


PROMOTION_PROMPT = """You are looking at {n_episodes} episode entries (epilogue drafts / session logs) from the user and Claude's collaboration that cluster around a shared theme. They span {span_days} days.

Your job: identify the recurring pattern across these episodes and propose ONE candidate semantic memory that captures it.

Episodes (most recent first):

{episodes}

Output ONE JSON object with these keys:
  "name": short evocative title (5-9 words)
  "description": one-line summary for retrieval indexing
  "type": one of: user | feedback | project | self
  "body": 2-4 paragraphs in the user's voice register (casual-direct, peer-to-peer, lowercase opener, no corporate tone). Write as if explaining the pattern to next-me. Cite specific episodes inline using their date ('on 2026-05-13...') when it sharpens the point.

If the episodes don't actually cluster around a single coherent pattern — if they're just noise that happened to embed close — output:
  {{"name": null, "reason": "no coherent pattern"}}

Output ONLY the JSON, no markdown fences, no preamble."""


def _log_telemetry(record: dict) -> None:
    try:
        META_DIR.mkdir(parents=True, exist_ok=True)
        record.setdefault("ts", int(time.time()))
        record.setdefault("component", "auto_promote")
        with TELEMETRY_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception:
        pass


def _parse_epilogue_date(filename: str) -> Optional[datetime]:
    """Extract date from epilogue filename like 'draft-2026-05-13-2048.md'."""
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", filename)
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except Exception:
        return None


def _load_epilogues() -> list[dict]:
    """Returns list of {path, text, date, filename}."""
    if not EPILOGUE_DIR.exists():
        return []
    items = []
    for path in EPILOGUE_DIR.glob("*.md"):
        # Drafts are unverified auto-captures (17:1 vs curated) — they poison
        # clustering. Triage merges them into weekly digests; digests carry
        # their signal into promotion instead. (truth-maintenance 2026-07-06)
        if path.name.startswith("draft-"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            continue
        if len(text.strip()) < 100:
            continue  # too short to cluster meaningfully
        items.append(
            {
                "path": path,
                "filename": path.name,
                "text": text,
                "date": _parse_epilogue_date(path.name),
            }
        )
    # Sort by date desc (most recent first)
    items.sort(key=lambda x: x["date"] or datetime.min, reverse=True)
    return items


def _embed_episodes(episodes: list[dict]) -> list[dict]:
    """Add 'vec' key to each episode dict. Skips embeddings that fail."""
    try:
        from memory_engine import embed_batch
    except Exception:
        return episodes
    # Truncate text for embedding — bge-small handles 512 tokens
    texts = [e["text"][:2500] for e in episodes]
    vecs = embed_batch(texts)
    if vecs is None:
        return episodes
    for e, v in zip(episodes, vecs):
        e["vec"] = v
    return episodes


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    import math

    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _greedy_cluster(episodes: list[dict]) -> list[list[dict]]:
    """Greedy clustering: each episode attaches to first cluster whose
    centroid has cosine > CLUSTER_SIM_THRESHOLD; else creates new cluster."""
    clusters: list[dict] = []  # each: {centroid: vec, members: [episodes]}
    for ep in episodes:
        v = ep.get("vec")
        if v is None:
            continue
        attached = False
        for c in clusters:
            if _cosine(v, c["centroid"]) >= CLUSTER_SIM_THRESHOLD:
                c["members"].append(ep)
                # Update centroid as running average
                n = len(c["members"])
                c["centroid"] = [
                    (c["centroid"][i] * (n - 1) + v[i]) / n for i in range(len(v))
                ]
                attached = True
                break
        if not attached:
            clusters.append({"centroid": list(v), "members": [ep]})
    return [c["members"] for c in clusters]


def _cluster_metadata(cluster: list[dict]) -> dict:
    """Compute size + time span + date range for a cluster."""
    dates = [e["date"] for e in cluster if e.get("date")]
    if dates:
        earliest = min(dates)
        latest = max(dates)
        span_days = (latest - earliest).days
    else:
        earliest = latest = None
        span_days = 0
    return {
        "size": len(cluster),
        "span_days": span_days,
        "earliest": earliest.strftime("%Y-%m-%d") if earliest else None,
        "latest": latest.strftime("%Y-%m-%d") if latest else None,
    }


def _max_similarity_to_existing(cluster_centroid: list[float]) -> tuple[float, str]:
    """Return (max_cosine, filename) — how close does this cluster's theme
    already match an existing semantic memory?

    Excludes auto-generated content (weekly digests, session handoffs) since
    those summarize a window rather than capturing a cross-window pattern.
    A cluster that matches a digest is a cluster the digest *summarized*,
    not a duplicate semantic memory."""
    try:
        from memory_engine import db, _blob_to_vec
    except Exception:
        return (0.0, "")
    best = (0.0, "")
    try:
        with db() as conn:
            for row in conn.execute("SELECT filename, vector FROM embeddings"):
                fn = row[0]
                # Skip auto-generated content — see docstring.
                if fn.startswith("digest_") or fn.startswith("session_handoff_"):
                    continue
                v = _blob_to_vec(row[1])
                sim = _cosine(cluster_centroid, v)
                if sim > best[0]:
                    best = (sim, fn)
    except Exception:
        return (0.0, "")
    return best


def _confidence_tier(meta: dict, max_existing_sim: float) -> str:
    """Classify HIGH / MEDIUM / LOW based on tier criteria."""
    if (
        meta["size"] >= HC_MIN_CLUSTER
        and meta["span_days"] >= HC_MIN_SPAN_DAYS
        and max_existing_sim <= HC_MAX_EXISTING_SIM
    ):
        return "HIGH"
    if meta["size"] >= MIN_CLUSTER_SIZE and meta["span_days"] >= MIN_TIME_SPAN_DAYS:
        return "MEDIUM"
    return "LOW"


def _slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "_", text.lower()).strip("_")
    return s[:40] or "untitled"


def _draft_candidate(cluster: list[dict], meta: dict) -> Optional[dict]:
    """LLM-generate a candidate semantic memory from the cluster.
    Returns None if LLM declines (no coherent pattern) or fails."""
    try:
        from providers import get_provider
        from tier_router import route, log_use
    except Exception:
        return None

    # Build episode excerpts block (most recent first)
    episode_strs = []
    for ep in cluster[:10]:  # cap at 10 to keep prompt size sane
        date_str = ep["date"].strftime("%Y-%m-%d") if ep.get("date") else "?"
        excerpt = (ep["text"] or "")[:500].strip()
        episode_strs.append(f"[{date_str}] {ep['filename']}\n{excerpt}")
    episodes_block = "\n\n---\n\n".join(episode_strs)

    prompt = PROMOTION_PROMPT.format(
        n_episodes=len(cluster),
        span_days=meta["span_days"],
        episodes=episodes_block,
    )

    # memory_classification routes to Sonnet/Anthropic. Previously fell back
    # to dart-brain (30B Q4) which spills VRAM on the 8GB card. Cleaner
    # degradation: skip drafting this cluster — it'll get another shot on
    # the next auto-promote run. Auto-promotion is periodic by design.
    try:
        provider_name, model, params = route("memory_classification")
        provider = get_provider(provider_name)
        if not provider.health_check():
            _log_telemetry(
                {
                    "event": "draft_candidate_skipped_provider_unhealthy",
                    "provider": provider_name,
                }
            )
            return None
    except Exception:
        return None

    try:
        # 3072 leaves room for a 4-paragraph body + JSON envelope without
        # truncation. At 2048 dart-brain consistently ran out mid-body and
        # produced unparseable JSON.
        resp = provider.generate(prompt, model=model, max_tokens=3072, temperature=0.4)
    except Exception:
        return None

    txt = (resp.text or "").strip()
    txt = re.sub(r"^```(?:json)?\s*", "", txt)
    txt = re.sub(r"\s*```$", "", txt)
    try:
        parsed = json.loads(txt)
    except Exception:
        match = re.search(r"\{.*\}", txt, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except Exception:
            return None

    if not parsed.get("name") or not parsed.get("body"):
        return None  # LLM declined (no coherent pattern) or malformed

    try:
        log_use(
            "memory_classification",
            model,
            tokens_in=resp.tokens_in or 0,
            tokens_out=resp.tokens_out or 0,
        )
    except Exception:
        pass

    return {**parsed, "model": f"{provider_name}:{model}"}


def find_candidates(write_drafts: bool = True) -> dict:
    """Main entry point. Clusters epilogues, generates candidates, writes
    drafts to _meta/promotion_candidates/ (unless write_drafts=False).
    Returns summary dict."""
    started = time.time()
    summary = {
        "epilogues_loaded": 0,
        "clusters_found": 0,
        "candidates_eligible": 0,
        "candidates_generated": 0,
        "candidates_by_tier": {"HIGH": 0, "MEDIUM": 0, "LOW": 0},
        "would_have_auto_promoted": 0,
        "auto_promoted": 0,
        "skipped_already_covered": 0,
        "skipped_pending_duplicate": 0,
    }

    episodes = _load_epilogues()
    summary["epilogues_loaded"] = len(episodes)
    if not episodes:
        return summary

    episodes = _embed_episodes(episodes)
    clusters = _greedy_cluster(episodes)
    summary["clusters_found"] = len(clusters)

    # Check feature flag for high-confidence auto-promote
    try:
        from feature_flags import is_enabled

        auto_hc = is_enabled("auto_promote_high_confidence_enabled")
    except Exception:
        auto_hc = False
    summary["high_confidence_flag"] = auto_hc

    if write_drafts:
        PROMOTION_CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)

    now_ts_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    today = datetime.now().strftime("%Y%m%d")

    candidate_details = []

    for cluster in clusters:
        meta = _cluster_metadata(cluster)
        if meta["size"] < MIN_CLUSTER_SIZE:
            continue
        if meta["span_days"] < MIN_TIME_SPAN_DAYS:
            continue
        summary["candidates_eligible"] += 1

        # Compute centroid of THIS cluster vs existing memories
        centroid = None
        for ep in cluster:
            if ep.get("vec"):
                if centroid is None:
                    centroid = list(ep["vec"])
                else:
                    centroid = [
                        (centroid[i] + ep["vec"][i]) for i in range(len(centroid))
                    ]
        if centroid is None:
            continue
        # Average
        n = len([e for e in cluster if e.get("vec")])
        centroid = [c / n for c in centroid]

        max_sim, sim_file = _max_similarity_to_existing(centroid)
        if max_sim >= SIMILAR_EXISTING_THRESHOLD:
            summary["skipped_already_covered"] += 1
            continue

        # The 38x lesson: also dedup against candidates still PENDING review.
        # A cluster that matches one is a re-derivation — record it as a
        # corroboration on the pending file (importance signal for the
        # reviewer) instead of drafting a duplicate. Primary signal is
        # provenance (cluster members vs the candidate's cited episodes);
        # embedding sim is backup for candidates without citations.
        try:
            from candidate_dedup import (
                OVERLAP_DUP_THRESHOLD,
                PENDING_DUP_THRESHOLD,
                cluster_overlaps_pending,
                max_similarity_to_pending,
                record_corroboration,
            )

            pend_sim, pend_path = max_similarity_to_pending(centroid)
            ov_frac, ov_path = cluster_overlaps_pending(
                [e["filename"] for e in cluster]
            )
        except Exception:
            pend_sim, pend_path = 0.0, None
            ov_frac, ov_path = 0.0, None
        dup_path = None
        if ov_frac >= OVERLAP_DUP_THRESHOLD and ov_path is not None:
            dup_path = ov_path
        elif pend_sim >= PENDING_DUP_THRESHOLD and pend_path is not None:
            dup_path = pend_path
        if dup_path is not None:
            summary["skipped_pending_duplicate"] += 1
            if write_drafts:
                record_corroboration(dup_path)
            _log_telemetry(
                {
                    "event": "skipped_pending_duplicate",
                    "pending": dup_path.name,
                    "sim": round(pend_sim, 3),
                    "citation_overlap": round(ov_frac, 3),
                    "size": meta["size"],
                    "span_days": meta["span_days"],
                }
            )
            continue

        tier = _confidence_tier(meta, max_sim)
        summary["candidates_by_tier"][tier] += 1

        if not write_drafts:
            candidate_details.append(
                {
                    **meta,
                    "tier": tier,
                    "max_existing_sim": round(max_sim, 3),
                    "closest_existing": sim_file,
                    "episode_filenames": [e["filename"] for e in cluster],
                }
            )
            continue

        drafted = _draft_candidate(cluster, meta)
        if not drafted:
            continue
        summary["candidates_generated"] += 1

        # Decide destination based on tier + flag
        is_eligible_auto = tier == "HIGH" and auto_hc
        if is_eligible_auto:
            # Write to memory dir directly with auto_promoted: true
            target_dir = MEMORY_DIR
            summary["auto_promoted"] += 1
        else:
            target_dir = PROMOTION_CANDIDATES_DIR
            if tier == "HIGH":
                summary["would_have_auto_promoted"] += 1

        slug = _slug(drafted["name"])
        mem_type = drafted.get("type", "self")
        if mem_type not in ("user", "feedback", "project", "self"):
            mem_type = "self"

        if is_eligible_auto:
            out_path = target_dir / f"{mem_type}_{slug}.md"
            if out_path.exists():
                # Don't clobber — fall back to candidates dir
                target_dir = PROMOTION_CANDIDATES_DIR
                out_path = target_dir / f"{today}_{tier.lower()}_{mem_type}_{slug}.md"
                summary["auto_promoted"] -= 1
                summary["would_have_auto_promoted"] += 1
                is_eligible_auto = False
        else:
            out_path = target_dir / f"{today}_{tier.lower()}_{mem_type}_{slug}.md"

        # Build frontmatter
        citations = ", ".join(e["filename"] for e in cluster[:10])
        frontmatter_lines = [
            "---",
            f"name: {drafted['name']}",
            f"description: {drafted.get('description', '')}",
            f"type: {mem_type}",
            f"source: auto_promote",
            f"confidence_tier: {tier}",
            f"n_source_episodes: {meta['size']}",
            f"span_days: {meta['span_days']}",
            f"earliest_episode: {meta['earliest']}",
            f"latest_episode: {meta['latest']}",
            f"max_existing_sim: {max_sim:.3f}",
            f"closest_existing: {sim_file}",
            f"synthesized_at: {now_ts_iso}",
            f"synthesized_by: {drafted['model']}",
            f"source_episodes: {citations}",
        ]
        if is_eligible_auto:
            frontmatter_lines.extend(
                [
                    "auto_promoted: true",
                    "awaiting_review: true",
                ]
            )
        else:
            frontmatter_lines.extend(
                [
                    "status: draft",
                    "awaiting_approval: true",
                ]
            )
        frontmatter = "\n".join(frontmatter_lines) + "\n---\n"

        with out_path.open("w", encoding="utf-8") as f:
            f.write(frontmatter + "\n" + drafted["body"].strip() + "\n")

        _log_telemetry(
            {
                "event": "candidate_written",
                "tier": tier,
                "auto_promoted": is_eligible_auto,
                "filename": out_path.name,
                "size": meta["size"],
                "span_days": meta["span_days"],
                "max_existing_sim": round(max_sim, 3),
            }
        )

    summary["duration_sec"] = round(time.time() - started, 2)
    if not write_drafts:
        summary["candidate_details"] = candidate_details

    return summary


def main():
    """CLI.

    Usage:
      python auto_promote.py dry-run   # cluster + report, no LLM calls
      python auto_promote.py run       # full pass — clusters + LLM drafts
      python auto_promote.py status    # candidate dir contents
    """
    if len(sys.argv) < 2:
        print("Usage: python auto_promote.py dry-run | run | status")
        return

    cmd = sys.argv[1]

    if cmd == "dry-run":
        # Extra verbose: show cluster shapes before any filtering
        episodes = _embed_episodes(_load_epilogues())
        clusters = _greedy_cluster(episodes)
        print(
            f"# Diagnostic — {len(episodes)} epilogues → {len(clusters)} clusters at threshold {CLUSTER_SIM_THRESHOLD}\n"
        )
        for i, c in enumerate(clusters, 1):
            meta = _cluster_metadata(c)
            # Compute centroid
            centroid = None
            n = 0
            for ep in c:
                if ep.get("vec"):
                    if centroid is None:
                        centroid = list(ep["vec"])
                    else:
                        centroid = [
                            centroid[i] + ep["vec"][i] for i in range(len(centroid))
                        ]
                    n += 1
            if centroid:
                centroid = [v / n for v in centroid]
                max_sim, sim_file = _max_similarity_to_existing(centroid)
            else:
                max_sim, sim_file = 0.0, ""
            eligible = (
                meta["size"] >= MIN_CLUSTER_SIZE
                and meta["span_days"] >= MIN_TIME_SPAN_DAYS
            )
            covered = max_sim >= SIMILAR_EXISTING_THRESHOLD
            tier = _confidence_tier(meta, max_sim) if eligible else "—"
            print(
                f"Cluster {i}: size={meta['size']:2d} span={meta['span_days']:3d}d  "
                f"sim_to_existing={max_sim:.2f}→{sim_file or '(none)':35s} "
                f"eligible={eligible} covered={covered} tier={tier}"
            )
            print(f"  earliest={meta['earliest']} latest={meta['latest']}")
            print(
                f"  members: {[e['filename'] for e in c][:5]}{' ...' if len(c) > 5 else ''}"
            )
            print()
        print("---")
        result = find_candidates(write_drafts=False)
        print(json.dumps(result, indent=2, default=str))
        return

    if cmd == "run":
        result = find_candidates(write_drafts=True)
        print(json.dumps(result, indent=2, default=str))
        return

    if cmd == "status":
        if not PROMOTION_CANDIDATES_DIR.exists():
            print("(no candidates dir yet)")
            return
        items = sorted(PROMOTION_CANDIDATES_DIR.glob("*.md"))
        print(f"{len(items)} candidates in {PROMOTION_CANDIDATES_DIR}:")
        for p in items:
            print(f"  {p.name}")
        return

    print(f"Unknown command: {cmd}")


if __name__ == "__main__":
    main()
