"""
event_bus.py — write-only append API for the v13 outbox.

All session activity gets a deterministic content-addressed event_id so the same
event can never be processed twice. Events are immutable; only the outbox jobs
table tracks processing status.

Schema:
  _meta/events.jsonl  — append-only, never deleted, never modified
  _meta/jobs.jsonl    — outbox of {event_id, status, attempts, last_error}

Status flow: pending -> processing -> done | failed (dead-letter at >= 3 attempts)

This module is intentionally tiny and side-effect-only. Real processing lives
in outbox_worker.py. emit_event() must be cheap, idempotent, and never raise.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
import paths as _paths  # noqa: E402

META_DIR = _paths.META_DIR
EVENTS_LOG = META_DIR / "events.jsonl"
JOBS_LOG = META_DIR / "jobs.jsonl"

MAX_ATTEMPTS = 3


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _telemetry(record: dict) -> None:
    """Fail-soft observability breadcrumb for the durability-spine swallow branches.
    Silent fail-soft means a degraded outbox looks identical to a healthy idle one
    (recon H6); one line per swallow turns invisible degradation into a signal.
    Mirrors memory_engine._log_telemetry. Never raises."""
    try:
        rec = dict(record)
        rec.setdefault("ts", int(time.time()))
        rec.setdefault("component", "event_bus")
        tpath = META_DIR / "v3_2_telemetry.jsonl"
        tpath.parent.mkdir(parents=True, exist_ok=True)
        with tpath.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
    except Exception:
        pass


def _detect_platform_source() -> str:
    """Auto-detect which agent emitted this event. Phase 2 hook."""
    if os.environ.get("DARTAGNAN_AGENT"):
        return "dartagnan"
    if os.environ.get("DUCK_RUNTIME"):
        return "duck_sentinel"
    if os.environ.get("OPENCLAW_SESSION"):
        return "openclaw"
    if os.environ.get("CODEX_SESSION"):
        return "codex"
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE"):
        return "claude_code"
    return "claude_code"


def _session_id() -> str:
    """Best-effort session id. Falls back to a stable process id."""
    sid = (
        os.environ.get("CLAUDE_SESSION_ID")
        or os.environ.get("SESSION_ID")
        or f"pid-{os.getpid()}"
    )
    return sid


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def compute_event_id(timestamp: str, session_id: str, kind: str, payload: Any) -> str:
    raw = f"{timestamp}|{session_id}|{kind}|{_canonical(payload)}"
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")


SCHEMA_VERSION = 1  # event envelope contract version (additive; NOT in compute_event_id)


def emit_event(kind: str, payload: dict | None = None) -> str | None:
    """Append an event + a pending job. Returns event_id or None on failure.

    Best-effort: never raises. If disk is unavailable we silently noop and the
    caller (session_log / memory_write_postprocess) continues with its direct
    write path. The outbox is additive infrastructure.
    """
    try:
        ts = _now_iso()
        sid = _session_id()
        payload = payload or {}
        event_id = compute_event_id(ts, sid, kind, payload)
        event = {
            "event_id": event_id,
            "timestamp": ts,
            "session_id": sid,
            "kind": kind,
            "platform_source": _detect_platform_source(),
            "schema_version": SCHEMA_VERSION,
            "payload": payload,
        }
        _append_jsonl(EVENTS_LOG, event)
        job = {
            "event_id": event_id,
            "status": "pending",
            "attempts": 0,
            "last_error": None,
            "updated_at": ts,
        }
        _append_jsonl(JOBS_LOG, job)
        return event_id
    except Exception as e:
        _telemetry({"event": "emit_event_error", "kind": kind, "error": str(e)[:200]})
        return None


def lookup_event(event_id: str) -> dict | None:
    """Linear scan of events.jsonl for an event_id. Fine for current scale."""
    if not EVENTS_LOG.exists():
        return None
    try:
        with EVENTS_LOG.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    # One torn/malformed line costs one row, not the whole lookup
                    # (recon H2 applied to events.jsonl): a single bad line used to
                    # return None and dead-letter the valid neighbor event.
                    _telemetry({"event": "read_events_bad_line", "len": len(line)})
                    continue
                if not isinstance(rec, dict):
                    # valid JSON but not an object (e.g. "[1,2,3]") — .get() would
                    # raise and the outer except would dead-letter the neighbor.
                    _telemetry({"event": "read_events_bad_line", "len": len(line)})
                    continue
                if rec.get("event_id") == event_id:
                    return rec
    except Exception:
        return None
    return None


def read_jobs(status_filter: str | None = None, limit: int | None = None) -> list[dict]:
    """Read all jobs, optionally filtered. Most recent status per event_id wins."""
    if not JOBS_LOG.exists():
        return []
    latest: dict[str, dict] = {}
    try:
        with JOBS_LOG.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    # One torn/malformed line costs one row, not the whole queue
                    # (recon H2): a single bad line used to blank every job, making
                    # a memory write look unindexed or a consolidate re-run.
                    _telemetry({"event": "read_jobs_bad_line", "len": len(line)})
                    continue
                if not isinstance(rec, dict):
                    _telemetry({"event": "read_jobs_bad_line", "len": len(line)})
                    continue
                eid = rec.get("event_id")
                if eid:
                    latest[eid] = rec
    except Exception as e:
        _telemetry({"event": "read_jobs_error", "error": str(e)[:200]})
        return []
    jobs = list(latest.values())
    if status_filter:
        jobs = [j for j in jobs if j.get("status") == status_filter]
    if limit:
        jobs = jobs[:limit]
    return jobs


def update_job(event_id: str, status: str, error: str | None = None, attempts_delta: int = 0) -> None:
    """Append a new job state row. Latest row wins on read."""
    try:
        existing = None
        for rec in read_jobs():
            if rec.get("event_id") == event_id:
                existing = rec
                break
        attempts = (existing or {}).get("attempts", 0) + attempts_delta
        record = {
            "event_id": event_id,
            "status": status,
            "attempts": attempts,
            "last_error": error,
            "updated_at": _now_iso(),
        }
        _append_jsonl(JOBS_LOG, record)
    except Exception as e:
        _telemetry({"event": "update_job_error", "event_id": event_id, "status": status, "error": str(e)[:200]})


def compact_jobs(keep_done_days: int = 7) -> int:
    """Rewrite jobs.jsonl keeping only the latest row per event_id, and dropping
    'done' jobs older than keep_done_days. Returns rows removed.

    jobs.jsonl is outbox STATE (read_jobs already collapses to latest-per-id, so
    this is behavior-preserving) and is safe to rewrite. events.jsonl is the
    immutable replay log and is NEVER touched. Atomic (write-temp + os.replace)
    and fail-soft — on any error the original file is left intact.
    """
    if not JOBS_LOG.exists():
        return 0
    try:
        latest: dict[str, dict] = {}
        total = 0
        with JOBS_LOG.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                total += 1
                rec = json.loads(line)
                eid = rec.get("event_id")
                if eid:
                    latest[eid] = rec
        cutoff = time.time() - keep_done_days * 86400
        kept = []
        for rec in latest.values():
            if rec.get("status") == "done":
                try:
                    ts = datetime.fromisoformat(
                        rec.get("updated_at", "").replace("Z", "+00:00")).timestamp()
                except Exception:
                    ts = time.time()  # unparseable -> treat as fresh, keep
                if ts < cutoff:
                    continue  # old done job — terminal, safe to drop
            kept.append(rec)
        tmp = JOBS_LOG.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for rec in kept:
                f.write(json.dumps(rec, separators=(",", ":"), default=str) + "\n")
        os.replace(tmp, JOBS_LOG)
        return total - len(kept)
    except Exception as e:
        _telemetry({"event": "compact_jobs_error", "error": str(e)[:200]})
        return 0


def stats() -> dict:
    """Pipeline health snapshot. Counts strictly by status — 'dead' is now an
    explicit terminal status (set by the worker), so the old
    attempts>=MAX_ATTEMPTS heuristic is dropped (it double-counted dead jobs)."""
    out = {"pending": 0, "processing": 0, "done": 0, "failed": 0,
           "dead": 0, "cancelled": 0, "total": 0}
    for j in read_jobs():
        out["total"] += 1
        s = j.get("status", "pending")
        if s in out:
            out[s] += 1
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="event_bus inspection CLI")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("stats", help="Show outbox pipeline counts")
    p_emit = sub.add_parser("emit", help="Manually emit a synthetic event (testing)")
    p_emit.add_argument("kind")
    p_emit.add_argument("--payload", default="{}")
    sub.add_parser("pending", help="List pending jobs")
    args = ap.parse_args()
    if args.cmd == "stats":
        print(json.dumps(stats(), indent=2))
    elif args.cmd == "emit":
        eid = emit_event(args.kind, json.loads(args.payload))
        print(eid or "(failed)")
    elif args.cmd == "pending":
        for j in read_jobs(status_filter="pending"):
            print(json.dumps(j))
    else:
        ap.print_help()
