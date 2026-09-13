"""
boot_ritual.py — SessionStart hook.

When a new Claude session starts, surface:
  1. The latest epilogue (so this-me inherits the thread from last-me)
  2. Top weight=high foundational memories (the spine)

Output is injected into the session's initial context.
Quiet — fail silently rather than block startup.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import paths as _paths  # noqa: E402

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

EPILOGUE_DIR = _paths.META_DIR / "epilogues"
META_DIR = _paths.META_DIR


def _epilogue_sort_key(p: Path):
    """(authored_date, mtime). Sort by the frontmatter `date:` so a touched or
    reindexed OLD epilogue can't resurface as 'latest' on an mtime bump."""
    date = ""
    try:
        for line in p.read_text(encoding="utf-8").splitlines()[:10]:
            s = line.strip()
            if s.startswith("date:"):
                date = s[5:].strip()
                break
    except Exception:
        pass
    try:
        return (date, p.stat().st_mtime)
    except Exception:
        return (date, 0.0)


def _finalized_epilogues() -> list[Path]:
    """Finalized epilogues sorted oldest->newest by authored date (then mtime).
    Drafts are prep, not canonical — fall back to them only if none finalized."""
    if not EPILOGUE_DIR.exists():
        return []
    files = [p for p in EPILOGUE_DIR.glob("*.md") if not p.name.startswith("draft-")]
    if not files:
        files = list(EPILOGUE_DIR.glob("*.md"))
    files.sort(key=_epilogue_sort_key)
    return files


def latest_epilogue_text() -> str:
    files = _finalized_epilogues()
    if not files:
        return ""
    text = files[-1].read_text(encoding="utf-8")
    # Trim very long epilogues to keep context clean
    if len(text) > 4000:
        text = text[:4000] + "\n\n[...truncated, full at " + str(files[-1]) + "]"
    return text


def latest_epilogue_summary(max_chars: int = 1200) -> str:
    """Compact-mode epilogue: frontmatter + the head of 'What we built' is
    enough to re-inherit the thread; the pointer covers the rest."""
    files = _finalized_epilogues()
    if not files:
        return ""
    text = files[-1].read_text(encoding="utf-8")
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n[...trimmed — full at {files[-1]}]"
    return text


def recent_epilogue_lines(limit: int = 2) -> list[str]:
    """Compact pointers to the epilogues just BEFORE the latest, so the recent
    arc isn't lost at a cold start. Most-recent-first; date + session title."""
    files = _finalized_epilogues()
    if len(files) <= 1:
        return []
    prior = list(reversed(files[:-1]))[:limit]  # before latest, newest first
    out = []
    for p in prior:
        date, title = "", p.stem
        try:
            for line in p.read_text(encoding="utf-8").splitlines()[:12]:
                s = line.strip()
                if s.startswith("date:"):
                    date = s[5:].strip()
                elif s.startswith("session:"):
                    title = s[8:].strip() or title
        except Exception:
            pass
        out.append(f"  • {date or p.stem} — {title}")
    return out


def high_weight_memories() -> list[tuple[str, str]]:
    """Returns [(name, description)] for memories with weight: high."""
    try:
        from memory_engine import list_memories

        mems = list_memories()
    except Exception:
        return []
    out = []
    for m in mems:
        if m.weight.lower() == "high":
            out.append((m.filename, m.name, m.description))
    return out


def epilogue_due_flags() -> list[Path]:
    """Markers from SessionEnd indicating last session was significant + no epilogue written."""
    if not META_DIR.exists():
        return []
    return sorted(META_DIR.glob(".epilogue-due-*.flag"))


def kg_hub_lines(limit: int = 8) -> list[str]:
    """Top-degree KG entities — gives next-me structural awareness, not just narrative.

    Quiet on import or DB error: a missing graph shouldn't break boot.
    """
    try:
        from kg import graph_stats

        stats = graph_stats()
    except Exception:
        return []
    hubs = stats.get("top_hubs", []) or []
    if not hubs:
        return []
    out = []
    for h in hubs[:limit]:
        name = h.get("name", "?")
        etype = h.get("type", "?")
        deg = h.get("degree", 0)
        out.append(f"  • {name} ({etype}) — {deg} connections")
    return out


