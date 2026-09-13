"""
weekly_digest.py — orchestrate the weekly digest pipeline.

Reads epilogues for a target ISO week, runs three subagent prompts (theme
extractor, thread tracker, chapter writer), and produces a chapter file at
`_meta/digests/YYYY-Wnn.md`.

Subagent prompts live in `digest_subagents/`. Each call goes through
`tier_router` so model choice is centralized. Telemetry to router_log.jsonl.

The actual LLM call is gated on ANTHROPIC_API_KEY presence; without it we
generate a structural skeleton-chapter from raw inputs (no AI synthesis) so
the pipeline produces useful output even in offline mode.

Usage:
  python weekly_digest.py --week 2026-W21
  python weekly_digest.py --last-week        # default mode
  python weekly_digest.py --dry-run          # show what would be processed
  python weekly_digest.py --backfill 4       # backfill last N weeks
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import paths as _paths  # noqa: E402

import tier_router  # noqa: E402

META_DIR = _paths.META_DIR
MEMORY_DIR = _paths.MEMORY_DIR
EPILOGUE_DIR = META_DIR / "epilogues"
DIGEST_DIR = META_DIR / "digests"
SUBAGENT_DIR = Path(__file__).resolve().parent / "digest_subagents"
THREADS_FILE = META_DIR / "open_threads.md"

DATE_RE = re.compile(r"^(?:draft-)?(\d{4})-(\d{2})-(\d{2})")


# ─── Week math ──────────────────────────────────────────────────────────────
def iso_week_bounds(iso_week: str) -> tuple[date, date]:
    """'2026-W21' -> (Monday, Sunday) dates."""
    year, _, w = iso_week.partition("-W")
    year, week = int(year), int(w)
    monday = date.fromisocalendar(year, week, 1)
    sunday = date.fromisocalendar(year, week, 7)
    return (monday, sunday)


def current_iso_week_str() -> str:
    iso = date.today().isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def last_iso_week_str() -> str:
    last = date.today() - timedelta(days=7)
    iso = last.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def epilogues_in_week(iso_week: str) -> list[Path]:
    if not EPILOGUE_DIR.exists():
        return []
    start, end = iso_week_bounds(iso_week)
    out = []
    for p in sorted(EPILOGUE_DIR.glob("*.md")):
        m = DATE_RE.match(p.name)
        if not m:
            continue
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except Exception:
            continue
        if start <= d <= end:
            out.append(p)
    return out


# ─── LLM calls (gated on API key presence) ──────────────────────────────────
def _call_llm(task_type: str, system_prompt: str, user_prompt: str) -> str | None:
    """Returns LLM text or None if no provider available."""
    try:
        from providers import get_provider

        provider_name, model, params = tier_router.route(task_type)
        prov = get_provider(provider_name)
        if not prov.health_check():
            # Fall back to anthropic if local unhealthy
            if provider_name != "anthropic":
                prov = get_provider("anthropic")
                if not prov.health_check():
                    return None
            else:
                return None
        resp = prov.generate(
            user_prompt,
            model=model,
            max_tokens=params.get("max_tokens", 4096),
            system=system_prompt,
        )
        tier_router.log_use(
            task_type, model, resp.tokens_in, resp.tokens_out, provider=provider_name
        )
        return resp.text
    except Exception as e:
        print(f"  WARN: LLM call failed for {task_type}: {e}", file=sys.stderr)
        return None


def _read_subagent_prompt(slug: str) -> str:
    p = SUBAGENT_DIR / f"{slug}.md"
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8")


# ─── Skeleton-mode (offline fallback) ──────────────────────────────────────
def _skeleton_chapter(
    iso_week: str, epilogues: list[Path], prior_chapter: str | None
) -> str:
    start, end = iso_week_bounds(iso_week)
    parts = []
    parts.append("---")
    parts.append(f"week: {iso_week}")
    parts.append(f"date_range: {start.isoformat()} to {end.isoformat()}")
    parts.append(f"sessions: {len(epilogues)}")
    parts.append("generated_by: skeleton (no LLM provider)")
    parts.append("---")
    parts.append("")
    parts.append(f"# Week {iso_week} — {len(epilogues)} sessions")
    parts.append("")
    if prior_chapter:
        prior_week_m = re.search(r"^week:\s*(\S+)", prior_chapter, re.MULTILINE)
        if prior_week_m:
            parts.append(
                f"Continuing from [{prior_week_m.group(1)}](./{prior_week_m.group(1)}.md).\n"
            )
    if not epilogues:
        parts.append("_No epilogues found for this week._")
    else:
        parts.append("## Sessions\n")
        for p in epilogues:
            # Grab the H1 if present
            try:
                text = p.read_text(encoding="utf-8")
                h1 = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
                title = h1.group(1) if h1 else p.stem
                parts.append(f"- **{p.stem}** — {title}")
            except Exception:
                parts.append(f"- **{p.stem}**")
    parts.append("")
    parts.append(
        "_(LLM-generated narrative not available — set ANTHROPIC_API_KEY or run d'Artagnan to enable synthesis.)_"
    )
    return "\n".join(parts)


# ─── Main pipeline ─────────────────────────────────────────────────────────
def _promote_digest_to_memory(
    iso_week: str, digest_path: Path, mode: str, n_epilogues: int
) -> Path | None:
    """v14 Phase 6: write a pointer memory at memory/digest_YYYY-Wnn.md so the
    digest becomes a first-class searchable memory.

    Only promote LLM-mode digests — skeleton-mode chapters lack narrative
    content and would pollute retrieval with empty pointers.
    """
    if mode != "llm":
        return None
    start, end = iso_week_bounds(iso_week)
    pointer_name = f"digest_{iso_week.lower()}.md"
    pointer_path = MEMORY_DIR / pointer_name

    # Extract first paragraph of digest body as the description
    try:
        body = digest_path.read_text(encoding="utf-8")
    except Exception:
        return None
    paragraphs = [
        p.strip() for p in body.split("\n\n") if p.strip() and not p.startswith("---")
    ]
    first_real = next((p for p in paragraphs if not p.startswith("#")), "")
    description = first_real.replace("\n", " ")[:240]
    if not description:
        description = f"Synthesized arc of week {iso_week} — {n_epilogues} sessions"

    # Find prior week digest for `related` link
    start_dt, _ = iso_week_bounds(iso_week)
    prior_dt = start_dt - timedelta(days=7)
    prior_iso = prior_dt.isocalendar()
    prior_week = f"{prior_iso.year}-W{prior_iso.week:02d}"
    related = f"digest_{prior_week.lower()}"

    frontmatter = (
        "---\n"
        f"name: Week {iso_week} Digest\n"
        f"description: {description}\n"
        "type: digest\n"
        "weight: medium\n"
        "platform_source: claude_code\n"
        f"event_date: {start.isoformat()}\n"
        f"related: {related}\n"
        "verified: true\n"
        "---\n\n"
    )
    body_md = (
        f"Synthesized chapter from session epilogues {start.isoformat()} to {end.isoformat()}. "
        f"{n_epilogues} sessions covered.\n\n"
        f"Full text: `_meta/digests/{iso_week}.md`\n\n"
        f"{first_real[:600]}{'...' if len(first_real) > 600 else ''}\n"
    )
    pointer_path.write_text(frontmatter + body_md, encoding="utf-8")
    return pointer_path


def generate_digest(iso_week: str, dry_run: bool = False) -> dict:
    epi_paths = epilogues_in_week(iso_week)
    if not epi_paths:
        return {"week": iso_week, "epilogues": 0, "skipped": "no epilogues"}

    # Load epilogue text
    epilogue_corpus = []
    for p in epi_paths:
        try:
            epilogue_corpus.append(f"=== {p.name} ===\n{p.read_text(encoding='utf-8')}")
        except Exception:
            continue
    corpus_text = "\n\n".join(epilogue_corpus)

    # Load prior chapter for continuity
    prior_chapter = None
    start, _ = iso_week_bounds(iso_week)
    prior_monday = start - timedelta(days=7)
    prior_iso = prior_monday.isocalendar()
    prior_week_str = f"{prior_iso.year}-W{prior_iso.week:02d}"
    prior_path = DIGEST_DIR / f"{prior_week_str}.md"
    if prior_path.exists():
        try:
            prior_chapter = prior_path.read_text(encoding="utf-8")
        except Exception:
            pass

    # Load open threads
    threads_text = ""
    if THREADS_FILE.exists():
        try:
            threads_text = THREADS_FILE.read_text(encoding="utf-8")
        except Exception:
            pass

    if dry_run:
        return {
            "week": iso_week,
            "epilogues": len(epi_paths),
            "prior_chapter": bool(prior_chapter),
            "open_threads_loaded": bool(threads_text),
            "would_write": str(DIGEST_DIR / f"{iso_week}.md"),
        }

    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DIGEST_DIR / f"{iso_week}.md"

    # Kill-safe write order (BIGBUFF 2.0 D1-01: W28+W31 were lost because the
    # three LLM calls below ran 45+ min with nothing on disk, and the task
    # kill left zero output). Skeleton lands FIRST; the LLM payload upgrades
    # it in place. A killed run now degrades to a skeleton, never to a gap.
    if not out_path.exists():
        out_path.write_text(
            _skeleton_chapter(iso_week, epi_paths, prior_chapter), encoding="utf-8"
        )

    # Try the LLM pipeline first
    themes_json = _call_llm(
        "memory_classification",
        _read_subagent_prompt("theme_extractor"),
        f"Open threads at start of week:\n{threads_text}\n\nEpilogues:\n{corpus_text}",
    )
    threads_json = _call_llm(
        "memory_classification",
        _read_subagent_prompt("thread_tracker"),
        f"Prior open threads:\n{threads_text}\n\nThis week's epilogues:\n{corpus_text}",
    )
    chapter_md = _call_llm(
        "weekly_digest_chapter",
        _read_subagent_prompt("chapter_writer"),
        f"Prior chapter for continuity:\n{prior_chapter or '(none)'}\n\n"
        f"Open threads:\n{threads_text}\n\n"
        f"Themes JSON:\n{themes_json or '(skeleton)'}\n\n"
        f"Threads JSON:\n{threads_json or '(skeleton)'}\n\n"
        f"This week's epilogues:\n{corpus_text}",
    )

    if chapter_md is None:
        # Skeleton mode
        out_text = _skeleton_chapter(iso_week, epi_paths, prior_chapter)
        out_path.write_text(out_text, encoding="utf-8")
        # v14 Phase 6: skeleton digests are NOT promoted (no real narrative content)
        return {
            "week": iso_week,
            "epilogues": len(epi_paths),
            "mode": "skeleton",
            "path": str(out_path),
            "promoted": False,
        }

    # Full LLM mode — wrap with frontmatter
    start, end = iso_week_bounds(iso_week)
    frontmatter = (
        "---\n"
        f"week: {iso_week}\n"
        f"date_range: {start.isoformat()} to {end.isoformat()}\n"
        f"sessions: {len(epi_paths)}\n"
        f"prior_chapter: {prior_week_str if prior_chapter else 'none'}\n"
        "generated_by: weekly_digest.py\n"
        "---\n\n"
    )
    payload = frontmatter + chapter_md.strip() + "\n"
    if themes_json:
        payload += (
            "\n\n---\n\n## Themes (auto-extracted)\n\n```json\n"
            + themes_json.strip()
            + "\n```\n"
        )
    if threads_json:
        payload += (
            "\n\n## Threads (auto-tracked)\n\n```json\n"
            + threads_json.strip()
            + "\n```\n"
        )
    out_path.write_text(payload, encoding="utf-8")
    # v14 Phase 6: promote to first-class memory file for retrieval
    pointer = _promote_digest_to_memory(
        iso_week, out_path, mode="llm", n_epilogues=len(epi_paths)
    )
    return {
        "week": iso_week,
        "epilogues": len(epi_paths),
        "mode": "llm",
        "path": str(out_path),
        "promoted": str(pointer) if pointer else False,
    }


def _keepawake(on: bool) -> None:
    """BIGBUFF 2.0 P3 (D4-07): hold ES_SYSTEM_REQUIRED while generating so
    Modern Standby can't kill a long LLM run mid-digest (the 0xC000013A class).
    No-op off Windows / on failure."""
    try:
        import ctypes

        es_continuous, es_system_required = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(
            es_continuous | (es_system_required if on else 0)
        )
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="Weekly digest generator")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--week", help="ISO week e.g. 2026-W21")
    g.add_argument("--last-week", action="store_true", help="Generate for last week")
    g.add_argument("--backfill", type=int, help="Backfill last N weeks (regenerates)")
    g.add_argument(
        "--fill-gaps",
        type=int,
        help="Self-healing mode: last N completed weeks, skipping existing digests "
        "(BIGBUFF 2.0 D1-01 — the scheduled lane uses this so a killed week gets "
        "retried next run instead of being skipped forever)",
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    weeks_to_process: list[str] = []
    if args.week:
        weeks_to_process = [args.week]
    elif args.backfill or args.fill_gaps:
        # Last N completed weeks (don't include current incomplete week)
        n = args.backfill or args.fill_gaps
        for i in range(1, n + 1):
            d = date.today() - timedelta(days=7 * i)
            iso = d.isocalendar()
            weeks_to_process.append(f"{iso.year}-W{iso.week:02d}")
        weeks_to_process.reverse()
        if args.fill_gaps:

            def _needs_fill(w: str) -> bool:
                p = DIGEST_DIR / f"{w}.md"
                if not p.exists():
                    return True
                # A skeleton digest is a placeholder, not a chapter — retry it
                # so the lane upgrades it in place once an LLM provider is back.
                try:
                    return "generated_by: skeleton" in p.read_text(encoding="utf-8")
                except OSError:
                    return False

            weeks_to_process = [w for w in weeks_to_process if _needs_fill(w)]
            if not weeks_to_process:
                print(json.dumps({"fill_gaps": "no missing weeks"}))
    else:
        weeks_to_process = [last_iso_week_str()]

    _keepawake(True)
    try:
        for w in weeks_to_process:
            result = generate_digest(w, dry_run=args.dry_run)
            print(json.dumps(result, indent=2))
    finally:
        _keepawake(False)


if __name__ == "__main__":
    main()
