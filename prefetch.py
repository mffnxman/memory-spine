"""
prefetch.py — UserPromptSubmit hook.

Reads JSON from stdin (Claude Code hook input), extracts user prompt,
runs semantic search over auto-memory, prints top hints to stdout
which get injected into Claude's context.

Quiet by design — only emits if it finds high-confidence matches.
Fast — must complete in <500ms.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import paths as _paths  # noqa: E402
from memory_engine import search, search_hybrid, list_memories, log_access

# Force UTF-8 stdout on Windows
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

MIN_SCORE = 0.012  # RRF floor — hybrid scores are smaller than TF-IDF (1/(60+rank))
MAX_HITS = 3  # cap on injected hints

# v13 Phase 10 — progressive disclosure:
#   0 = title + score only       (~30 tok/hit)
#   1 = title + 1-line description (~100 tok/hit, prior default)
#   2 = title + summary + first paragraph (~300 tok/hit)
# Env override for experimentation: MEMORY_DETAIL_LEVEL
import os as _os

DEFAULT_DETAIL_LEVEL = int(_os.environ.get("MEMORY_DETAIL_LEVEL", "1"))

# v14 Phase 4 — homemade observer fallback bridge.
# Triggers only when observer_fallback_enabled and top v3.2 score below threshold.
# Spine stays sovereign — observer hits appended AFTER v3.2 hits.
_OBSERVER_FALLBACK_FLAG = "observer_fallback_enabled"
_OBSERVER_FALLBACK_THRESHOLD = "observer_fallback_threshold"
_OBSERVER_FALLBACK_LIMIT = "observer_fallback_limit"
_FLAGS_PATH = _paths.META_DIR / "feature_flags.json"


def _observer_flag(name, default):
    env_key = "OBSERVER_FLAG_" + name.upper()
    if env_key in _os.environ:
        v = _os.environ[env_key].lower()
        if isinstance(default, bool):
            return v in ("1", "true", "yes", "on")
        try:
            return type(default)(_os.environ[env_key])
        except Exception:
            return default
    try:
        if _FLAGS_PATH.exists():
            with open(_FLAGS_PATH, encoding="utf-8") as f:
                flags = json.load(f)
            return flags.get(name, default)
    except Exception:
        pass
    return default


def _rerank_floor():
    """Minimum cross-encoder rerank_score (logit) for an injected memory.

    Calibrated from the 2026-07-31 MiniLM-vs-bge replay (see
    _meta/audits/rerank_ab_2026-07-31/): noise queries pass the decayed-RRF
    MIN_SCORE trivially, so the rerank logit is the only signal that separates
    "this memory answers this prompt" from "this was the least-bad of a junk
    pool". Flag `prefetch_rerank_floor` in feature_flags.json; absent -> None
    -> floor disabled (pre-migration behavior)."""
    v = _observer_flag("prefetch_rerank_floor", None)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return

    prompt = payload.get("prompt", "") or payload.get("user_message", "")
    if not prompt or len(prompt) < 10:
        return

    # Skip noise: very short prompts, slash commands (already routed)
    if prompt.strip().startswith("/"):
        return

    try:
        mems = list_memories()
        # v14 Phase 2: HyDE expansion — generate a hypothetical memory entry,
        # search with prompt + hypothetical concatenated. Fail-soft to vanilla
        # search if HyDE is disabled / unavailable / times out.
        search_query = prompt
        try:
            from hyde import expand as _hyde_expand

            hypothetical = _hyde_expand(prompt)
            if hypothetical:
                # Concatenate: the original prompt keeps query intent, the
                # hypothetical adds memory-distribution vocabulary.
                search_query = f"{prompt}\n\n{hypothetical}"
        except Exception:
            pass
        # Hybrid retrieval (TF-IDF + vector via RRF). Falls back to TF-IDF if no embeddings.
        results = search_hybrid(
            search_query,
            mems=mems,
            top_k=MAX_HITS * 2,
            rerank_floor=_rerank_floor(),
        )
    except Exception as e:
        # Stay silent to the user (never block the prompt) but leave a breadcrumb
        # so a degraded retrieval path is visible, not guessed (recon H6).
        try:
            import memory_engine

            memory_engine._log_telemetry(
                {
                    "event": "prefetch_search_error",
                    "component": "prefetch",
                    "error": str(e)[:200],
                }
            )
        except Exception:
            pass
        return

    high_conf = [(m, s, t) for m, s, t in results if s >= MIN_SCORE][:MAX_HITS]

    # v14 Phase 4 — observer fallback bridge.
    # Trigger only when flag is ON and v3.2's top score is below threshold
    # (or there are no v3.2 hits at all). Spine stays sovereign — observer
    # output is appended AFTER v3.2 hits, in a separate labeled section.
    observer_hits = []
    # BIGBUFF 2.0 P2 (D1-05, the user's call 2026-08-05): when the rerank floor
    # ACTIVELY suppressed every spine candidate ("this prompt deserves
    # nothing"), the fallback stays quiet too — unscored excerpts through the
    # side door undid exactly what the floor decided. Fallback still fires
    # when the spine genuinely had nothing (uncovered topics).
    _floor_suppressed = False
    try:
        import memory_engine as _me

        _floor_suppressed = bool(getattr(_me, "LAST_FLOOR_SUPPRESSED_ALL", False))
    except Exception:
        pass
    if _floor_suppressed:
        try:
            import memory_engine

            memory_engine._log_telemetry(
                {
                    "event": "observer_fallback_gated",
                    "component": "prefetch",
                    "reason": "rerank_floor_suppressed_all",
                }
            )
        except Exception:
            pass
    if _observer_flag(_OBSERVER_FALLBACK_FLAG, False) and not _floor_suppressed:
        try:
            top_score = high_conf[0][1] if high_conf else 0.0
            threshold = float(_observer_flag(_OBSERVER_FALLBACK_THRESHOLD, 0.04))
            if top_score < threshold:
                limit = int(_observer_flag(_OBSERVER_FALLBACK_LIMIT, 3))
                import observer_query

                observer_hits = observer_query.search(prompt, limit=limit)
                if observer_hits:
                    observer_query.mark_referenced([h["id"] for h in observer_hits])
        except Exception:
            observer_hits = []

    # procedural (L2) recall lane — flag-gated (default OFF), fail-soft.
    proc_block = ""
    try:
        import procedural_lib

        proc_block = procedural_lib.recall_lane(prompt)
    except Exception:
        proc_block = ""

    if not high_conf and not observer_hits and not proc_block:
        return

    # Log access (which memories were prefetched)
    for m, _, _ in high_conf:
        try:
            log_access(m.filename, source="prefetch")
        except Exception:
            pass

    # Output as additionalContext via JSON — detail-level controls verbosity
    detail = DEFAULT_DETAIL_LEVEL
    lines = []
    if high_conf:
        lines.append("[memory pre-fetch] You may want to read these for this prompt:")
    for m, score, matched in high_conf:
        if detail <= 0:
            # Layer 0: title + score only (~30 tok)
            lines.append(f"  - {m.filename} ({m.type or '—'}, {score:.2f})")
        elif detail == 1:
            # Layer 1: title + 1-line description (current behavior)
            lines.append(
                f"  - {m.filename} ({m.type or '—'}, score {score:.2f}) — {m.description}"
            )
        else:
            # Layer 2: title + description + first paragraph of body
            first_para = (m.body or "").strip().split("\n\n", 1)[0]
            first_para = first_para[:600].replace("\n", " ")
            lines.append(
                f"  - {m.filename} ({m.type or '—'}, score {score:.2f}) — {m.description}\n      {first_para}"
            )
    if detail <= 0 and high_conf:
        lines.append(
            "  (Run `/recall <topic>` for description, `/recall --deep` for full text.)"
        )

    # v14 Phase 4 — append observer section if we have hits
    if observer_hits:
        try:
            import observer_query

            obs_block = observer_query.format_for_prefetch(observer_hits)
            if obs_block:
                if lines:
                    lines.append("")  # blank separator between sections
                lines.append(obs_block)
        except Exception:
            pass

    # procedural (L2) section — appended after spine + observer
    if proc_block:
        if lines:
            lines.append("")
        lines.append(proc_block)

    output = "\n".join(lines)

    # Hook output format: emit JSON with additionalContext for UserPromptSubmit
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": output,
                }
            }
        )
    )


if __name__ == "__main__":
    # Hot-path declaration: skips the in-process fastembed/ONNX load (~660MB,
    # seconds) and forbids in-process reranker cold loads. Search degrades to
    # TF-IDF+KG; the warm rerank daemon is still used when up. See the
    # 2026-06-10 freeze post-mortem. Both consumers (memory_engine._get_embedder,
    # reranker.rerank) read this at CALL time, so setting it here is equivalent
    # for hook runs — but set only under __main__ so that merely importing
    # prefetch (tests, other tools) doesn't flip the whole process into
    # degraded no-embedder mode.
    os.environ.setdefault("MEMORY_HOT_PATH", "1")
    main()