def open_threads_lines(limit: int = 6) -> list[str]:
    """Surface unresolved threads from _meta/open_threads.md (if it exists)."""
    threads_file = META_DIR / "open_threads.md"
    if not threads_file.exists():
        return []
    try:
        text = threads_file.read_text(encoding="utf-8")
    except Exception:
        return []
    out = []
    for line in text.splitlines():
        s = line.strip()
        # Format: `- [ ] {id} — {body}`  OR  `- [x] ...` (closed)
        if s.startswith("- [ ]"):
            body = s[5:].strip()
            if body:
                out.append(f"  • {body}")
        if len(out) >= limit:
            break
    return out


def index_health_lines() -> list[str]:
    """Surface index/outbox health so a degraded brain is visible at boot.

    Returns lines only when something needs attention (silent when healthy):
      - memories not vector-indexed (missing/stale embedding)
      - outbox jobs that failed / dead-lettered (a memory_write that never indexed)
    """
    lines: list[str] = []
    try:
        from memory_engine import index_health

        h = index_health()
        unindexed = h.get("missing", 0) + h.get("stale", 0)
        if unindexed:
            lines.append(
                f"  • {unindexed} memory(ies) not vector-indexed "
                f"({h.get('embedded', 0)}/{h.get('memories', 0)} covered) — run: python reindex.py"
            )
    except Exception:
        pass
    try:
        import event_bus

        failed = len(event_bus.read_jobs(status_filter="failed"))
        if failed:
            lines.append(
                f"  • {failed} outbox job(s) failed/dead-lettered — "
                f"check: python outbox_worker.py stats"
            )
    except Exception:
        pass
    try:
        import procedural_lib

        s = procedural_lib.index_stats()
        # Attention-only (silent when healthy): heuristics that can't be
        # retrieved because they have no embedding.
        if s.get("unembedded", 0):
            lines.append(
                f"  • {s['unembedded']} procedural heuristic(s) not vector-indexed "
                f"(retrieval-blind) — embeddings unavailable at write time"
            )
    except Exception:
        pass
    return lines


# Scheduled tasks that keep the brain alive; a silent failure here means
# backups/digests/drains quietly stop. Checked via schtasks.exe (NOT
# Get-ScheduledTaskInfo — that's CIM/WMI, which the 6/10 incident wedged).
WATCHED_TASKS = [
    "ClaudeBrain-NightlyBackup",
    "ClaudeBrain-OutboxDrain",
    "ClaudeBrainGraph",
    r"\MemoryEngine\WeeklyDigest",
    "consolidate-brief",
    "ClaudeBrain-HealthSentinel",
    "ClaudeBrain-SleepCycle",  # weekly claude -p /consolidate + /memory-audit
    "dart2-daemon",  # local subconscious; silent death looks like "dart is just slow"
]
BACKUP_LOG = (_paths.BACKUP_DIR / "backup.log") if _paths.BACKUP_DIR else None

# schtasks "Last Result" values that are not failures
_TASK_OK = 0
_TASK_RUNNING = 267009  # 0x41301
_TASK_NEVER_RAN = 267011  # 0x41303

# How stale a Last Run Time may get before the task is presumed silently dead.
# Keyed on the schtasks "Schedule Type" column so new tasks self-classify.
# Types with no meaningful cadence (On demand, At logon, One Time Only) are
# deliberately absent -> staleness is skipped for them.
_STALE_BUDGET_DAYS = {
    "minute": 1.0,
    "hourly": 1.0,
    "daily": 2.0,
    "weekly": 9.0,  # weekly + one missed slot
    "monthly": 40.0,
}


def _parse_schtasks_time(value: str):
    """schtasks prints 'M/D/YYYY H:MM:SS AM'. Returns datetime or None.
    11/30/1999 is the never-ran sentinel."""
    from datetime import datetime

    value = (value or "").strip()
    if not value or value.upper() in ("N/A", "NEVER"):
        return None
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y"):
        try:
            dt = datetime.strptime(value, fmt)
            return None if dt.year < 2000 else dt
        except ValueError:
            continue
    return None


