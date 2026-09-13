"""
observer_session_start.py - rotate session_id at SessionStart.

Phase 1 of plan_homemade_observer_v1. Run from the SessionStart hook to
generate a fresh UUID and persist it. PostToolUse hooks read it via
observer_lib.resolve_session_id() in the same session.

Why a separate script for this:
- Avoids races where the first PostToolUse hook generates the UUID — if
  two PostToolUse fire concurrently before .session_id exists, they could
  pick different IDs. SessionStart-first eliminates that.
- Lets us mark any prior-session row that's still status='active' as
  'abandoned' (the Stop hook may not have fired if claude code crashed).

Hook contract: stdin = JSON payload (may include source: startup|resume|clear|compact).
Always exits 0 even on failure (fail-open).

Usage (from settings.json):
    python observer_session_start.py 2>/dev/null
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    from observer_lib import (
        SESSION_ID_FILE, ensure_db, get_connection, ensure_session, log_error,
    )
except Exception:
    # Library import broke - nothing we can do, exit clean.
    sys.exit(0)


def _read_payload():
    try:
        raw = sys.stdin.read()
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return {}


def _mark_orphans_abandoned(conn):
    """Any session row that's still 'active' from a prior boot is orphaned —
    Stop hook didn't fire (claude code crashed, killed, etc). Mark abandoned.

    Cheap query (indexed on status implicitly via small row count)."""
    try:
        now = int(time.time())
        # Anything still 'active' is from a prior session (we haven't written
        # our new session row yet at this point).
        conn.execute(
            "UPDATE sessions SET status='abandoned', ended_at=? WHERE status='active'",
            (now,),
        )
    except Exception as e:
        log_error("session_start: mark_orphans failed: " + str(e))


def main():
    try:
        ensure_db()

        payload = _read_payload()
        cwd = payload.get("cwd") or os.getcwd()

        # Mark orphaned sessions first (before we register the new one).
        try:
            with get_connection() as conn:
                _mark_orphans_abandoned(conn)
                conn.commit()
        except Exception as e:
            log_error("session_start: orphan mark conn failed: " + str(e))

        # Prefer claude code's session_id from the payload — that's the id
        # PostToolUse hooks will use too, so we want our sessions row to match.
        # Fall back to a generated uuid only if payload is empty (won't happen
        # in normal operation; only when invoked manually for testing).
        new_sid = payload.get("session_id") or str(uuid.uuid4())

        try:
            SESSION_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
            SESSION_ID_FILE.write_text(new_sid, encoding="utf-8")
        except Exception as e:
            log_error("session_start: write session_id file failed: " + str(e))
            sys.exit(0)

        # Create the session row OR re-activate it if a previous SessionEnd
        # marked it ended but claude code is resuming the same session_id.
        # claude code's /resume keeps the same session_id, so an "ended" row
        # is correct to flip back to 'active'.
        try:
            with get_connection() as conn:
                ensure_session(conn, new_sid, cwd=cwd)
                # if the session row already existed in 'ended' status, revive it
                conn.execute(
                    "UPDATE sessions SET status='active', ended_at=NULL WHERE session_id=? AND status='ended'",
                    (new_sid,),
                )
                conn.commit()
        except Exception as e:
            log_error("session_start: ensure_session failed: " + str(e))

    except Exception as e:
        try:
            log_error("session_start: top-level failure: " + str(e))
        except Exception:
            pass
    finally:
        # Never block the session boot.
        sys.exit(0)


if __name__ == "__main__":
    main()
