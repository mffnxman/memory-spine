"""reconsolidate.py — refresh hot-but-stale memories with accumulated evidence
(v15, 2026-07-09).

Finding from the brain-viz session: the most-recalled memories (650+ recall
events) were untouched since May — the heaviest load-bearing neurons were the
stalest. Biology reconsolidates memories on heavy retrieval; this pass does it
at sleep time:

  1. select_targets(): memories with >= HOT_MIN access events whose file
     mtime is older than STALE_DAYS.
  2. For AUTO types (project / reference): gather epilogue mentions newer
     than the memory's mtime, LLM-redraft the body to integrate them,
     provenance-snapshot, rewrite in place with `reconsolidated_at:` stamped,
     emit reindex. Capped at CAP per cycle (LLM cost bound).
  3. IDENTITY types (self / user / feedback) are NEVER machine-rewritten —
     they get flagged in the summary for human-gated refresh (/consolidate).

Reversible: every rewrite has a pre-edit provenance snapshot.

CLI:
    python reconsolidate.py dry-run   # show targets + evidence counts
    python reconsolidate.py run       # apply (LLM-backed)
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import paths as _paths  # noqa: E402

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

MEMORY_DIR = _paths.MEMORY_DIR
DEFAULT_DB = MEMORY_DIR / "_meta" / "memory.db"
DEFAULT_EPILOGUES = MEMORY_DIR / "_meta" / "epilogues"

HOT_MIN = 20  # access events to count as "hot"
STALE_DAYS = 45  # file untouched this long counts as "stale"
CAP = 2  # LLM rewrites per cycle
AUTO_TYPES = {"project", "reference"}
IDENTITY_TYPES = {"self", "user", "feedback"}
DAY = 86400.0

REDRAFT_PROMPT = """This is a long-term memory file from the user and Claude's homemade memory system. It has been recalled heavily but not updated in a while. Newer episode evidence has accumulated since it was written.

CURRENT MEMORY:
{memory_text}

NEWER EVIDENCE (epilogue excerpts, most recent first):
{evidence}

Rewrite ONLY the body (everything after the frontmatter) so it integrates what the newer evidence shows — corrections, progressions, things that changed. Keep the user's voice register (casual-direct, peer-to-peer). Keep what is still true. Do not pad; if little changed, change little.