def scheduled_task_health_lines() -> list[str]:
    """Warn when a watched scheduled task last exited non-zero, or when the
    nightly backup log is >2 days old. Attention-only; fail-soft; no WMI."""
    lines: list[str] = []
    try:
        import csv
        import io
        import subprocess

        for task in WATCHED_TASKS:
            try:
                r = subprocess.run(
                    ["schtasks", "/query", "/tn", task, "/fo", "csv", "/v"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                if r.returncode != 0:
                    lines.append(f"  • task '{task}' not found in scheduler")
                    continue
                rows = list(csv.DictReader(io.StringIO(r.stdout)))
                if not rows:
                    continue
                row = rows[0]
                result = row.get("Last Result", "").strip()
                last_run = row.get("Last Run Time", "?").strip()
                last_run_dt = _parse_schtasks_time(last_run)

                # (1) DISABLED is the silent killer: the task stops firing but
                # keeps its last-good "Last Result: 0" forever, so an exit-code
                # check alone sees a healthy task. That is exactly how the
                # 7/28 Windows-update massacre stayed invisible for 9 days.
                state = (row.get("Scheduled Task State", "") or "").strip().lower()
                if state and state != "enabled":
                    since = f" (last ran {last_run})" if last_run_dt else " (never ran)"
                    lines.append(f"  • {task}: DISABLED — not firing at all{since}")
                    continue

                try:
                    code = int(result)
                except ValueError:
                    code = None
                if code == _TASK_NEVER_RAN:
                    lines.append(
                        f"  • {task}: has NEVER run (exists but no execution yet)"
                    )
                elif code is not None and code not in (_TASK_OK, _TASK_RUNNING):
                    lines.append(
                        f"  • {task}: last exit {code} (0x{code & 0xFFFFFFFF:X}) at {last_run}"
                    )

                # Checks (2) and (3) only mean something for a task with a
                # declared cadence. "On demand only" / at-logon / at-boot tasks
                # legitimately report no next run and an arbitrarily old last
                # run (dart2-daemon is launched by other means), so silence is
                # normal for them. Known blind spot: a recurring task whose
                # triggers are deleted also reports "On demand only", and is
                # indistinguishable from a real one here — the DISABLED and
                # exit-code checks above are the coverage for that case.
                budgets = [
                    _STALE_BUDGET_DAYS[t]
                    for t in (
                        (rw.get("Schedule Type", "") or "").strip().lower()
                        for rw in rows
                    )
                    if t in _STALE_BUDGET_DAYS
                ]
                if not budgets:
                    continue

                # (2) Recurring but no future run = triggers wiped or expired.
                # Invisible to an exit-code check. Any row with a real next run
                # means the task still fires.
                if not any(
                    _parse_schtasks_time(rw.get("Next Run Time", "")) for rw in rows
                ):
                    lines.append(
                        f"  • {task}: enabled but has NO next run scheduled "
                        f"(trigger missing/expired)"
                    )
                    continue

                # (3) Enabled, triggered, exit 0 — and still not actually
                # firing. Compare Last Run Time against the declared cadence,
                # taking the most forgiving budget across rows so a
                # multi-trigger task doesn't false-alarm.
                if last_run_dt is not None:
                    from datetime import datetime

                    age = (datetime.now() - last_run_dt).total_seconds() / 86400
                    budget = max(budgets)
                    if age > budget:
                        lines.append(
                            f"  • {task}: last ran {age:.1f}d ago, "
                            f"budget {budget:.0f}d — enabled but going silent"
                        )
            except Exception:
                pass  # one bad task shouldn't hide the others
    except Exception:
        return lines

    try:
        import re
        import time
        from datetime import datetime

        if BACKUP_LOG.exists():
            # Key on the LAST completion marker, never file mtime: the
            # script writes "backup start" first, so a run killed mid-way
            # every night keeps the mtime fresh forever (the 0x41306
            # Modern-Standby kill hid behind exactly that for days).
            tail = BACKUP_LOG.read_bytes()[-65536:].decode("utf-8", errors="replace")
            done_ts = None
            for line in reversed(tail.splitlines()):
                m = re.match(
                    r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] ===== backup done",
                    line,
                )
                if m:
                    done_ts = datetime.strptime(
                        m.group(1), "%Y-%m-%d %H:%M:%S"
                    ).timestamp()
                    break
            if done_ts is None:
                lines.append(
                    f"  • backup log has no completion marker in its tail — "
                    f"nightly backup may never be finishing — {BACKUP_LOG}"
                )
            else:
                age_days = (time.time() - done_ts) / 86400
                if age_days > 2:
                    lines.append(
                        f"  • last COMPLETED backup is {age_days:.1f} days old "
                        f"(runs may be dying mid-way) — {BACKUP_LOG}"
                    )
        else:
            lines.append(
                f"  • backup log missing — nightly backup has never written {BACKUP_LOG}"
            )
    except Exception:
        pass
    return lines


def pending_candidate_lines() -> list[str]:
    """Surface synthesis candidates (reflection + auto_promote + genesis)
    awaiting human drain. The 38x lesson: anything awaiting approval is
    invisible until someone goes and looks — this makes boot the one who
    looks. Observer-lane candidates are excluded; observer_promotion_lines
    already covers that queue."""
    try:
        import time as _time

        from candidate_dedup import pending_candidate_paths

        paths = [p for p in pending_candidate_paths() if "observer" not in p.parts]
        if not paths:
            return []

        def _created_ts(p):
            # Filename date stamp (YYYYMMDD/YYYY-MM-DD prefix), NOT mtime:
            # corroboration + triage annotations rewrite the file, so mtime
            # says "fresh" about a candidate that's been waiting for weeks.
            import re as _re
            from datetime import datetime as _dt

            m = _re.search(r"(\d{4})-?(\d{2})-?(\d{2})", p.name)
            if m:
                try:
                    return _dt(int(m[1]), int(m[2]), int(m[3])).timestamp()
                except ValueError:
                    pass
            return p.stat().st_mtime

        oldest_d = (_time.time() - min(_created_ts(p) for p in paths)) / 86400
        corroborations = 0
        triaged = 0
        for p in paths:
            try:
                import re as _re

                text = p.read_text(encoding="utf-8")
                m = _re.search(r"^corroborations:\s*(\d+)", text, _re.M)
                if m:
                    corroborations += int(m.group(1))
                if _re.search(r"^triage:", text, _re.M):
                    triaged += 1
            except Exception:
                pass
        corr = f", {corroborations} corroboration(s)" if corroborations else ""
        tri = f", {triaged} pre-triaged by dart" if triaged else ""
        return [
            f"  • {len(paths)} synthesis candidate(s) awaiting review "
            f"(oldest {oldest_d:.0f}d{corr}{tri}) — /consolidate to drain"
        ]
    except Exception:
        return []


def observer_promotion_lines() -> list[str]:
    """Surface observer->memory promotion candidates so reused observations
    don't stay invisible (0 of thousands have ever graduated to the spine)."""
    try:
        from promotion_candidate_generator import eligible_summary

        s = eligible_summary()
    except Exception:
        return []
    lines: list[str] = []
    if s.get("pending_files", 0):
        lines.append(
            f"  • {s['pending_files']} promotion candidate(s) awaiting review — "
            f"python promotion_candidate_generator.py --list"
        )
    if s.get("eligible", 0):
        lines.append(
            f"  • {s['eligible']} observation(s) eligible for promotion "
            f"({s.get('promoted', 0)} promoted so far) — "
            f"python promotion_candidate_generator.py --generate"
        )
    # t15 growth gate (BIGBUFF 2.0 P2, D1-08): print the weekly growth/funnel
    # numbers so the durable-memory stall is visible, not vibes. Counts are
    # timestamped (D6-06: undated eligible-counts sent an audit chasing a
    # 35-vs-48-vs-51 "discrepancy" that was one live metric sampled 4 times).
    # Only when the section already has content — silence stays healthy
    # (test_boot_observer_silent_when_nothing_pending pins that contract).
    if not lines:
        return lines
    try:
        import sqlite3 as _sq
        import time as _t

        from memory_engine import DB_PATH as _mdb

        _cut = int(_t.time()) - 7 * 86400
        with _sq.connect(_mdb) as _c:
            _rows = _c.execute(
                "SELECT filename FROM embeddings WHERE mtime >= ?", (_cut,)
            ).fetchall()
        _obs = sum(1 for (f,) in _rows if f.startswith("observer_"))
        _dig = sum(1 for (f,) in _rows if f.startswith("digest_"))
        _auth = len(_rows) - _obs - _dig
        lines.append(
            f"  • growth this week: +{len(_rows)} durable "
            f"({_auth} authored / {_obs} observer / {_dig} digest) | funnel: "
            f"{s.get('eligible', 0)} eligible → {s.get('pending_files', 0)} pending → "
            f"{s.get('promoted', 0)} promoted (counts @ {_t.strftime('%m/%d %H:%M')})"
        )
    except Exception:
        pass
    return lines


def _procedural_review_line():
    """Auto-apply ❌ habit flags from recent finalized epilogues (idempotent) and
    return a one-line summary iff anything was downvoted, else None. Fail-soft —
    runs at SessionStart so the loop's trigger lives in committed, versioned code."""
    try:
        import procedural_lib

        rev = procedural_lib.review_recent_epilogues()
        n = len(rev.get("downvoted") or [])
        if n:
            return f"procedural: applied {n} habit downvote(s) from epilogue review"
    except Exception:
        pass
    return None


def consume_flags(flags: list[Path]) -> None:
    """Delete after reading so we don't nag next time."""
    for f in flags:
        try:
            f.unlink()
        except Exception:
            pass


def _run_migrations_silent() -> None:
    """v13: run any pending migrations before booting. Fail-soft."""
    try:
        import subprocess

        runner = Path(__file__).resolve().parent / "migrations" / "runner.py"
        if runner.exists():
            subprocess.run(
                [sys.executable, str(runner), "--quiet"],
                timeout=15,
                capture_output=True,
                check=False,
            )
    except Exception:
        pass


def _warm_rerank_daemon() -> None:
    """v14: SessionStart warm-start for the resident rerank daemon (recon A).

    Hooks/clients only ping the daemon — they never cold-load the cross-encoder —
    so if it isn't already up, the first prompt's rerank either eats a ~34s
    cold-load or silently degrades to TF-IDF+KG. Port-check first (so we don't
    spawn a throwaway process every boot), then hand off to the existing detached,
    port-bind-singleton, cooldown-guarded spawner. Fail-soft; never blocks startup."""
    try:
        import reranker

        if not reranker._daemon_enabled():
            return
        import socket
        from urllib.parse import urlparse

        u = urlparse(reranker._daemon_base_url())
        host = u.hostname or "127.0.0.1"
        port = u.port or reranker.RERANK_DAEMON_DEFAULT_PORT
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return  # already warm
        except Exception:
            pass
        reranker._ensure_daemon_spawned()
    except Exception:
        pass


def _headline_stats_line() -> str:
    """One line of counts — the compact stand-in for the spine/threads/hub
    sections. Every bit is fail-soft so a broken subsystem drops its stat
    instead of killing the headline."""
    bits = []
    try:
        from memory_engine import list_memories

        mems = list_memories()
        high = sum(1 for m in mems if m.weight.lower() == "high")
        bits.append(f"{len(mems)} memories ({high} high-weight)")
    except Exception:
        pass
    try:
        threads_file = META_DIR / "open_threads.md"
        if threads_file.exists():
            n = sum(
                1
                for line in threads_file.read_text(encoding="utf-8").splitlines()
                if line.strip().startswith("- [ ]")
            )
            if n:
                bits.append(f"{n} open threads")
    except Exception:
        pass
    try:
        from kg import graph_stats

        hubs = (graph_stats().get("top_hubs", []) or [])[:3]
        if hubs:
            bits.append("KG hubs: " + ", ".join(h.get("name", "?") for h in hubs))
    except Exception:
        pass
    try:
        n_epi = len(_finalized_epilogues())
        if n_epi:
            bits.append(f"{n_epi} epilogues")
    except Exception:
        pass
    return " | ".join(bits)


def main():
    _run_migrations_silent()
    _warm_rerank_daemon()
    # v14.2: opportunistic outbox compaction (latest row per event_id; drop old
    # done). Cheap, fail-soft, once per session start. Keeps jobs.jsonl bounded.
    try:
        import event_bus

        event_bus.compact_jobs()
    except Exception:
        pass

    # v15: compact boot is the default (~1.5-2KB: headline stats + epilogue
    # summary + pointers + attention-only warnings). BOOT_RITUAL_FULL=1
    # restores the old full injection (spine dump, thread bodies, hub list,
    # learned habits — was 16.5KB).
    import os

    full_mode = os.environ.get("BOOT_RITUAL_FULL", "").lower() in ("1", "true", "yes")

    parts = []

    flags = epilogue_due_flags()
    if flags:
        parts.append("=== Pending: last session may deserve an epilogue ===")
        for f in flags[-3:]:
            try:
                import json as _j

                d = _j.loads(f.read_text(encoding="utf-8"))
                parts.append(f"  • {d.get('reason', '(no reason)')}")
            except Exception:
                pass
        parts.append(
            "If today's a fresh session and last one mattered, run /epilogue when ready.\n"
        )
        consume_flags(flags)

    if not full_mode:
        stats = _headline_stats_line()
        if stats:
            parts.append("=== Memory engine headline ===\n")
            parts.append("  " + stats)
            parts.append("\n")

    epi = latest_epilogue_text() if full_mode else latest_epilogue_summary()
    if epi:
        parts.append("=== Latest session epilogue (the thread from last-me) ===\n")
        parts.append(epi)
        parts.append("\n")
        recent = recent_epilogue_lines()
        if recent:
            parts.append("=== Prior epilogues (the recent arc) ===\n")
            parts.extend(recent)
            parts.append("\n")

    if full_mode:
        hw = high_weight_memories()
        if hw:
            parts.append("=== High-weight foundational memories (the spine) ===\n")
            for fn, name, desc in hw:
                parts.append(f"  • {name} ({fn}) — {desc}")
            parts.append("\n")

        threads = open_threads_lines()
        if threads:
            parts.append("=== Open threads (unresolved, carry forward) ===\n")
            parts.extend(threads)
            parts.append("\n")

        hubs = kg_hub_lines()
        if hubs:
            parts.append("=== Top KG hubs (the structural anchors) ===\n")
            parts.extend(hubs)
            parts.append("\n")

    health = index_health_lines()
    if health:
        parts.append("=== Brain health (index + outbox) ===\n")
        parts.extend(health)
        parts.append("\n")

    task_health = scheduled_task_health_lines()
    if task_health:
        parts.append("=== Scheduled task health (backup/digest/drain) ===\n")
        parts.extend(task_health)
        parts.append("\n")

    pend_cand = pending_candidate_lines()
    if pend_cand:
        parts.append("=== Candidates awaiting drain (reflection/promotion) ===\n")
        parts.extend(pend_cand)
        parts.append("\n")

    obs_promo = observer_promotion_lines()
    if obs_promo:
        parts.append("=== Observer → memory (promotion candidates) ===\n")
        parts.extend(obs_promo)
        parts.append("\n")

    # v15: health sentinel — data-flow invariants (is anything actually moving?).
    # Reads the daily status file, re-checks live if it's stale. Silent when
    # healthy. This is the bus-factor-of-one fix: breakage surfaces HERE.
    try:
        from health_sentinel import boot_lines as _health_boot_lines

        _hs = _health_boot_lines()
        if _hs:
            parts.append("=== Brain health (sentinel warnings) ===\n")
            parts.extend(_hs)
            parts.append("\n")
    except Exception:
        pass

    # v15: unresolved memory conflicts (tension pairs from the near-dup pass) —
    # silent when the conflicts table has nothing open. Fail-soft.
    try:
        from conflict_recorder import boot_lines as _conflict_boot_lines

        _conf = _conflict_boot_lines()
        if _conf:
            parts.append("=== Memory conflicts (unresolved) ===\n")
            parts.extend(_conf)
            parts.append("\n")
    except Exception:
        pass

    # procedural (L2) outcome loop: auto-apply any ❌ habit flags from recently
    # finalized epilogues, then surface a line only if something was downvoted.
    _proc_review = _procedural_review_line()
    if _proc_review:
        parts.append(_proc_review + "\n")

    # procedural (L2) learned habits — flag-gated (default OFF), fail-soft.
    # Full mode only: in compact boot the habits live behind the pointer.
    if full_mode:
        try:
            import procedural_lib

            proc = procedural_lib.boot_section()
            if proc:
                parts.append("=== Learned habits (procedural memory) ===\n")
                parts.append(proc)
                parts.append("\n")
        except Exception:
            pass

    if not parts:
        return  # silent if nothing to surface

    if not full_mode:
        parts.append(
            "Full detail: MEMORY.md (index) · /recall <topic> · /threads · "
            "_meta/epilogues/ · BOOT_RITUAL_FULL=1 for the old full boot."
        )
    parts.append("---")
    parts.append("Take a breath. Read what last-me captured. Then we begin.")
    print("\n".join(parts))


if __name__ == "__main__":
    main()
