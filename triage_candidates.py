"""triage_candidates.py — dart2 pre-reads the drain queue (subconscious lane).

The drain-approval loop's bottleneck is human attention: candidates sit for
weeks because reviewing them cold is work. This gives every pending candidate
a dart2 triage annotation — recommendation + one-line reason, written into
its frontmatter — so /consolidate starts from an opinionated queue instead of
raw drafts. dart2-only by design (zero cost, zero quota, sleep-time speed is
fine); if dart2 is down, triage just doesn't happen this round. The human
always remains the approver — triage is advice, never action.

Frontmatter written per candidate:
  triage: approve | merge | reject
  triage_reason: <one line>
  triage_by: dart2-<tier> @ <date>

Usage:
  python triage_candidates.py run       # triage all untriaged pending
  python triage_candidates.py run --force  # re-triage everything
  python triage_candidates.py status    # queue + triage coverage
"""

from __future__ import annotations

import json
import re
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

META_DIR = _paths.META_DIR
TELEMETRY_PATH = META_DIR / "v3_2_telemetry.jsonl"

TRIAGE_PROMPT = """You are dart, triaging a candidate memory for the user and Claude's long-term memory system. The human reviewer will make the final call — your job is a fast, honest read.

The COMPLETE candidate text is pasted below between the markers. Do NOT use tools, do NOT look for files on disk — everything you need is right here. Judge only the pasted text.

CANDIDATE ({filename}):
<<<CANDIDATE START>>>
{candidate}
<<<CANDIDATE END>>>

CLOSEST EXISTING MEMORY (cosine {sim:.2f}): {closest}
CORROBORATIONS: {corroborations} (times the sleep cycle independently re-derived this while it waited for review)

Judge it:
- approve: novel, durable, correct at the right altitude — worth permanent memory
- merge: real signal but belongs inside an existing memory (name which one)
- reject: noise, too narrow, session-specific, or already covered

Output ONLY a JSON object: {{"recommendation": "approve"|"merge"|"reject", "reason": "<one blunt sentence>", "merge_target": "<filename or null>"}}"""


def _log_telemetry(record: dict) -> None:
    try:
        record.setdefault("ts", int(time.time()))
        record.setdefault("component", "triage_candidates")
        with TELEMETRY_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception:
        pass


def _frontmatter_value(text: str, key: str) -> str | None:
    m = re.search(rf"^{key}:\s*(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else None


def _write_triage(path: Path, rec: str, reason: str, by: str) -> bool:
    """Insert/replace triage keys in the frontmatter block. Non-destructive:
    body untouched, existing triage lines replaced on --force."""
    try:
        text = path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            return False
        # Drop any prior triage lines, then insert fresh ones before the
        # closing --- so re-triage stays idempotent.
        text = re.sub(r"^triage(_reason|_by)?:.*\n", "", text, flags=re.MULTILINE)
        end = text.find("\n---", 3)
        if end == -1:
            return False
        reason = reason.replace("\n", " ").strip()
        block = f"\ntriage: {rec}\ntriage_reason: {reason}\ntriage_by: {by}"
        path.write_text(text[:end] + block + text[end:], encoding="utf-8")
        return True
    except Exception:
        return False


def triage_all(force: bool = False) -> dict:
    """Triage every pending candidate lacking an annotation. Fail-soft: any
    per-file error skips that file; dart2 down skips the whole round."""
    from candidate_dedup import max_similarity_to_existing, pending_candidate_paths

    summary = {"pending": 0, "triaged": 0, "skipped_done": 0, "errors": 0}

    paths = pending_candidate_paths()
    summary["pending"] = len(paths)
    if not paths:
        return summary

    try:
        from providers import get_provider

        d2 = get_provider("dart2")
        if not d2.health_check():
            summary["skipped"] = "dart2 unhealthy — triage waits for next round"
            _log_telemetry({"event": "triage_skipped_dart2_down"})
            return summary
    except Exception as e:
        summary["skipped"] = f"provider: {e}"
        return summary

    from memory_engine import embed_batch
    from tier_router import log_use

    today = datetime.now().strftime("%Y-%m-%d")

    for path in paths:
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            summary["errors"] += 1
            continue
        if not force and _frontmatter_value(text, "triage"):
            summary["skipped_done"] += 1
            continue

        closest, sim = "(none)", 0.0
        try:
            vecs = embed_batch([text[:2500]])
            if vecs:
                s, fn = max_similarity_to_existing(vecs[0])
                if fn:
                    sim, closest = s, fn
        except Exception:
            pass

        corroborations = _frontmatter_value(text, "corroborations") or "0"

        prompt = TRIAGE_PROMPT.format(
            filename=path.name,
            candidate=text[:3000],
            sim=sim,
            closest=closest,
            corroborations=corroborations,
        )

        # Two attempts: dart occasionally goes off-script (its /chat runs an
        # agent loop with tools — one observed failure had it hunting the
        # filesystem for the "missing" file instead of judging the pasted
        # text). A retry with the same prompt usually lands.
        parsed, raw, resp = None, "", None
        for attempt in (1, 2):
            try:
                resp = d2.generate(prompt, model="dart2-deep")
            except Exception as e:
                _log_telemetry(
                    {
                        "event": "triage_generate_failed",
                        "file": path.name,
                        "attempt": attempt,
                        "err": str(e)[:120],
                    }
                )
                continue
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", (resp.text or "").strip())
            try:
                parsed = json.loads(raw)
            except Exception:
                m = re.search(r"\{.*\}", raw, re.DOTALL)
                if m:
                    try:
                        parsed = json.loads(m.group(0))
                    except Exception:
                        parsed = None
            if parsed and parsed.get("recommendation") in (
                "approve",
                "merge",
                "reject",
            ):
                break
            parsed = None

        rec = (parsed or {}).get("recommendation", "")
        if rec not in ("approve", "merge", "reject"):
            summary["errors"] += 1
            _log_telemetry(
                {"event": "triage_unparseable", "file": path.name, "preview": raw[:120]}
            )
            continue

        reason = (parsed.get("reason") or "").strip()
        target = parsed.get("merge_target")
        if rec == "merge" and target:
            reason = f"{reason} -> {target}"

        if _write_triage(path, rec, reason, f"{resp.model} @ {today}"):
            summary["triaged"] += 1
            _log_telemetry(
                {"event": "triaged", "file": path.name, "recommendation": rec}
            )
            try:
                log_use(
                    "candidate_triage",
                    resp.model,
                    tokens_in=resp.tokens_in,
                    tokens_out=resp.tokens_out,
                    provider="dart2",
                )
            except Exception:
                pass
        else:
            summary["errors"] += 1

    return summary


def status() -> dict:
    from candidate_dedup import pending_candidate_paths

    paths = pending_candidate_paths()
    triaged = []
    for p in paths:
        try:
            t = _frontmatter_value(p.read_text(encoding="utf-8"), "triage")
        except Exception:
            t = None
        triaged.append((p.name, t))
    return {
        "pending": len(paths),
        "triaged": sum(1 for _, t in triaged if t),
        "files": [{"file": n, "triage": t} for n, t in triaged],
    }


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("run", "status"):
        print("Usage: python triage_candidates.py run [--force] | status")
        return
    if sys.argv[1] == "status":
        print(json.dumps(status(), indent=2))
        return
    print(json.dumps(triage_all(force="--force" in sys.argv), indent=2))


if __name__ == "__main__":
    main()
