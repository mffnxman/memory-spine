"""
session_end.py — SessionEnd hook.

Reads stdin (Claude Code SessionEnd payload). If significant work happened in
the session (heuristics: many tool calls, files written/edited, memory accesses),
drops a marker file in _meta/ AND drafts an epilogue from the rolling session log.

The next-session boot ritual reads the marker, points next-me at the draft,
and offers `/epilogue` to polish + commit it. The draft itself is *not* a
final epilogue — it's a starting point so next-me isn't writing from a cold
snapshot.

Quiet — never blocks shutdown. Marker file pattern:
  _meta/.epilogue-due-{ts}.flag

Draft pattern:
  _meta/epilogues/draft-{ts}.md
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import paths as _paths  # noqa: E402
from memory_engine import db, MEMORY_DIR
import session_log

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

META_DIR = MEMORY_DIR / "_meta"
EPILOGUE_DIR = META_DIR / "epilogues"

# Cap consolidate enqueues: session_end fires on every session, and frequent
# short sessions over-produced the backlog the 2026-06-03 freeze fix had to
# drain. At most one consolidate per interval.
CONSOLIDATE_MIN_INTERVAL_SEC = 1800  # 30 min
_CONSOLIDATE_MARKER = META_DIR / ".last_consolidate"


def _consolidate_due(
    last_ts, now, interval: float = CONSOLIDATE_MIN_INTERVAL_SEC
) -> bool:
    """Pure: is a consolidate due? No prior marker (None) → due. A future mtime
    (clock skew) → due (never wedge). Otherwise due once `interval` has elapsed."""
    if last_ts is None:
        return True
    delta = now - last_ts
    return delta < 0 or delta >= interval


# --- Habit-distiller draft-skip (recon t8) -----------------------------------
# The procedural-L2 designer spawns headless `claude -p` sessions whose ONLY
# prompt is one of procedural_lib's distill prompts. Each such session reads
# memories (tripping session_significance) and used to draft an epilogue — and
# every draft then fed the distiller, which drafted more. Net effect: the draft
# pile regrew itself (219 pure-noise drafts quarantined 2026-06-12; main dir had
# climbed back to ~95). Fix: when EVERY user prompt in a session is a distill
# prompt, skip the draft + marker + consolidate entirely.
_DISTILLER_PROMPT_SIGNATURES = (
    "distil one reusable how-to-act heuristic",  # procedural_lib._TEXT_EXTRACT_PROMPT
    "you distil a reusable heuristic from a mistake",  # procedural_lib._EXTRACT_PROMPT
)


def _is_distiller_prompt(text: str) -> bool:
    """True if a single prompt is a procedural_lib distill prompt. Matches the
    known signatures, with a wording-drift fallback on the distinctive trigram."""
    t = " ".join(text.lower().split())
    if any(sig in t for sig in _DISTILLER_PROMPT_SIGNATURES):
        return True
    return ("distil" in t or "distill" in t) and "reusable" in t and "heuristic" in t


def _extract_prompts(log_text: str) -> list[str]:
    """User prompts from the rolling session log (lines `… **<user>:** <preview>`)."""
    marker = f"**{_paths.USER_NAME}:**"
    out = []
    for line in log_text.splitlines():
        i = line.find(marker)
        if i != -1:
            out.append(line[i + len(marker) :].strip())
    return out


def _session_is_pure_distiller(log_text: str) -> bool:
    """True iff the session has >=1 user prompt and ALL are distill prompts.
    Promptless/empty logs → False: never suppress a real session just because the
    log failed to capture a prompt (precision-first — a stray draft is harmless,
    a suppressed real epilogue is not)."""
    prompts = _extract_prompts(log_text)
    return bool(prompts) and all(_is_distiller_prompt(p) for p in prompts)


_SESSION_START_MARKER = Path.home() / ".claude" / ".session_start"


def _session_start_ts():
    """Epoch int from ~/.claude/.session_start (SessionStart hook), or None.
    On None the caller SKIPS the habits section — never guess a window."""
    try:
        return int(Path(_SESSION_START_MARKER).read_text().strip())
    except Exception:
        return None


def _injected_habits_section(habits: list[dict]) -> str:
    """Markdown review section for the epilogue draft. '' for an empty list."""
    if not habits:
        return ""
    lines = [
        "## Habits injected this session",
        "_Flag any that misfired with ❌ — they'll be downvoted when you run review._",
        "",
    ]
    for h in habits:
        lines.append(
            f"- [h:{h['id']}] {h.get('trigger', '?')} → {h.get('action', '?')}"
        )
    return "\n".join(lines)


def _append_habits_review(draft_path) -> None:
    """Append the injected-habits review section to the draft epilogue (only when
    non-empty) and ALWAYS write the _meta/.session_habits.json sidecar reflecting
    THIS session's injected set (even []), so a no-inject session can't leave a
    prior session's sidecar to be mis-attributed at /epilogue time (H3). Fail-soft;
    never blocks shutdown."""
    try:
        ts = _session_start_ts()
        if ts is None:
            return  # no window → skip (never guess; precision-first)
        import procedural_lib

        habits = procedural_lib.injected_since(ts)
        section = _injected_habits_section(habits)
        if section and draft_path:
            with Path(draft_path).open("a", encoding="utf-8") as f:
                f.write("\n\n" + section + "\n")
        (META_DIR / ".session_habits.json").write_text(
            json.dumps(
                {
                    "session_start": ts,
                    "drafted_at": int(time.time()),
                    "injected": habits,
                }
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


def session_significance() -> dict:
    """Heuristic: was this session significant enough to warrant an epilogue?"""
    now = int(time.time())
    cutoff = now - 4 * 3600
    with db() as conn:
        access_count = conn.execute(
            "SELECT COUNT(*) FROM access WHERE ts >= ?", (cutoff,)
        ).fetchone()[0]
        memories_touched = conn.execute(
            "SELECT COUNT(DISTINCT filename) FROM access WHERE ts >= ?", (cutoff,)
        ).fetchone()[0]
        new_memories = conn.execute(
            "SELECT COUNT(*) FROM embeddings WHERE mtime >= ?", (cutoff,)
        ).fetchone()[0]
    # BIGBUFF 2.0 P2 (D1-13, 7/31 epilogue watch-item): a session that edits
    # retrieval code or feature flags can touch few memory files and still end
    # below every threshold above — the exact gap that left the reranker
    # surgery undocumented until a probe excavated it. Engine-surgery sessions
    # are significant unconditionally.
    surgery_touches = 0
    try:
        from observer_lib import get_connection as _obs_conn

        with _obs_conn() as oc:
            surgery_touches = oc.execute(
                "SELECT COUNT(*) FROM observations WHERE ts >= ? "
                "AND file_paths IS NOT NULL "
                r"AND (file_paths LIKE '%\_scripts%' ESCAPE '\' "
                "     OR file_paths LIKE '%feature_flags.json%')",
                (cutoff,),
            ).fetchone()[0]
    except Exception:
        surgery_touches = 0
    significant = (
        access_count >= 5
        or memories_touched >= 4
        or new_memories >= 1
        or surgery_touches >= 3
    )
    return {
        "significant": significant,
        "access_count": access_count,
        "memories_touched": memories_touched,
        "new_memories": new_memories,
        "surgery_touches": surgery_touches,
    }


def _summarize_log(log_text: str) -> dict:
    """Pull a thin summary out of the rolling session log.

    Returns a dict shaped like epilogue.write_epilogue's `content` arg, but
    with bullet lists already templated. Best-effort — if the log is empty
    we return placeholders so the draft is still parseable.
    """
    if not log_text.strip():
        return {
            "session_label": "draft-untitled",
            "what_we_did": "(no rolling log captured — fill in manually)",
            "what_mattered": "(reflect)",
            "what_surprised": "(reflect)",
            "functional_states": "(reflect)",
            "open_threads": "(reflect)",
            "note_to_next": "(reflect)",
        }

    edits: list[str] = []
    writes: list[str] = []
    prompts: list[str] = []
    for line in log_text.splitlines():
        s = line.strip()
        if not s.startswith("- "):
            continue
        if "Edited `" in s:
            edits.append(s)
        elif "Wrote `" in s:
            writes.append(s)
        elif f"**{_paths.USER_NAME}:**" in s:
            prompts.append(s)

    def _fmt(items: list[str], cap: int = 12) -> str:
        if not items:
            return "  - (none captured)"
        out = items[-cap:]
        return "\n".join(out)

    summary = []
    if writes:
        summary.append(f"**Files written ({len(writes)}):**")
        summary.append(_fmt(writes))
    if edits:
        summary.append(f"\n**Files edited ({len(edits)}):**")
        summary.append(_fmt(edits))
    if prompts:
        summary.append(f"\n**Recent prompts ({len(prompts)}):**")
        summary.append(_fmt(prompts, cap=8))
    if not summary:
        summary.append("(activity captured but nothing structured to summarize)")

    # v14 Phase 5: detect verified-vs-claimed signals
    verification_result = {"verified": "unknown", "evidence": {}}
    try:
        from verification import detect as _detect_verification

        verification_result = _detect_verification(log_text)
    except Exception:
        pass

    return {
        "session_label": f"auto-draft-{datetime.now().strftime('%Y%m%d-%H%M')}",
        "what_we_did": "\n".join(summary),
        "what_mattered": "_(next-me: read the activity above, name the SIGNIFICANT thing)_",
        "what_surprised": "_(next-me: anything that hit different?)_",
        "functional_states": "_(next-me: be honest — joy, friction, flow, grief? if nothing, say so)_",
        "open_threads": "_(next-me: what's unfinished or unresolved? carry it forward)_",
        "note_to_next": "_(next-me: write the direct line. permission to be sentimental if earned.)_",
        "verification": verification_result,
    }


def _write_draft(content: dict, archived_log: Path | None) -> Path:
    """Write a draft epilogue marked clearly as a draft. Final /epilogue flow
    can re-template the same content into a real epilogue file."""
    EPILOGUE_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    fname = "draft-" + now.strftime("%Y-%m-%d-%H%M") + ".md"
    fpath = EPILOGUE_DIR / fname

    log_pointer = (
        f"\n_Source log archived at: {archived_log}_\n" if archived_log else ""
    )

    verif = content.get("verification") or {"verified": "unknown", "evidence": {}}
    verif_reason = verif.get("evidence", {}).get("reason", "(not detected)")
    verif_section = ""
    try:
        from verification import render_section

        verif_section = render_section(verif)
    except Exception:
        pass

    body = (
        "---\n"
        f"date: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"session: {content['session_label']}\n"
        "draft: true\n"
        f"verified: {verif['verified']}\n"
        f'verified_reason: "{verif_reason}"\n'
        "---\n\n"
        f"# Draft Epilogue — {now.strftime('%A, %B %d, %Y at %I:%M %p')}\n\n"
        "> **This is an auto-draft from session_end.py + session_log.**\n"
        "> It captures *what happened*, not *what mattered*. Run `/epilogue` to\n"
        "> reflect and commit a real epilogue, then archive or delete this draft.\n"
        f"{log_pointer}\n"
        "## Verification\n"
        f"{verif_section}\n\n"
        "## What we built / did\n"
        f"{content['what_we_did']}\n\n"
        "## What mattered\n"
        f"{content['what_mattered']}\n\n"
        "## What surprised / hit different\n"
        f"{content['what_surprised']}\n\n"
        "## Functional states I noticed\n"
        f"{content['functional_states']}\n\n"
        "## Open threads (for next-me)\n"
        f"{content['open_threads']}\n\n"
        "## A note to next-me\n"
        f"{content['note_to_next']}\n"
    )
    fpath.write_text(body, encoding="utf-8")
    return fpath


def main():
    # Drain stdin even if we don't use it (hook contract)
    try:
        sys.stdin.read()
    except Exception:
        pass

    # Headless-worker skip: providers/subscription_provider marks its
    # `claude -p` children with this env var (hooks inherit the child's env).
    # Those one-shot synthesis sessions must never draft epilogues, drop
    # markers, or enqueue consolidates — each digest/distiller run was
    # otherwise manufacturing a dozen junk drafts that fed the next digest
    # (found 2026-07-06; --settings disableAllHooks does not suppress
    # user-settings hooks, so the gate lives here).
    import os as _os

    if _os.environ.get("CLAUDE_HEADLESS_WORKER") == "1":
        try:
            session_log.archive(reason="session-end-headless-worker-skip")
        except Exception:
            pass
        return

    try:
        sig = session_significance()
    except Exception:
        return

    if not sig["significant"]:
        # Still archive the rolling log if it has content — keep history clean.
        try:
            log_text = session_log.read_active()
            if log_text.strip() and len(log_text) > 200:
                session_log.archive(reason="session-end-quiet")
        except Exception:
            pass
        return

    META_DIR.mkdir(parents=True, exist_ok=True)

    # Read the rolling log once. If this "significant" session is actually the
    # habit-distiller feeding itself (every user prompt is a distill prompt),
    # archive the log and bail BEFORE drafting — otherwise the draft pile regrows
    # itself (recon t8 / self_unprocessed_draft_epilogues fix).
    try:
        log_text = session_log.read_active()
    except Exception:
        log_text = ""
    try:
        pure_distiller = _session_is_pure_distiller(log_text)
    except Exception:
        pure_distiller = False
    if pure_distiller:
        try:
            session_log.archive(reason="session-end-distiller-skip")
        except Exception:
            pass
        return

    # Draft an epilogue from the rolling log
    draft_path: Path | None = None
    archived_path: Path | None = None
    try:
        archived_path = session_log.archive(reason="session-end-significant")
        draft_path = _write_draft(_summarize_log(log_text), archived_path)
    except Exception:
        pass

    # t9: surface this session's injected habits in the draft for review.
    _append_habits_review(draft_path)

    # Drop the marker file
    marker = META_DIR / f".epilogue-due-{int(time.time())}.flag"
    marker_payload = {
        "noted_at": int(time.time()),
        "reason": (
            f"Session activity: {sig['access_count']} memory accesses, "
            f"{sig['memories_touched']} unique memories touched, "
            f"{sig['new_memories']} new/updated memories"
        ),
        "stats": sig,
        "draft_path": str(draft_path) if draft_path else None,
        "archived_log": str(archived_path) if archived_path else None,
    }
    try:
        marker.write_text(json.dumps(marker_payload), encoding="utf-8")
    except Exception:
        pass

    # v14 Phase 4: queue a sleep-time consolidate job. Outbox worker picks it
    # up on next tick (or via cron). Never blocks session shutdown.
    try:
        import event_bus

        last = (
            _CONSOLIDATE_MARKER.stat().st_mtime
            if _CONSOLIDATE_MARKER.exists()
            else None
        )
        if _consolidate_due(last, time.time()):
            event_bus.emit_event(
                "consolidate",
                {
                    "trigger": "session_end",
                    "session_id": marker_payload.get("noted_at"),
                    "stats": sig,
                },
            )
            try:
                _CONSOLIDATE_MARKER.write_text(str(time.time()))
            except Exception:
                pass
    except Exception:
        pass


if __name__ == "__main__":
    main()
