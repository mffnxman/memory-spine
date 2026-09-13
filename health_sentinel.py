"""health_sentinel.py — the brain checks its own pulse (v15, 2026-07-09).

Bus-factor-of-one failure mode, witnessed: the promotion pipeline sat dead
for WEEKS (17,447 observations, zero promotions) and nobody noticed, because
the only maintainer is us. This sentinel runs cheap data-flow invariants —
is anything actually MOVING? — on a daily scheduled task and at boot:

  - observations flowing (capture hooks alive)
  - outbox drained (no dead-lettered jobs, bounded pending)
  - consolidate cycle ran recently (the sleep cycle isn't wedged)
  - epilogues still being written
  - promotion candidate backlog bounded (graduation isn't stalled again)
  - embedding coverage complete (every memory retrievable)
  - hallucination audit clean (no phantoms in the recall path)
  - db sizes bounded

Writes _meta/health_status.json; boot_ritual surfaces ONLY failures
(silence = healthy — attention is a budget). A stale status file (>26h)
triggers a live re-check at boot, so even if the scheduled task dies,
the sentinel's own death gets noticed.

CLI:
    python health_sentinel.py run      # run checks, write status, print
    python health_sentinel.py status   # show last status file
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import paths as _paths  # noqa: E402

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

MEMORY_DIR = _paths.MEMORY_DIR
META_DIR = MEMORY_DIR / "_meta"
STATUS_PATH = META_DIR / "health_status.json"

OBS_MAX_AGE_H = 48  # no observations in 2 days = capture hooks dead
OUTBOX_MAX_PENDING = 25  # drain task keeps this near zero
CONSOLIDATE_MAX_AGE_D = 9  # weekly cadence + slack
EPILOGUE_MAX_AGE_D = 7
CANDIDATE_MAX_PENDING = 60  # graduation stalled if the pile grows past this
DB_CAPS_MB = {"observations.db": 500, "memory.db": 200}


def _check(name, ok, detail=""):
    return {"name": name, "ok": bool(ok), "detail": detail}


def check_observations_flowing(db_path=None, max_age_h=OBS_MAX_AGE_H):
    db_path = db_path or META_DIR / "observations.db"
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            last = conn.execute("SELECT MAX(ts) FROM observations").fetchone()[0]
    except Exception as e:
        return _check("observations_flowing", False, f"db unreadable: {e}"[:120])
    if last is None:
        return _check("observations_flowing", False, "no observations at all")
    age_h = (time.time() - last) / 3600.0
    return _check(
        "observations_flowing",
        age_h <= max_age_h,
        f"last observation {age_h:.0f}h ago"
        + ("" if age_h <= max_age_h else " — capture hooks stale"),
    )


def check_outbox_backlog(jobs_path=None, max_pending=OUTBOX_MAX_PENDING):
    jobs_path = jobs_path or META_DIR / "jobs.jsonl"
    if not Path(jobs_path).exists():
        return _check("outbox_backlog", True, "no jobs file yet")
    last_status = {}
    try:
        with open(jobs_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    last_status[r.get("event_id")] = r.get("status")
                except json.JSONDecodeError:
                    continue
    except OSError as e:
        return _check("outbox_backlog", False, f"jobs unreadable: {e}"[:120])
    pending = sum(1 for s in last_status.values() if s in ("pending", "processing"))
    dead = sum(1 for s in last_status.values() if s == "failed")
    ok = pending <= max_pending and dead == 0
    return _check("outbox_backlog", ok, f"{pending} pending, {dead} dead-lettered")


def check_consolidate_fresh(marker_path=None, max_age_days=CONSOLIDATE_MAX_AGE_D):
    marker_path = Path(marker_path or META_DIR / ".last_worker_consolidate")
    try:
        age_d = (time.time() - marker_path.stat().st_mtime) / 86400.0
    except OSError:
        return _check(
            "consolidate_fresh", False, "no consolidate marker — cycle never ran?"
        )
    return _check(
        "consolidate_fresh",
        age_d <= max_age_days,
        f"last sleep cycle {age_d:.1f}d ago",
    )


def check_epilogue_fresh(epi_dir=None, max_age_days=EPILOGUE_MAX_AGE_D):
    epi_dir = Path(epi_dir or META_DIR / "epilogues")
    try:
        newest = max((p.stat().st_mtime for p in epi_dir.glob("*.md")), default=None)
    except OSError:
        newest = None
    if newest is None:
        return _check("epilogue_fresh", False, "no epilogues found")
    age_d = (time.time() - newest) / 86400.0
    return _check(
        "epilogue_fresh", age_d <= max_age_days, f"newest epilogue {age_d:.1f}d old"
    )


def check_candidate_backlog(cand_dir=None, max_pending=CANDIDATE_MAX_PENDING):
    # Count only genuinely-pending candidates across ALL lanes (promotion +
    # reflection). The old inline glob missed reflection_candidates/ entirely
    # and counted drained files in _promoted/ as pending — after the 07-30
    # drain that was 57 ghosts, two files short of a permanent false alarm.
    if cand_dir is None:
        try:
            from candidate_dedup import pending_candidate_paths

            pending = len(pending_candidate_paths())
        except Exception:
            pending = -1
        if pending < 0:
            cand_dir = META_DIR / "promotion_candidates"  # degraded fallback
    if cand_dir is not None:
        cand_dir = Path(cand_dir)
        skip = {"accepted", "rejected", "_promoted", "_failed", ".pytest_cache"}
        pending = (
            sum(1 for p in cand_dir.glob("**/*.md") if not skip.intersection(p.parts))
            if cand_dir.exists()
            else 0
        )
    return _check(
        "candidate_backlog",
        pending <= max_pending,
        f"{pending} pending candidate(s)"
        + ("" if pending <= max_pending else " — graduation stalled?"),
    )


def check_embedding_coverage(memory_dir=None, db_path=None):
    memory_dir = Path(memory_dir or MEMORY_DIR)
    db_path = db_path or META_DIR / "memory.db"
    on_disk = {p.name for p in memory_dir.glob("*.md")} - {"MEMORY.md"}
    try:
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            embedded = {r[0] for r in conn.execute("SELECT filename FROM embeddings")}
    except Exception as e:
        return _check("embedding_coverage", False, f"db unreadable: {e}"[:120])
    missing = sorted(on_disk - embedded)
    return _check(
        "embedding_coverage",
        not missing,
        "full coverage" if not missing else f"{len(missing)} unembedded: {missing[:4]}",
    )


def check_hallucination_clean():
    try:
        from benchmark_gen import hallucination_audit

        a = hallucination_audit()
        detail = (
            "clean"
            if a["clean"]
            else f"orphan_emb={len(a['orphan_embeddings'])} phantom_idx={len(a['phantom_index'])} orphan_syn={a['orphan_co_recall']}"
        )
        return _check("hallucination_audit", a["clean"], detail)
    except Exception as e:
        return _check("hallucination_audit", False, f"audit failed: {e}"[:120])


def check_db_sizes():
    over = []
    for name, cap_mb in DB_CAPS_MB.items():
        p = META_DIR / name
        if p.exists() and p.stat().st_size > cap_mb * 1e6:
            over.append(f"{name} {p.stat().st_size / 1e6:.0f}MB > {cap_mb}MB")
    return _check("db_sizes", not over, "; ".join(over) or "within bounds")


def check_floor_regressions(floor_last_path=None):
    """Read the weekly floor-benchmark result (written by maintenance.py).
    Missing file = the check hasn't run yet — informational OK, not a failure."""
    p = Path(floor_last_path or META_DIR / "floor_last.json")
    if not p.exists():
        return _check("floor_regressions", True, "no floor run yet")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return _check("floor_regressions", False, f"floor_last unreadable: {e}"[:120])
    regs = data.get("regressions", [])
    return _check(
        "floor_regressions",
        not regs,
        (
            "no retrieval rot"
            if not regs
            else f"{len(regs)} regressed case(s): {regs[:3]}"
        ),
    )


