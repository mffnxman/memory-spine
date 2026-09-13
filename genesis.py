"""Spine Genesis — a retrieval-independent, single-session novelty growth signal.

For each FINALIZED epilogue not yet seen, embed its "What mattered" + "What
surprised" prose, measure max cosine vs the indexed spine, and — only if it is
genuinely NOVEL and SIGNIFICANT and the LLM affirms a coherent insight — write
ONE promotion CANDIDATE (never a spine memory) for human review.

Differentiator vs auto_promote: cardinality, not corpus. auto_promote fires only
on RECURRENCE (n>=3 over >=7d); Genesis catches the single significant+novel
session it structurally cannot. Anti-fabrication red line: "no fabricated
memories" (brain/brain-full.md:222). Default OFF, fail-soft, candidates-only.
"""
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
import sys as _sys  # noqa: E402
_sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths as _paths  # noqa: E402

_META_DIR = _paths.META_DIR
EPILOGUE_DIR = _META_DIR / "epilogues"
CANDIDATES_DIR = _META_DIR / "promotion_candidates" / "genesis"
TELEMETRY_PATH = _META_DIR / "v3_2_telemetry.jsonl"
MARKER_SUFFIX = ".genesis"

GENESIS_NOVELTY_MAX_SIM = 0.50      # novel iff max-cosine to spine < this
GENESIS_MAX_DRAFTS_PER_PASS = 3     # cost cap per pass
GENESIS_MIN_SECTION_CHARS = 80      # significance floor
GENESIS_TIER_HIGH_SIM = 0.30        # more novel (lower sim) -> higher tier
GENESIS_TIER_MED_SIM = 0.40
_NOT_CAPTURED = "(not captured)"
_EXCLUDE_PREFIXES = ("digest_", "session_handoff_")
EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"

try:
    import feature_flags as _ff
except Exception:  # pragma: no cover
    _ff = None


def _enabled(name: str) -> bool:
    """Read a feature flag, fail-soft to OFF."""
    try:
        return bool(_ff.is_enabled(name)) if _ff else False
    except Exception:
        return False


def _log_telemetry(record: dict) -> None:
    """Append one JSON breadcrumb to v3_2_telemetry.jsonl. Never raises."""
    try:
        TELEMETRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        record.setdefault("ts", int(time.time()))
        record.setdefault("component", "genesis")
        with TELEMETRY_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception:
        pass


def run() -> dict:
    """Sleep-time genesis pass. Flag-gated, fail-soft, candidates-only.

    Marking discipline: the .genesis marker records a *completed assessment* —
    written on every STABLE verdict (trivial / covered / drafted / declined) so
    it's never rescanned, and BEFORE the generate() call so a crash mid-draft
    can't re-spend it. NOT written on a TRANSIENT infra failure (embeddings
    unreadable / cap-break), so those epilogues retry next pass (R3 x R6).
    """
    if not _enabled("genesis_enabled"):
        return {"skipped": "flag_off"}
    if not _index_clean():
        return {"skipped": "index_unhealthy"}
    summary = {"assessed": 0, "drafted": 0, "declined": 0,
               "skipped_covered": 0, "skipped_trivial": 0,
               "skipped_unassessable": 0, "skipped_unreadable": 0}
    drafts = 0
    for epi in _finalized_epilogues():
        if drafts >= GENESIS_MAX_DRAFTS_PER_PASS:
            break  # cap: leave remaining epilogues UNMARKED for next pass
        try:
            raw = epi.read_text(encoding="utf-8")
        except Exception:
            # A finalized epilogue is immutable, so a read failure is a STABLE
            # verdict (not transient like a DB lock) -> mark so we don't rescan
            # it every pass forever.
            _mark(epi)
            summary["skipped_unreadable"] += 1
            _log_telemetry({"event": "genesis_unreadable", "cited_epilogue": epi.name})
            continue
        text = _extract_significant_text(raw)
        if not _is_significant(text):
            _mark(epi); summary["skipped_trivial"] += 1; continue
        assessable, sim, closest = _novelty(text)
        if not assessable:
            summary["skipped_unassessable"] += 1; continue  # transient -> no mark
        summary["assessed"] += 1
        novel = sim < GENESIS_NOVELTY_MAX_SIM
        _log_telemetry({"event": "genesis_assessed", "cited_epilogue": epi.name,
                        "novelty_sim": round(sim, 3),
                        "would_have_auto_promoted": novel, "drafted": False})
        if not novel:
            _mark(epi); summary["skipped_covered"] += 1; continue
        _mark(epi); drafts += 1  # mark BEFORE the expensive generate (R6)
        drafted = _draft_single_session(text, epi.name)
        if not drafted:
            summary["declined"] += 1
            _log_telemetry({"event": "genesis_declined", "cited_epilogue": epi.name,
                            "novelty_sim": round(sim, 3)})
            continue
        try:
            _write_candidate(drafted, epi, sim, closest)
        except Exception as e:
            # Disk/permission failure writing the candidate — log and move on so
            # one bad write can't abort the rest of the pass (marker already set,
            # so the LLM spend isn't repeated; R6).
            _log_telemetry({"event": "genesis_write_failed", "cited_epilogue": epi.name,
                            "error": str(e)[:120]})
            continue
        summary["drafted"] += 1
        _log_telemetry({"event": "genesis_drafted", "cited_epilogue": epi.name,
                        "novelty_sim": round(sim, 3),
                        "would_have_auto_promoted": True, "drafted": True})
    return summary


