"""
runner.py — discover and execute migrations exactly once each.

A migration is any .py file in this dir matching `YYYY_MM_DD_*.py` that
exposes:
  MIGRATION_ID: str   -- unique, idempotent identifier
  def run(dry_run: bool = False) -> dict   -- returns summary of work done

Marker file: _meta/migrations_applied.json
  Stores: {applied: ["<MIGRATION_ID>", ...], log: [{id, ts, summary, ok}]}

The runner is fail-soft: a failing migration logs and continues. Boot ritual
calls this early so any maintenance happens before prefetch/recall need the
state cleaned up. Run manually:

  python migrations/runner.py --dry-run   # report planned work
  python migrations/runner.py             # apply un-applied migrations
  python migrations/runner.py --force <ID>  # rerun a specific migration
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import paths as _paths  # noqa: E402

MIGRATIONS_DIR = Path(__file__).resolve().parent
META_DIR = _paths.META_DIR
MARKER_PATH = META_DIR / "migrations_applied.json"


def _load_marker() -> dict:
    if not MARKER_PATH.exists():
        return {"applied": [], "log": []}
    try:
        return json.loads(MARKER_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"applied": [], "log": []}


def _save_marker(data: dict) -> None:
    META_DIR.mkdir(parents=True, exist_ok=True)
    MARKER_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def discover() -> list[tuple[str, Path]]:
    """Return [(MIGRATION_ID, path)] sorted by filename."""
    out = []
    for p in sorted(MIGRATIONS_DIR.glob("[0-9]*.py")):
        if p.name == "runner.py":
            continue
        spec = importlib.util.spec_from_file_location(p.stem, p)
        if not spec or not spec.loader:
            continue
        try:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as e:
            print(f"  WARN: failed to load {p.name}: {e}", file=sys.stderr)
            continue
        mid = getattr(mod, "MIGRATION_ID", p.stem)
        out.append((mid, p))
    return out


def run_one(path: Path, dry_run: bool) -> tuple[bool, dict, str]:
    """Execute a single migration. Returns (ok, summary_dict, migration_id)."""
    spec = importlib.util.spec_from_file_location(path.stem, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mid = getattr(mod, "MIGRATION_ID", path.stem)
    run_fn = getattr(mod, "run", None)
    if not run_fn:
        return (False, {"error": "no run() function"}, mid)
    try:
        summary = run_fn(dry_run=dry_run) or {}
        return (True, summary, mid)
    except Exception as e:
        return (False, {"error": str(e)}, mid)


def main():
    ap = argparse.ArgumentParser(description="Migration runner")
    ap.add_argument("--dry-run", action="store_true", help="Report only, don't apply")
    ap.add_argument("--force", help="Rerun a specific MIGRATION_ID even if applied")
    ap.add_argument("--quiet", action="store_true", help="Suppress per-migration output")
    args = ap.parse_args()

    marker = _load_marker()
    applied = set(marker.get("applied", []))

    migrations = discover()
    if not migrations:
        if not args.quiet:
            print("(no migrations found)")
        return

    n_run = 0
    n_skip = 0
    n_fail = 0

    for mid, path in migrations:
        if args.force and mid != args.force:
            continue
        if not args.force and mid in applied:
            n_skip += 1
            if not args.quiet:
                print(f"  SKIP  {mid}  (already applied)")
            continue

        ok, summary, mid_actual = run_one(path, args.dry_run)
        status = "DRY" if args.dry_run else ("OK" if ok else "FAIL")
        if not args.quiet:
            print(f"  {status:5s} {mid_actual}  {json.dumps(summary)[:120]}")

        if not args.dry_run:
            if ok:
                applied.add(mid_actual)
                n_run += 1
            else:
                n_fail += 1
            marker.setdefault("log", []).append({
                "id": mid_actual,
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "ok": ok,
                "summary": summary,
            })

    if not args.dry_run:
        marker["applied"] = sorted(applied)
        _save_marker(marker)

    if not args.quiet:
        print(f"\nRan: {n_run}  Skipped: {n_skip}  Failed: {n_fail}")


if __name__ == "__main__":
    main()