def check_hotpath_floor_bypass(
    telemetry_path=None, days=7, max_bypass_frac=0.20, min_events=10
):
    """BIGBUFF 2.0 P2 (D1-03): the rerank floor only protects the hot path when
    the warm daemon answers. When it's cold/failed, prefetch fail-softs to raw
    decayed-RRF injection with NO pair-level junk filter — the exact pre-7/31
    behavior — and nothing alarmed (27% of post-surgery prompts got that).
    This check pairs each vec_empty_fused event with the next rerank event
    within 60s and warns when the cold/failed fraction exceeds the threshold."""
    p = Path(telemetry_path or META_DIR / "v3_2_telemetry.jsonl")
    if not p.exists():
        return _check("hotpath_floor_bypass", True, "no telemetry yet")
    cutoff = time.time() - days * 86400
    events = []
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                ts = e.get("ts")
                if isinstance(ts, str):
                    try:
                        from datetime import datetime as _dt

                        ts = _dt.fromisoformat(ts).timestamp()
                    except Exception:
                        continue
                if not isinstance(ts, (int, float)) or ts < cutoff:
                    continue
                ev = e.get("event", "")
                if ev == "vec_empty_fused" or ev.startswith("rerank"):
                    events.append((ts, ev))
    except OSError as e:
        return _check("hotpath_floor_bypass", False, f"telemetry unreadable: {e}"[:120])
    events.sort()
    fused = [i for i, (_, ev) in enumerate(events) if ev == "vec_empty_fused"]
    if len(fused) < min_events:
        return _check(
            "hotpath_floor_bypass",
            True,
            f"only {len(fused)} vec_empty_fused in {days}d (< {min_events} min sample)",
        )
    bypassed = 0
    for i in fused:
        ts_i = events[i][0]
        nxt = next(
            (
                ev
                for ts, ev in events[i + 1 :]
                if ev.startswith("rerank") and ts - ts_i <= 60
            ),
            None,
        )
        if nxt is None or "cold" in nxt or "fail" in nxt:
            bypassed += 1
    frac = bypassed / len(fused)
    return _check(
        "hotpath_floor_bypass",
        frac <= max_bypass_frac,
        f"{bypassed}/{len(fused)} hot-path prompts bypassed the floor "
        f"({frac:.0%}, threshold {max_bypass_frac:.0%}, trailing {days}d)",
    )