def _finalized_epilogues() -> list:
    """Finalized epilogues (name NOT starting 'draft-') without a .genesis marker."""
    try:
        files = sorted(p for p in EPILOGUE_DIR.glob("*.md")
                       if not p.name.startswith("draft-"))
    except Exception:
        return []
    return [p for p in files
            if not p.with_name(p.name + MARKER_SUFFIX).exists()]


def _extract_significant_text(epi_text: str) -> str:
    """Combined 'What mattered' + 'What surprised' prose. '' if neither usable.
    Postscripts (observer/habits) are excluded — they aren't in EPI_SECTIONS."""
    try:
        from consolidate import _split_sections
    except Exception:
        return ""
    secs = _split_sections(epi_text or "")
    parts = [secs.get("What mattered", "").strip(),
             secs.get("What surprised", "").strip()]
    return "\n\n".join(p for p in parts if p).strip()


def _is_significant(text: str) -> bool:
    """Significance floor: reject empty / '(not captured)' / thin sections."""
    cleaned = (text or "").replace(_NOT_CAPTURED, "").strip()
    return len(cleaned) >= GENESIS_MIN_SECTION_CHARS


def _index_clean() -> bool:
    """True iff every spine memory is embedded (0 missing, 0 stale). Any
    exception -> treat as dirty -> caller skips this round (fail-soft)."""
    try:
        import memory_engine as me
        h = me.index_health()
        return h.get("missing", 1) == 0 and h.get("stale", 1) == 0
    except Exception:
        return False


def _spine_vectors() -> list:
    """(filename, vec) for model-matching, non-excluded spine rows. May raise."""
    import memory_engine as me
    out = []
    with me.db() as conn:
        for fn, model, blob in conn.execute(
                "SELECT filename, model, vector FROM embeddings"):
            if fn.startswith(_EXCLUDE_PREFIXES):
                continue
            if model != EMBEDDING_MODEL:      # guard auto_promote lacks
                continue
            out.append((fn, me._blob_to_vec(blob)))
    return out


def _novelty(text: str):
    """FAIL-CLOSED novelty. Returns (assessable, max_sim, closest_filename).

    assessable=False (embed missing / no rows / DB error) -> caller SKIPS and
    does NOT mark, so a transient failure retries next pass. Never reports
    'novel' when it cannot actually compare.
    """
    import memory_engine as me
    vec = me.embed_text((text or "")[:me.EMBED_INPUT_CHARS])
    if not vec:
        return (False, 1.0, "")
    try:
        rows = _spine_vectors()
    except Exception:
        return (False, 1.0, "")
    if not rows:
        return (False, 1.0, "")
    best_sim, best_fn = -1.0, ""
    for fn, v in rows:
        s = me._cosine(vec, v)
        if s > best_sim:
            best_sim, best_fn = s, fn
    return (True, best_sim, best_fn)


def _mark(epi_path: Path) -> None:
    """Stamp the per-epilogue 'assessed' marker. Never raises. Distinct suffix
    from procedural_lib's '.reviewed' so neither disables the other (R2)."""
    try:
        marker = epi_path.with_name(epi_path.name + MARKER_SUFFIX)
        marker.write_text(str(int(time.time())), encoding="utf-8")
    except Exception:
        pass