Output ONE JSON object: {{"body": "..."}} — no markdown fences, no preamble."""


def _frontmatter_type(text):
    m = re.search(r"^type:\s*(\S+)", text, re.M)
    return m.group(1).strip() if m else ""


def select_targets(
    memory_dir=None, db_path=None, hot_min=HOT_MIN, stale_days=STALE_DAYS
):
    """Hot-and-stale memories, sorted by access count desc.

    Returns [{filename, count, age_days, type, auto}]."""
    memory_dir = Path(memory_dir or MEMORY_DIR)
    now = time.time()
    counts = {}
    with sqlite3.connect(str(db_path or DEFAULT_DB), timeout=5.0) as conn:
        for fn, c in conn.execute(
            "SELECT filename, COUNT(*) FROM access GROUP BY filename"
        ):
            counts[fn] = c

    targets = []
    for fn, c in counts.items():
        if c < hot_min:
            continue
        p = memory_dir / fn
        if not p.exists() or fn == "MEMORY.md":
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        age_days = (now - _last_substantive_ts(p, text)) / DAY
        if age_days < stale_days:
            continue
        mem_type = _frontmatter_type(text)
        targets.append(
            {
                "filename": fn,
                "count": c,
                "age_days": round(age_days, 1),
                "type": mem_type,
                "auto": mem_type in AUTO_TYPES,
            }
        )
    targets.sort(key=lambda t: -t["count"])
    return targets


def _last_substantive_ts(path, text):
    """When was this memory last SUBSTANTIVELY updated?

    mtime lies once machine passes (backlink, index touches) edit files —
    prefer content dates: reconsolidated_at > created/date/event_date.
    Fall back to mtime when no frontmatter date exists."""
    fm = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if fm:
        for key in ("reconsolidated_at", "created", "date", "event_date"):
            m = re.search(
                r"^{}:\s*(\d{{4}}-\d{{2}}-\d{{2}})".format(key), fm.group(1), re.M
            )
            if m:
                try:
                    return time.mktime(time.strptime(m.group(1), "%Y-%m-%d"))
                except ValueError:
                    continue
    return path.stat().st_mtime


def gather_evidence(
    filename, epilogue_dir=None, since_ts=0.0, max_excerpts=4, window=400
):
    """Epilogue excerpts mentioning this memory, newer than since_ts."""
    epilogue_dir = Path(epilogue_dir or DEFAULT_EPILOGUES)
    stem = Path(filename).stem
    out = []
    if not epilogue_dir.exists():
        return out
    for p in sorted(epilogue_dir.rglob("*.md"), reverse=True):
        # Filename date is the real signal (consolidation rewrites bump mtime
        # on ancient epilogues). Fall back to mtime only when unparseable.
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", p.name)
        if m:
            try:
                file_dt = time.mktime(time.strptime("-".join(m.groups()), "%Y-%m-%d"))
            except ValueError:
                continue
            if file_dt <= since_ts:
                continue
        elif p.stat().st_mtime <= since_ts:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for hit in (stem, filename):
            idx = text.find(hit)
            if idx >= 0:
                start = max(0, idx - window // 2)
                out.append(
                    {
                        "epilogue": p.name,
                        "excerpt": text[start : start + window].strip(),
                    }
                )
                break
        if len(out) >= max_excerpts:
            break
    return out


def _snapshot(path):
    try:
        from provenance import snapshot

        snapshot(Path(path), reason="reconsolidation")
    except Exception:
        pass


def _emit_reindex(path):
    try:
        from event_bus import emit_event

        emit_event("reindex", {"file_path": str(path)})
    except Exception:
        pass


def _draft(memory_text, evidence):
    """LLM redraft. Returns {'body': ...} or None (skip this cycle)."""
    try:
        from providers import get_provider
        from tier_router import route, log_use
    except Exception:
        return None
    ev_block = "\n\n---\n\n".join(
        "[{}]\n{}".format(e["epilogue"], e["excerpt"]) for e in evidence
    )
    prompt = REDRAFT_PROMPT.format(memory_text=memory_text[:6000], evidence=ev_block)
    try:
        provider_name, model, params = route("memory_classification")
        provider = get_provider(provider_name)
        if not provider.health_check():
            return None
        resp = provider.generate(prompt, model=model, max_tokens=2048, temperature=0.3)
    except Exception:
        return None
    txt = (resp.text or "").strip()
    txt = re.sub(r"^```(?:json)?\s*", "", txt)
    txt = re.sub(r"\s*```$", "", txt)
    try:
        parsed = json.loads(txt)
    except Exception:
        m = re.search(r"\{.*\}", txt, re.DOTALL)
        if not m:
            return None
        try:
            parsed = json.loads(m.group(0))
        except Exception:
            return None
    if not parsed.get("body"):
        return None
    try:
        log_use(
            "memory_classification",
            model,
            tokens_in=resp.tokens_in or 0,
            tokens_out=resp.tokens_out or 0,
        )
    except Exception:
        pass
    return parsed


def _apply_body(text, new_body, stamp):
    """Replace everything after the frontmatter; stamp reconsolidated_at."""
    fm = re.match(r"^---\s*\n.*?\n---", text, re.S)
    if not fm:
        return None
    head = fm.group(0)
    if "reconsolidated_at:" in head:
        head = re.sub(
            r"^reconsolidated_at:.*$", "reconsolidated_at: " + stamp, head, flags=re.M
        )
    else:
        head = head[:-3].rstrip("\n") + "\nreconsolidated_at: " + stamp + "\n---"
    return head + "\n\n" + new_body.strip() + "\n"


def run(
    memory_dir=None,
    db_path=None,
    epilogue_dir=None,
    hot_min=HOT_MIN,
    stale_days=STALE_DAYS,
    cap=CAP,
):
    """One reconsolidation cycle. Returns summary."""
    memory_dir = Path(memory_dir or MEMORY_DIR)
    summary = {
        "targets": 0,
        "rewritten": 0,
        "identity_flagged": 0,
        "skipped_no_evidence": 0,
        "skipped_draft_failed": 0,
        "identity": [],
    }
    targets = select_targets(memory_dir, db_path, hot_min, stale_days)
    summary["targets"] = len(targets)
    rewritten = 0
    for t in targets:
        if not t["auto"]:
            if t["type"] in IDENTITY_TYPES:
                summary["identity_flagged"] += 1
                summary["identity"].append(t["filename"])
            continue
        if rewritten >= cap:
            continue
        p = memory_dir / t["filename"]
        text_now = p.read_text(encoding="utf-8", errors="replace")
        since = _last_substantive_ts(p, text_now)
        evidence = gather_evidence(t["filename"], epilogue_dir, since_ts=since)
        if not evidence:
            summary["skipped_no_evidence"] += 1
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        drafted = _draft(text, evidence)
        if not drafted:
            summary["skipped_draft_failed"] += 1
            continue
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        new_text = _apply_body(text, drafted["body"], stamp)
        if not new_text:
            summary["skipped_draft_failed"] += 1
            continue
        _snapshot(p)
        p.write_text(new_text, encoding="utf-8")
        _emit_reindex(p)
        rewritten += 1
    summary["rewritten"] = rewritten
    return summary


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "dry-run"
    if cmd == "dry-run":
        targets = select_targets()
        print(f"{len(targets)} hot-and-stale target(s):")
        for t in targets:
            lane = "AUTO" if t["auto"] else "identity (human-gated)"
            _p = MEMORY_DIR / t["filename"]
            ev = gather_evidence(
                t["filename"],
                since_ts=_last_substantive_ts(
                    _p, _p.read_text(encoding="utf-8", errors="replace")
                ),
            )
            print(
                f"  {t['count']:4d} recalls, {t['age_days']:5.0f}d old  "
                f"[{lane}]  {t['filename']}  ({len(ev)} evidence excerpts)"
            )
    elif cmd == "run":
        s = run()
        print(json.dumps(s, indent=2))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
