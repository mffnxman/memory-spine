"""
outbox_worker.py — drains pending jobs from the event bus.

Reads pending events from event_bus, dispatches each to the appropriate
processor based on `kind`, marks jobs done/failed. Runs as:

  - PostToolUse hook tick (drain up to 5 jobs)
  - Cron via /schedule skill (drain up to 50 jobs every 5 min)
  - Manual: `python outbox_worker.py drain --max 100`

Crash-safety: if a worker dies mid-processing, the job sits in 'processing'
indefinitely. The reaper sweep promotes stuck 'processing' rows older than
5 minutes back to 'pending'. Idempotent retries are safe because all
downstream operations are upserts (kg.upsert_entity, conflict.dedup).

This worker is the v13 foundation. All future phases (read gate, tier
routing, weekly digest) hook into the same event stream.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import event_bus  # noqa: E402

# Promote a 'processing' job back to 'pending' only after this long. Must exceed
# the longest legitimate job: a consolidate's LLM designer pass runs 20-35 min,
# so the old 5-min timeout re-queued still-running consolidates into duplicate
# concurrent runs (a thundering herd that burned tokens). 60 min clears that with
# margin while still recovering genuinely dead jobs. (Heartbeating updated_at
# mid-run would be the fully robust fix should a job ever exceed this.)
STUCK_PROCESSING_TIMEOUT_SEC = 3600  # 60 min

# Job kinds that do LLM-backed or otherwise unbounded work. These MUST NOT run
# on the hot path (the Write|Edit PostToolUse hook, 60s budget): consolidate and
# reflection invoke procedural_lib.run_designer → subscription `claude -p` (180s)
# or Opus; importance_score routes to Ollama/dartagnan (120s). Running them
# synchronously in the hook blew past the budget — the edit landed but the
# tool_result was killed before return ("Tool result missing" / UI freeze,
# diagnosed 2026-06-03). The cron/manual drainer (hot_path=False) still runs
# them, off the tool's critical path.
HEAVY_KINDS = frozenset({"consolidate", "reflection", "importance_score"})

# Wall-clock ceiling for a single hot-path drain. Far under the 60s hook budget,
# so even an unexpectedly slow cheap job (e.g. a cold fastembed reindex load)
# can't push the hook into the kill zone. Checked between jobs.
HOT_PATH_BUDGET_SEC = 8.0


def _iso_to_dt(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)


def reap_stuck() -> int:
    """Promote stuck 'processing' jobs back to 'pending'. Returns count moved."""
    n = 0
    now = datetime.now(timezone.utc)
    for job in event_bus.read_jobs(status_filter="processing"):
        updated = _iso_to_dt(job.get("updated_at", ""))
        if (now - updated).total_seconds() > STUCK_PROCESSING_TIMEOUT_SEC:
            event_bus.update_job(job["event_id"], "pending", error="reaped-stuck")
            n += 1
    return n


OBSERVATION_KINDS = frozenset({"agent_activity", "duck_signal", "dartagnan_task"})


def process_event(event: dict) -> None:
    """Dispatch by event kind. All downstream ops must be idempotent."""
    if not isinstance(event, dict):
        return  # malformed/torn event -> ignore, never crash the worker
    kind = event.get("kind", "")
    payload = event.get("payload", {}) or {}

    # Triware server-side kind-gate (UNCONDITIONAL safety barrier — NOT flag-gated).
    # A source is trusted ONLY if it explicitly stamps platform_source == "claude_code";
    # a MISSING/empty source defaults to UNTRUSTED — a spine-write event with no
    # provenance must not be honored. A non-claude_code source may emit only
    # observation-only kinds; anything else (memory_write/reindex/...) is quarantined
    # and never routed into the spine. The vendored client allowlist is only convention
    # (bypassable via import event_bus); this worker check is the load-bearing barrier.
    # KNOWN PHASE-1 LIMITATION: platform_source is self-asserted/unsigned, so this
    # defends against an HONEST mis-tagged agent, not an adversarial one claiming
    # claude_code. Authenticated/out-of-band provenance is deferred to Phase 3.
    # claude_code passes unchanged -> today's pipeline is byte-identical.
    src = event.get("platform_source") or ""
    if src != "claude_code" and kind not in OBSERVATION_KINDS:
        event_bus._telemetry({"event": "kind_gate_quarantine",
                              "platform_source": src, "kind": kind, "component": "triware"})
        return

    if kind == "memory_write":
        # Delegate to existing entity extraction pipeline.
        file_path = payload.get("file_path", "")
        if not file_path:
            return
        try:
            from memory_write_postprocess import _process
            _process(file_path)
        except Exception as e:
            raise RuntimeError(f"memory_write_postprocess failed: {e}")

    elif kind == "reindex":
        # v14.1: embed the single changed memory so semantic recall reflects
        # disk within one tool tick (was: vector-invisible until manual
        # reindex.py). Idempotent: reindex_embeddings skips unchanged
        # content_hashes and uses INSERT OR REPLACE, so retries are safe.
        file_path = payload.get("file_path", "")
        if not file_path:
            return
        try:
            from pathlib import Path as _P
            from memory_engine import load_memory, reindex_embeddings
            p = _P(file_path)
            if not p.exists():
                return
            m = load_memory(p)
            reindex_embeddings([m])
        except Exception as e:
            raise RuntimeError(f"reindex failed: {e}")

    elif kind == "user_prompt":
        # Future: hook for prompt-aware indexing. Noop for now.
        pass

    elif kind == "tool_use":
        # Future: tool-use telemetry / read-cache update. Noop for now.
        pass

    elif kind == "session_start" or kind == "session_end":
        # Boundary markers. Already handled by session_log.py direct path.
        pass

    elif kind == "consolidate":
        # v14 Phase 4: sleep-time consolidation. Slow LLM+cosine work.
        try:
            from consolidate_worker import process_consolidate
            process_consolidate(payload)
        except Exception as e:
            raise RuntimeError(f"consolidate worker failed: {e}")

    elif kind == "importance_score":
        # v3.2 Phase 3: LLM-rate memory importance on write.
        # Idempotent via content-hash cache. Triggers reflection event
        # downstream if accumulated importance for the memory's type
        # crosses the threshold.
        file_path = payload.get("file_path", "")
        if not file_path:
            return
        try:
            from pathlib import Path
            from memory_engine import load_memory
            from importance import score_memory, accumulate
            p = Path(file_path)
            if not p.exists():
                return
            m = load_memory(p)
            result = score_memory(m.name, m.description, m.body, m.type, m.filename)
            if result is not None and m.type:
                accumulate(m.type, int(result["score"]))
        except Exception as e:
            raise RuntimeError(f"importance scoring failed: {e}")

    elif kind == "reflection":
        # v3.2 Phase 4: sleep-agent synthesis consumes the reflection event
        # fired when an importance threshold trips. Writes candidate drafts
        # to _meta/reflection_candidates/ — never to memory dir directly.
        try:
            from feature_flags import is_enabled
            if is_enabled("synthesis_passes_enabled"):
                from consolidate_worker import process_reflection
                process_reflection(payload)
        except Exception as e:
            raise RuntimeError(f"reflection synthesis failed: {e}")

    elif kind in OBSERVATION_KINDS:
        # Triware feedstock: observation-only, made VISIBLE via telemetry but NEVER
        # routed into memory (R2). The server-side gate above already blocked
        # non-observation kinds from low-trust sources.
        event_bus._telemetry({"event": "agent_observation",
                              "platform_source": event.get("platform_source", "?"),
                              "kind": kind, "component": "triware"})

    else:
        # Unknown kind — silent no-op (byte-identical to pre-Triware). Agent-sourced
        # unknown kinds never reach here (the gate above already quarantined them with
        # kind_gate_quarantine); only claude_code unknown kinds (e.g. legacy test_event)
        # land here, and stay silent as before.
        pass


def drain(max_jobs: int = 50, verbose: bool = False, hot_path: bool = False) -> dict:
    """Drain pending jobs. Returns counts.

    hot_path=True is for the PostToolUse Edit/Write hook: it leaves HEAVY_KINDS
    pending (cron handles those) and stops after HOT_PATH_BUDGET_SEC so the hook
    always returns well under its 60s budget. hot_path=False (cron/manual) keeps
    the full behavior, including the LLM-backed kinds.
    """
    counts = {"processed": 0, "done": 0, "failed": 0, "dead": 0, "reaped": 0,
              "skipped_heavy": 0, "deferred": 0}
    counts["reaped"] = reap_stuck()

    # On the hot path, look a little past max_jobs so cheap jobs sitting behind a
    # few heavy ones still get processed rather than starved by the limit window.
    fetch = max(max_jobs, 50) if hot_path else max_jobs
    pending = event_bus.read_jobs(status_filter="pending", limit=fetch)
    start = time.time()
    for job in pending:
        if counts["processed"] >= max_jobs:
            break
        if hot_path and (time.time() - start) > HOT_PATH_BUDGET_SEC:
            counts["deferred"] += 1
            break

        eid = job["event_id"]
        attempts = job.get("attempts", 0)
        event = event_bus.lookup_event(eid)

        # Hot path: never run LLM-backed kinds synchronously. Leave them pending
        # (do NOT mark processing) so the cron drainer picks them up later.
        if hot_path and event and event.get("kind") in HEAVY_KINDS:
            counts["skipped_heavy"] += 1
            continue

        event_bus.update_job(eid, "processing")
        try:
            if not event:
                # Permanent failure: the event log has no matching record, so a
                # retry can't help. Dead-letter immediately (terminal).
                event_bus.update_job(eid, "dead", error="event-not-found", attempts_delta=1)
                counts["dead"] += 1
                counts["processed"] += 1
                continue
            process_event(event)
            event_bus.update_job(eid, "done")
            counts["done"] += 1
        except Exception as e:
            new_attempts = attempts + 1
            if new_attempts >= event_bus.MAX_ATTEMPTS:
                # Retries exhausted → terminal dead-letter. 'dead' is never
                # re-read by drain (only 'pending' is), so it won't loop.
                status = "dead"
                counts["dead"] += 1
            else:
                # Transient failure → back to 'pending' so a later drain retries
                # (downstream ops are idempotent upserts). This is what makes
                # MAX_ATTEMPTS reachable — before, 'failed' was terminal at
                # attempts=1 and nothing ever retried or dead-lettered.
                status = "pending"
                counts["failed"] += 1
            event_bus.update_job(eid, status, error=str(e)[:200], attempts_delta=1)
            if verbose:
                print(f"[worker] {eid}: {e} (attempt {new_attempts} -> {status})", file=sys.stderr)
        counts["processed"] += 1

    return counts


def main():
    ap = argparse.ArgumentParser(description="outbox_worker — drain pending events")
    sub = ap.add_subparsers(dest="cmd")
    p_drain = sub.add_parser("drain", help="Drain pending jobs")
    p_drain.add_argument("--max", type=int, default=50)
    p_drain.add_argument("--verbose", action="store_true")
    sub.add_parser("tick", help="Single short tick — for PostToolUse hook")
    sub.add_parser("stats", help="Show pipeline stats")
    sub.add_parser("reap", help="Reap stuck processing jobs")
    args = ap.parse_args()

    if args.cmd == "drain":
        counts = drain(max_jobs=args.max, verbose=args.verbose)
        print(json.dumps(counts, indent=2))
    elif args.cmd == "tick":
        # PostToolUse hook tick: drain up to 5 cheap jobs, stay quiet.
        try:
            drain(max_jobs=5, verbose=False, hot_path=True)
        except Exception:
            pass
    elif args.cmd == "stats":
        print(json.dumps(event_bus.stats(), indent=2))
    elif args.cmd == "reap":
        print(json.dumps({"reaped": reap_stuck()}, indent=2))
    else:
        # Default to a single tick — safe for hook invocation with no args.
        try:
            drain(max_jobs=5, verbose=False, hot_path=True)
        except Exception:
            pass


if __name__ == "__main__":
    main()