GENESIS_PROMPT = """You are reviewing ONE finalized session epilogue from the user and Claude's collaboration. This is a SINGLE session — not a pattern across many. Below are its "What mattered" and "What surprised" reflections.

Epilogue: {epi_name}

{text}

Decide whether this ONE session contains a genuinely NEW, self-contained insight worth preserving as a durable memory the existing memory does not already name. This is n=1: ground any claim ONLY in THIS session; do not assert recurrence or generality. The red line is "no fabricated memories" (brain/brain-full.md:222) — invent nothing.

If yes, output ONE JSON object:
  "name": short evocative title (5-9 words)
  "description": one-line summary for retrieval indexing
  "type": one of: user | feedback | project | self
  "body": 1-3 paragraphs in the user's voice register (casual-direct, peer-to-peer, lowercase opener, no corporate tone), grounded ONLY in this session.

If this session is routine, or the insight is already obvious, or there is no self-contained durable insight worth keeping — DECLINE with:
  {{"name": null, "reason": "no novel insight"}}

Output ONLY the JSON, no markdown fences, no preamble."""


def _draft_single_session(text: str, epi_name: str):
    """LLM-draft a candidate from ONE session. Returns dict or None (decline/fail).
    Reuses auto_promote's JSON-salvage + decline-gate + provider orchestration."""
    try:
        from providers import get_provider
        from tier_router import route, log_use
    except Exception:
        return None
    prompt = GENESIS_PROMPT.format(epi_name=epi_name, text=text)
    try:
        provider_name, model, _params = route("memory_classification")
        provider = get_provider(provider_name)
        if not provider.health_check():
            _log_telemetry({"event": "genesis_draft_skipped_provider_unhealthy",
                            "provider": provider_name})
            return None
        resp = provider.generate(prompt, model=model, max_tokens=3072, temperature=0.4)
    except Exception:
        return None
    txt = (resp.text or "").strip()
    txt = re.sub(r"^```(?:json)?\s*", "", txt)
    txt = re.sub(r"\s*```$", "", txt)
    try:
        parsed = json.loads(txt)
    except Exception:
        match = re.search(r"\{.*\}", txt, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except Exception:
            return None
    if not parsed.get("name") or not parsed.get("body"):
        return None  # LLM declined (no novel insight) or malformed
    try:
        log_use("memory_classification", model,
                tokens_in=resp.tokens_in or 0, tokens_out=resp.tokens_out or 0)
    except Exception:
        pass
    return {**parsed, "model": f"{provider_name}:{model}"}


def _tier(novelty_sim: float) -> str:
    """More novel (lower sim) -> higher confidence it's genuinely new."""
    if novelty_sim < GENESIS_TIER_HIGH_SIM:
        return "HIGH"
    if novelty_sim < GENESIS_TIER_MED_SIM:
        return "MEDIUM"
    return "LOW"


def _write_candidate(drafted: dict, epi_path: Path, novelty_sim: float,
                     closest: str) -> Path:
    """Write ONE promotion candidate (FLAT schema) under CANDIDATES_DIR.
    Deterministic filename keyed on the epilogue stem (re-run overwrites, never
    duplicates). NEVER under MEMORY_DIR — Genesis is candidates-only."""
    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    mem_type = drafted.get("type", "self")
    if mem_type not in ("user", "feedback", "project", "self"):
        mem_type = "self"
    tier = _tier(novelty_sim)
    # Filename keyed ONLY on the immutable epilogue stem (R6): a re-draft on a
    # later day or after a tier flip overwrites rather than duplicates. Date /
    # tier / type live in the frontmatter, not the name.
    out_path = CANDIDATES_DIR / f"genesis_{epi_path.stem}.md"
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fm = [
        "---",
        f"name: {drafted['name']}",
        f"description: {drafted.get('description', '')}",
        f"type: {mem_type}",
        "source: genesis",
        f"confidence_tier: {tier}",
        f"novelty_sim: {novelty_sim:.3f}",
        f"closest_existing: {closest}",
        f"cited_epilogue: {epi_path.name}",
        f"synthesized_at: {now_iso}",
        f"synthesized_by: {drafted.get('model', '')}",
        "status: draft",
        "awaiting_approval: true",
        "---",
    ]
    out_path.write_text("\n".join(fm) + "\n\n" + drafted["body"].strip() + "\n",
                        encoding="utf-8")
    return out_path
