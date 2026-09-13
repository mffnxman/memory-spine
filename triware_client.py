"""triware_client.py — vendored, standalone append-only client for local agents.

Lets same-machine agents (d'Artagnan, duck-sentinel) append OBSERVATION-ONLY events
into the brain's shared events.jsonl/jobs.jsonl. Standalone: does NOT import event_bus
(which has __file__-relative paths + a sys.path side effect). Vendors compute_event_id
VERBATIM so event_ids match the brain's exactly. Fail-soft: never raises.

Gated by triware_ledger_enabled (default OFF). Power-gated by a client-side allowlist
(belt; the load-bearing barrier is the worker's UNCONDITIONAL server-side kind-gate).
"""

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
import sys as _sys  # noqa: E402
_sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths as _paths  # noqa: E402

SCHEMA_VERSION = 1
OBSERVATION_KINDS = frozenset({"agent_activity", "duck_signal", "dartagnan_task"})
TRIWARE_MAX_PAYLOAD_BYTES = 4096
TRIWARE_DEBOUNCE_SEC = 60

# portability v15: default derives from this file's location; env still overrides
_DEFAULT_META = Path(
    os.environ.get(
        "BRAIN_META_DIR", str(_paths.META_DIR)
    )
)


def _events_path() -> Path:
    return Path(
        os.environ.get("TRIWARE_EVENTS_PATH", str(_DEFAULT_META / "events.jsonl"))
    )


def _jobs_path() -> Path:
    return Path(os.environ.get("TRIWARE_JOBS_PATH", str(_DEFAULT_META / "jobs.jsonl")))


def _debounce_path() -> Path:
    return _DEFAULT_META / "triware_debounce.json"


def _content_key(source: str, kind: str, payload) -> str:
    """event_id minus the timestamp — identical-content events share this key."""
    raw = f"{source}|{kind}|{_canonical(payload)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _debounced(source: str, kind: str, payload, now: float) -> bool:
    """True iff an identical-content event from this source was emitted within the
    window. Fail-OPEN: any read error returns False (allow) — dropping a legit agent
    event is worse than a dup."""
    key = _content_key(source, kind, payload)
    try:
        p = _debounce_path()
        seen = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except Exception:
        return False
    last = seen.get(key)
    if last is not None and (now - last) < TRIWARE_DEBOUNCE_SEC:
        return True
    seen[key] = now
    seen = {
        k: t for k, t in seen.items() if (now - t) < TRIWARE_DEBOUNCE_SEC * 10
    }  # bound file
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(seen), encoding="utf-8")
    except Exception:
        pass
    return False


# --- vendored VERBATIM from event_bus.py:83-89 (do NOT drift; a test pins equality) ---
def _canonical(payload) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def compute_event_id(timestamp: str, session_id: str, kind: str, payload) -> str:
    raw = f"{timestamp}|{session_id}|{kind}|{_canonical(payload)}"
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


# --- end vendored ---


def _platform_source() -> str:
    if os.environ.get("DARTAGNAN_AGENT"):
        return "dartagnan"
    if os.environ.get("DUCK_RUNTIME"):
        return "duck_sentinel"
    return "unknown_agent"


def _enabled() -> bool:
    try:
        import feature_flags

        return bool(feature_flags.is_enabled("triware_ledger_enabled"))
    except Exception:
        return False


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")


def emit(kind: str, payload: dict | None = None) -> str | None:
    """Append an observation event + pending job. Returns event_id or None.
    Never raises. Refuses non-observation kinds (belt; worker enforces server-side)."""
    try:
        if not _enabled():
            return None
        if kind not in OBSERVATION_KINDS:
            return None
        payload = payload or {}
        if len(_canonical(payload).encode("utf-8")) > TRIWARE_MAX_PAYLOAD_BYTES:
            return None
        source = _platform_source()
        if _debounced(source, kind, payload, time.time()):
            return None
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        sid = (
            os.environ.get("CLAUDE_SESSION_ID")
            or os.environ.get("SESSION_ID")
            or f"pid-{os.getpid()}"
        )
        event_id = compute_event_id(ts, sid, kind, payload)
        event = {
            "event_id": event_id,
            "timestamp": ts,
            "session_id": sid,
            "kind": kind,
            "platform_source": source,
            "schema_version": SCHEMA_VERSION,
            "payload": payload,
        }
        _append_jsonl(_events_path(), event)
        _append_jsonl(
            _jobs_path(),
            {
                "event_id": event_id,
                "status": "pending",
                "attempts": 0,
                "last_error": None,
                "updated_at": ts,
            },
        )
        return event_id
    except Exception:
        return None
