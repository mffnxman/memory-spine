"""
observer_init.py - idempotent db setup for the homemade observer.

Run this once to verify the observations.db is created cleanly. Safe to re-run.

Phase 0 of plan_homemade_observer_v1.

Usage:
    python observer_init.py
"""
from __future__ import annotations

import sys

from observer_lib import DB_PATH, ensure_db, get_connection


def main():
    print("observer_init: target db = " + str(DB_PATH))
    pre_existed = DB_PATH.exists()
    print("observer_init: db existed before run = " + str(pre_existed))

    ensure_db()
    print("observer_init: ensure_db() OK")

    # Verify all expected tables / indices / triggers exist.
    expected_tables = {"sessions", "observations", "session_summaries", "observations_fts"}
    expected_indices = {
        "idx_obs_session", "idx_obs_ts", "idx_obs_tool",
        "idx_obs_promoted", "idx_sessions_started",
    }
    expected_triggers = {"obs_ai", "obs_ad"}

    with get_connection() as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
        ).fetchall()}
        indices = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()}
        triggers = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()}

    missing_tables = expected_tables - tables
    missing_indices = expected_indices - indices
    missing_triggers = expected_triggers - triggers

    print("observer_init: tables   = " + ", ".join(sorted(tables)))
    print("observer_init: indices  = " + ", ".join(sorted(indices)))
    print("observer_init: triggers = " + ", ".join(sorted(triggers)))

    if missing_tables or missing_indices or missing_triggers:
        print("observer_init: FAIL")
        if missing_tables:
            print("  missing tables:   " + ", ".join(sorted(missing_tables)))
        if missing_indices:
            print("  missing indices:  " + ", ".join(sorted(missing_indices)))
        if missing_triggers:
            print("  missing triggers: " + ", ".join(sorted(missing_triggers)))
        sys.exit(1)

    print("observer_init: PASS")


if __name__ == "__main__":
    main()