def check_wmi_liveness(alert_path=None, max_age_h=24):
    """BIGBUFF 2.0 P3 (D4-05): surface a WMI stall caught by the watchdog task
    (ClaudeBrain-WmiWatchdog, 15-min probe). The watchdog clears the alert
    when WMI answers again, so a lingering alert = current or recent wedge."""
    p = Path(alert_path or META_DIR / "wmi_stall_alert.json")
    if not p.exists():
        return _check("wmi_liveness", True, "no stall alert")
    age_h = (time.time() - p.stat().st_mtime) / 3600
    if age_h > max_age_h:
        return _check(
            "wmi_liveness", True, f"stale alert ({age_h:.0f}h old) — ignoring"
        )
    try:
        detail = json.loads(p.read_text(encoding="utf-8")).get("ts", "?")
    except Exception:
        detail = "?"
    return _check(
        "wmi_liveness",
        False,
        f"WMI STALL detected at {detail} — see _meta/wmi_watchdog.log for commit charge + top processes",
    )


def _dart_port_open(port=8180, timeout_s=2.0):
    """One cheap TCP probe of dartd's daemon port. Import-local socket so
    the sentinel keeps its no-new-top-level-deps shape."""
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _schtask_info(name):
    """{'state': ..., 'last_result': int} for a scheduled task, or None if
    missing/unqueryable. schtasks LIST output is locale-dependent — en-US
    field names are assumed (this machine); any parse miss returns None and
    the check degrades to a warning instead of lying green."""
    import re
    import subprocess

    try:
        out = subprocess.run(
            ["schtasks", "/query", "/tn", name, "/fo", "LIST", "/v"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if out.returncode != 0:
            return None
        state = re.search(r"^Status:\s+(.+)$", out.stdout, re.M)
        last = re.search(r"^Last Result:\s+(-?\d+)", out.stdout, re.M)
        if not state or not last:
            return None
        return {"state": state.group(1).strip(), "last_result": int(last.group(1))}
    except Exception:
        return None


def check_dart_lane(port_open=None, task_query=None):
    """BIGBUFF 3 (2026-08-13 squeeze autopsy): dart's crash lane must never
    look green. daemon_loop.ps1 propagates dartd's real exit code to the
    dart2-daemon task, so LastTaskResult is ground truth for HOW dart went
    down: 0 = deliberate /admin/shutdown (window close — the user's 7/27
    no-logon-trigger call, not a warning), nonzero = crash (the
    dart2-watchdog task revives within 5m; this check still tripping means
    the watchdog itself is broken). Also warns whenever the watchdog task
    is missing/disabled — with dart up OR down, an unguarded crash lane is
    the finding."""
    up = (port_open or _dart_port_open)()
    q = task_query or _schtask_info
    watchdog = q("dart2-watchdog")
    wd_bad = watchdog is None or str(watchdog.get("state", "")).lower() == "disabled"
    if up:
        if wd_bad:
            return _check(
                "dart_lane",
                False,
                "dartd up but dart2-watchdog task missing/disabled — crash lane unguarded",
            )
        return _check("dart_lane", True, "dartd up (port 8180)")
    daemon = q("dart2-daemon")
    if daemon is None:
        return _check(
            "dart_lane", False, "dartd down and dart2-daemon task missing/unqueryable"
        )
    if str(daemon.get("state", "")).lower() == "running":
        return _check("dart_lane", True, "dartd loading (daemon loop running)")
    if wd_bad:
        return _check(
            "dart_lane",
            False,
            "dartd down and dart2-watchdog task missing/disabled — crash lane unguarded",
        )
    last = daemon.get("last_result", 0)
    if last == 0:
        return _check(
            "dart_lane", True, "dartd down (deliberate shutdown) — boots on demand"
        )
    return _check(
        "dart_lane",
        False,
        f"dartd CRASHED and is down (dart2-daemon last result {last}) — the "
        "watchdog revives within 5m; persisting means the watchdog is broken "
        "(see ~/.dart2/watchdog.log)",
    )


def check_frontmatter_complete(memory_dir=None):
    """BIGBUFF 2.0 P2 (D1-07): every memory carries name/description/type/weight
    (CLAUDE.md standing rule). Missing weight/type silently excludes files from
    decay, importance scoring, and --weight-gated continuity tests — 43% of the
    spine was invisible to those lanes before the 2026-08-05 backfill."""
    import re as _re

    mdir = Path(memory_dir or MEMORY_DIR)
    missing = []
    for p in mdir.glob("*.md"):
        if p.name == "MEMORY.md":
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        m = _re.match(r"^---\n(.*?)\n---\n", text, _re.DOTALL)
        fm = m.group(1) if m else ""
        absent = [
            k
            for k in ("name", "description", "type", "weight")
            if not _re.search(rf"^\s*{k}:\s*\S", fm, _re.MULTILINE)
        ]
        if absent:
            missing.append(f"{p.name}({','.join(absent)})")
    return _check(
        "frontmatter_complete",
        not missing,
        (
            "; ".join(missing[:5])
            + (f" +{len(missing) - 5} more" if len(missing) > 5 else "")
            if missing
            else "all memories carry name/description/type/weight"
        ),
    )


def check_backup_freshness(snap_root=None, max_age_h=48):
    """BIGBUFF 2.0 Phase 1 item 8: the newest dated snapshot generation under
    the backup destination must be <48h old. Catches zero-run days,
    a rotted nightly task, AND a vanished/renamed backup dir — the
    audit found 4 zero-run days plus 1 killed run that nothing complained
    about ('machine was off' and 'lane rotted' were indistinguishable)."""
    if snap_root is None and _paths.BACKUP_DIR is None:
        return _check("backup_freshness", True, "MEMORY_BACKUP_DIR unset — check skipped")
    root = Path(snap_root or (_paths.BACKUP_DIR / "snapshots"))
    if not root.exists():
        return _check("backup_freshness", False, f"snapshot root missing: {root}")
    dated = sorted(
        (d for d in root.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=True
    )
    if not dated:
        return _check("backup_freshness", False, "no snapshot generations at all")
    newest = dated[0]
    age_h = (time.time() - newest.stat().st_mtime) / 3600
    return _check(
        "backup_freshness",
        age_h < max_age_h,
        f"newest generation {newest.name} is {age_h:.0f}h old"
        + ("" if age_h < max_age_h else f" (> {max_age_h}h — backup lane stale)"),
    )


def _all_checks():
    return [
        check_observations_flowing(),
        check_outbox_backlog(),
        check_consolidate_fresh(),
        check_epilogue_fresh(),
        check_candidate_backlog(),
        check_embedding_coverage(),
        check_hallucination_clean(),
        check_db_sizes(),
        check_floor_regressions(),
        check_backup_freshness(),
        check_hotpath_floor_bypass(),
        check_frontmatter_complete(),
        check_wmi_liveness(),
        check_dart_lane(),
    ]


def run_all(write=True):
    checks = _all_checks()
    status = {
        "ts": int(time.time()),
        "ok": all(c["ok"] for c in checks),
        "warnings": [f"{c['name']}: {c['detail']}" for c in checks if not c["ok"]],
        "checks": checks,
    }
    if write:
        try:
            META_DIR.mkdir(parents=True, exist_ok=True)
            STATUS_PATH.write_text(json.dumps(status, indent=2), encoding="utf-8")
        except OSError:
            pass
    return status


def boot_lines(max_status_age_h=26):
    """Failure lines for boot_ritual. Re-runs live if the status file is stale
    — so the sentinel's own death gets noticed too. Silent when healthy."""
    status = None
    try:
        if STATUS_PATH.exists():
            age_h = (time.time() - STATUS_PATH.stat().st_mtime) / 3600.0
            if age_h <= max_status_age_h:
                status = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        status = None
    if status is None:
        status = run_all(write=True)
    return [f"  • {w}" for w in status.get("warnings", [])]


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        status = run_all(write=True)
        print(json.dumps(status, indent=2))
        print("HEALTHY" if status["ok"] else f"{len(status['warnings'])} WARNING(S)")
    elif cmd == "status":
        if not STATUS_PATH.exists():
            print("no status yet — run: python health_sentinel.py run")
            return
        print(STATUS_PATH.read_text(encoding="utf-8"))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
