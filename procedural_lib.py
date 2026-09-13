"""
procedural_lib.py — procedural (L2) memory layer.

The first "how to act" tier on top of L0 (observer tool-logs), L1 (epilogues),
and L3 (semantic spine). Learns trigger->action heuristics from our own
trajectories via a blended success/failure signal:
  - tool error -> recovery within a session   (auto, failure-derived)
  - The user's corrections / praise             (feedback_*.md, both)
  - epilogue "what mattered" lessons          (success-derived)

Grounded in ExpeL (arXiv 2308.10144, AAAI 2024) + MemSkill (arXiv 2602.02474):
  - extraction = two LLM passes (failed-vs-successful; cross-task successes)
  - lifecycle  = ADD / EDIT / UPVOTE / DOWNVOTE + integer importance counter
  - retrieval  = embedding kNN over triggers (LLM-rerank deferred, A/B later)
  - designer   = offline sleep-cycle job (slots into consolidate_worker)

See plan_procedural_memory_v1.md for the full design.

Storage lives in memory.db (via memory_engine.db()) so it reuses the embedding,
decay, corroboration, and bitemporal machinery already in place.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths as _paths  # noqa: E402
import memory_engine as me

try:
    import feature_flags as _ff
except Exception:  # pragma: no cover - flags optional
    _ff = None


def _enabled(name: str) -> bool:
    """Read a procedural feature flag, fail-soft to OFF."""
    try:
        return bool(_ff.is_enabled(name)) if _ff else False
    except Exception:
        return False

# ─── Schema ──────────────────────────────────────────────────────────────────
# Mirrors kg.db's self-heal: CREATE ... IF NOT EXISTS on connect so a fresh
# install (or an older db) gains the table without a separate migration step.
_SCHEMA = """
CREATE TABLE IF NOT EXISTS heuristics (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger           TEXT NOT NULL,            -- "When [situation]" — the retrieval key (embedded)
    action            TEXT NOT NULL,            -- "do [action]" — the guidance
    insight           TEXT,                     -- fuller free-form ExpeL-style rationale
    origin            TEXT NOT NULL,            -- tool_recovery | feedback | epilogue | success_pattern
    polarity          TEXT NOT NULL,            -- failure_derived | success_derived
    importance        INTEGER NOT NULL DEFAULT 2,   -- ExpeL counter; archived at 0
    corroboration     INTEGER DEFAULT 1,
    use_count         INTEGER DEFAULT 0,
    source_session    TEXT,
    source_obs_ids    TEXT,                     -- JSON array -> provenance into observations.db
    source_refs       TEXT,                     -- JSON array -> epilogue/feedback filenames
    created_ts        INTEGER NOT NULL,
    last_used_ts      INTEGER,
    last_validated_ts INTEGER,
    valid_to          INTEGER,                  -- bitemporal: superseded heuristics
    superseded_by     INTEGER,                  -- FK -> heuristics.id
    status            TEXT DEFAULT 'active',    -- active | superseded | archived
    embedding         BLOB                      -- struct-packed trigger embedding (bge-small)
);
CREATE INDEX IF NOT EXISTS idx_heur_status ON heuristics(status);
CREATE INDEX IF NOT EXISTS idx_heur_importance ON heuristics(importance);
"""


def ensure_schema() -> None:
    """Create the heuristics table + indexes if absent. Idempotent; safe to
    call on every connect (fresh-install / older-db self-heal). Also self-heals
    the embedding column onto a table created by an earlier version."""
    with me.db() as c:
        c.executescript(_SCHEMA)
        cols = {r[1] for r in c.execute("PRAGMA table_info(heuristics)")}
        if "embedding" not in cols:
            c.execute("ALTER TABLE heuristics ADD COLUMN embedding BLOB")
        c.commit()


# ─── Designer: failure-pass (tool error -> recovery) ─────────────────────────
import json
import re
import time
import math

# Error markers, deliberately precise. The exception-class match is LINE-ANCHORED
# (^): a real traceback's final line is "ModuleNotFoundError: ...", whereas
# grepped/cat'd source mentions errors MID-line ("42: raise ValueError"). This
# kills the dominant false-positive — reading a file whose content discusses
# errors. Case-sensitive on the class name (no lowercase "errors" FPs).
_ERR_CODE_LINE = re.compile(
    r"^\s*(?:Traceback \(most recent call last\)|[A-Z][A-Za-z]*(?:Error|Exception)[:\s])",
    re.MULTILINE,
)
# Distinctive shell-failure phrases — safe to match anywhere.
_ERR_SHELL = re.compile(
    r"command not found|is not recognized as (?:an|the name)|"
    r"^fatal:|\nfatal:|Segmentation fault|Traceback \(most recent call last\)",
    re.IGNORECASE | re.MULTILINE,
)


def _has_error_marker(text: str) -> bool:
    return bool(_ERR_CODE_LINE.search(text) or _ERR_SHELL.search(text) or "tool_use_error" in text)

# Only executor tools count for the failure-pass: a non-zero exit / stderr is a
# real failure there. Search/read tools (Grep, Read, ...) routinely surface
# "Error"-like strings as *content* (grepping source) — those are not failures.
_EXEC_TOOLS = {
    "Bash", "PowerShell",
    "mcp__desktop-commander__start_process",
    "mcp__desktop-commander__interact_with_process",
}

SIM_PAIR = 0.3       # min token-Jaccard for two ops to be "the same operation"
DEDUP_SIM = 0.85     # min cosine for a new heuristic to dedup into an existing one
_PAIR_WINDOW = 20    # how many subsequent obs to scan for a recovery


def _looks_like_error(output_excerpt: str | None) -> bool:
    """True if a tool output excerpt shows a failure. Checks the stderr field
    (when the excerpt is JSON) plus a strong-marker scan of the whole text."""
    if not output_excerpt:
        return False
    text = output_excerpt
    try:
        obj = json.loads(output_excerpt)
        if isinstance(obj, dict):
            stderr = (obj.get("stderr") or "").strip()
            if stderr and _has_error_marker(stderr):
                return True
            text = f"{obj.get('stdout', '')}\n{stderr}"
    except (ValueError, TypeError):
        pass
    return _has_error_marker(text)


def _tokens(s: str | None) -> set[str]:
    return set(re.findall(r"[a-z0-9_]+", (s or "").lower()))


def _similar(a: str | None, b: str | None) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _op_text(row) -> str:
    return row["cmd_excerpt"] or row["tool_input_excerpt"] or ""


def detect_error_recoveries(conn, session_id: str | None = None) -> list[dict]:
    """Find (errored obs -> later similar succeeding obs) pairs within a session.
    The diff between the two is the lesson the failure-pass extracts."""
    conn.row_factory = sqlite3.Row
    q = "SELECT * FROM observations"
    args: tuple = ()
    if session_id:
        q += " WHERE session_id = ?"
        args = (session_id,)
    q += " ORDER BY session_id, ts, id"
    rows = [dict(r) for r in conn.execute(q, args)]

    pairs: list[dict] = []
    for i, err in enumerate(rows):
        if err["tool_name"] not in _EXEC_TOOLS:
            continue
        if not _looks_like_error(err.get("tool_output_excerpt")):
            continue
        for cand in rows[i + 1 : i + 1 + _PAIR_WINDOW]:
            if cand["session_id"] != err["session_id"]:
                break
            if cand["tool_name"] != err["tool_name"]:
                continue
            if _looks_like_error(cand.get("tool_output_excerpt")):
                continue
            if _similar(_op_text(err), _op_text(cand)) >= SIM_PAIR:
                pairs.append({"tool_name": err["tool_name"], "error": err, "recovery": cand})
                break
    return pairs


# ─── Designer: LLM extraction ────────────────────────────────────────────────
_EXTRACT_PROMPT = """You distil a reusable heuristic from a mistake an AI agent made and then fixed.

Tool: {tool}
FAILED attempt:
  input: {err_in}
  output: {err_out}
RECOVERED attempt (this one worked):
  input: {ok_in}
  output: {ok_out}

Write ONE heuristic capturing the lesson, as strict JSON only:
{{"trigger": "When <situation this applies to>", "action": "do <the corrective action>", "insight": "<one sentence why>"}}
The trigger must be a general situation, not this exact command. Output JSON only."""


def _parse_heuristic_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    trig, act = (obj.get("trigger") or "").strip(), (obj.get("action") or "").strip()
    if not trig or not act:
        return None
    return {"trigger": trig, "action": act, "insight": (obj.get("insight") or "").strip()}


def extract_heuristic(pair: dict, llm_fn) -> dict | None:
    """Contrast a failed vs recovered attempt into a heuristic via llm_fn.
    llm_fn(prompt: str) -> str. Returns None if the response is unparseable."""
    err, ok = pair.get("error", {}), pair.get("recovery", {})
    prompt = _EXTRACT_PROMPT.format(
        tool=pair.get("tool_name", "?"),
        err_in=_op_text_d(err), err_out=(err.get("tool_output_excerpt") or "")[:500],
        ok_in=_op_text_d(ok), ok_out=(ok.get("tool_output_excerpt") or "")[:300],
    )
    return _parse_heuristic_json(llm_fn(prompt))


def _op_text_d(d: dict) -> str:
    return d.get("cmd_excerpt") or d.get("tool_input_excerpt") or ""


# ─── Pool: upsert with ExpeL importance-counter dedup ────────────────────────
def _embed(text: str):
    try:
        return me.embed_text(text)
    except Exception:
        return None


def _cosine(a, b) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def upsert_heuristic(h: dict, *, origin: str, polarity: str,
                     source_session: str | None = None,
                     source_obs_ids: list | None = None,
                     source_refs: list | None = None) -> dict:
    """ADD a new heuristic, or UPVOTE the nearest existing one if it is a
    near-duplicate (cosine >= DEDUP_SIM). ExpeL lifecycle: new starts at
    importance 2; an upvote increments importance + corroboration."""
    ensure_schema()
    now = int(time.time())
    new_emb = _embed(h["trigger"])
    with me.db() as c:
        c.row_factory = sqlite3.Row
        existing = c.execute(
            "SELECT id, trigger, embedding FROM heuristics WHERE status='active'"
        ).fetchall()
        best_id, best_sim = None, 0.0
        for row in existing:
            if new_emb is not None:
                row_emb = me._blob_to_vec(row["embedding"]) if row["embedding"] else _embed(row["trigger"])
                sim = _cosine(new_emb, row_emb)
            else:
                sim = 1.0 if row["trigger"].strip().lower() == h["trigger"].strip().lower() else 0.0
            if sim > best_sim:
                best_id, best_sim = row["id"], sim

        if best_id is not None and best_sim >= DEDUP_SIM:
            c.execute(
                "UPDATE heuristics SET importance = importance + 1, "
                "corroboration = corroboration + 1, last_validated_ts = ? WHERE id = ?",
                (now, best_id),
            )
            c.commit()
            return {"op": "upvoted", "id": best_id, "sim": best_sim}

        blob = me._vec_to_blob(new_emb) if new_emb is not None else None
        cur = c.execute(
            "INSERT INTO heuristics (trigger, action, insight, origin, polarity, "
            "importance, corroboration, source_session, source_obs_ids, source_refs, "
            "created_ts, last_validated_ts, status, embedding) "
            "VALUES (?,?,?,?,?,2,1,?,?,?,?,?,'active',?)",
            (h["trigger"], h["action"], h.get("insight", ""), origin, polarity,
             source_session, json.dumps(source_obs_ids or []),
             json.dumps(source_refs or []), now, now, blob),
        )
        c.commit()
        return {"op": "added", "id": cur.lastrowid}


# ─── Controller: embedding-kNN retrieval ─────────────────────────────────────
_RECENCY_HALFLIFE_DAYS = 60.0


def _recency_mult(last_ts: int | None, now: int) -> float:
    if not last_ts:
        return 1.0
    age_days = max(0.0, (now - last_ts) / 86400.0)
    return math.exp(-math.log(2) * age_days / _RECENCY_HALFLIFE_DAYS)


def retrieve(task_context: str, k: int = 5, min_importance: int = 1) -> list[dict]:
    """Top-k active heuristics for a task context, by embedding cosine x recency
    x corroboration. Returns [] on an empty pool or when embeddings are
    unavailable. v1 controller = plain kNN (no LLM rerank)."""
    q_emb = _embed(task_context)
    if q_emb is None:
        return []
    now = int(time.time())
    with me.db() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM heuristics WHERE status='active' AND importance >= ?",
            (min_importance,),
        ).fetchall()
    scored = []
    for r in rows:
        # H4: never inline-embed on the prompt hot path. A row with no embedding is
        # skipped (the reindex/designer path embeds it shortly); inline-embedding
        # every unembedded row would fire N uncapped model calls on the next prompt
        # right after a sleep cycle adds a batch — the freeze-class hazard.
        if not r["embedding"]:
            continue
        emb = me._blob_to_vec(r["embedding"])
        cos = _cosine(q_emb, emb)
        if cos <= 0:
            continue
        # Cosine relevance DOMINATES. Corroboration is only a gentle nudge (a
        # heavily-confirmed heuristic must never override a clearly-better match)
        # and recency a mild decay. (P6 found a multiplicative corrob term let
        # the 2 most-corroborated heuristics win every query — fixed here.)
        score = cos * _recency_mult(r["last_validated_ts"], now) * (1 + 0.05 * math.log1p(max(0, r["corroboration"] - 1)))
        d = dict(r)
        d.pop("embedding", None)
        d["cosine"] = cos
        d["score"] = score
        scored.append(d)
    scored.sort(key=lambda d: d["score"], reverse=True)
    return scored[:k]


# ─── Injection: recall lane + boot section (flag-gated, default OFF) ─────────
INJECTION_FLAG = "procedural_injection_enabled"
MATCH_MIN = 0.62         # min cosine to inject. Tuned on real-pool data (procedural_test):
                         # bge-small compresses cosine, so distractor prompts peaked at 0.605
                         # while the weakest genuine match was 0.626 — 0.62 separates them and
                         # biases to precision (a wrong injected habit is worse than a missing
                         # one; the always-on boot_section covers recall). A/B-revisable.
RECALL_K = 3
BOOT_K = 5
_RECENT_INJECT_SEC = 6 * 3600  # burn() treats a habit used within this window as "was injected"


_TRIG_MAX, _ACT_MAX = 120, 200


def _clean(text: str, limit: int) -> str:
    """Collapse all whitespace (incl. newlines) to single spaces and truncate.
    Habits ride in every prompt now — keep each to one bounded line, and never
    let a stored newline split one heuristic across lines (or carry an injected
    payload onto its own line)."""
    t = " ".join((text or "").split())
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def format_for_injection(heuristics: list[dict], header: str) -> str:
    if not heuristics:
        return ""
    lines = [f"[procedural memory] {header}:"]
    for h in heuristics:
        trig = _clean(h.get("trigger", ""), _TRIG_MAX)
        if trig.lower().startswith("when "):   # avoid "When When ..." (proper prefix strip)
            trig = trig[5:].lstrip()
        act = _clean(h.get("action", ""), _ACT_MAX)
        lines.append(f"  • When {trig} → {act}")
    return "\n".join(lines)


def recall_lane(prompt: str, k: int = RECALL_K) -> str:
    """Trigger-matched heuristics for the current prompt. '' if flag OFF, pool
    empty, or nothing clears MATCH_MIN."""
    if not _enabled(INJECTION_FLAG):
        return ""
    hits = [h for h in retrieve(prompt, k=k) if h.get("cosine", 0) >= MATCH_MIN]
    if hits:
        try:
            mark_used([h["id"] for h in hits])
        except Exception:
            pass
    return format_for_injection(hits, header="learned habits relevant to this task")


def boot_section(k: int = BOOT_K) -> str:
    """Top global heuristics by standing (importance x corroboration). '' if flag OFF."""
    if not _enabled(INJECTION_FLAG):
        return ""
    ensure_schema()
    with me.db() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT trigger, action FROM heuristics WHERE status='active' "
            "ORDER BY (importance * corroboration) DESC, last_validated_ts DESC LIMIT ?",
            (k,),
        ).fetchall()
    return format_for_injection([dict(r) for r in rows], header="learned habits")


# ─── Lifecycle: ExpeL importance counter + bitemporal supersession ───────────
def upvote(hid: int) -> None:
    now = int(time.time())
    with me.db() as c:
        c.execute(
            "UPDATE heuristics SET importance = importance + 1, "
            "corroboration = corroboration + 1, last_validated_ts = ? WHERE id = ?",
            (now, hid),
        )
        c.commit()


def downvote(hid: int) -> None:
    """Decrement importance; archive at <= 0 (ExpeL: removed when counter hits 0;
    we soft-archive to keep provenance)."""
    now = int(time.time())
    with me.db() as c:
        c.execute("UPDATE heuristics SET importance = importance - 1, last_validated_ts = ? WHERE id = ?",
                  (now, hid))
        c.execute("UPDATE heuristics SET status = 'archived' WHERE id = ? AND importance <= 0",
                  (hid,))
        c.commit()


def prune() -> int:
    """Archive all active heuristics whose importance has fallen to <= 0. Returns
    the count archived. (Soft-delete: rows are kept for provenance.)"""
    ensure_schema()
    with me.db() as c:
        cur = c.execute("UPDATE heuristics SET status = 'archived' "
                        "WHERE status = 'active' AND importance <= 0")
        c.commit()
        return cur.rowcount


def burn(hid: int) -> None:
    """Kill switch: immediately archive a heuristic (a bad one spotted in a live
    session), bypassing the importance counter. Soft-delete — provenance kept,
    so it's reversible by flipping status back. Pairs with the injection flag
    (which kills ALL injection); burn kills ONE habit without a full rollback.

    Emits a 'burned' telemetry trace noting whether it was recently injected, so
    the outcome loop can observe hard negatives."""
    now = int(time.time())
    with me.db() as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT trigger, last_used_ts FROM heuristics WHERE id=?", (hid,)).fetchone()
        c.execute("UPDATE heuristics SET status='archived', importance=0, "
                  "last_validated_ts=? WHERE id=?", (now, hid))
        c.commit()
    was_injected = bool(row and row["last_used_ts"] and (now - row["last_used_ts"]) < _RECENT_INJECT_SEC)
    _telemetry({"event": "burned", "id": hid,
                "trigger": (row["trigger"] if row else None), "was_injected": was_injected})


def supersede(old_id: int, new_id: int) -> None:
    """Retire an old heuristic in favour of a refined one — bitemporal, mirrors
    the KG's fact supersession (valid_to + superseded_by, status flip)."""
    now = int(time.time())
    with me.db() as c:
        c.execute("UPDATE heuristics SET valid_to = ?, superseded_by = ?, status = 'superseded' WHERE id = ?",
                  (now, new_id, old_id))
        c.commit()


def record_outcome(ids: list[int], success: bool) -> None:
    """Outcome feedback: upvote heuristics that were injected and the task went
    well, downvote ones that didn't. The only learned signal in v1."""
    for hid in ids:
        (upvote if success else downvote)(hid)


def review_epilogue(path) -> list[int]:
    """Parse a finalized epilogue for habit lines flagged with ❌ and downvote
    them (record_outcome success=False). Idempotent via a sibling '.reviewed'
    marker so re-running never double-downvotes. Returns the downvoted ids."""
    p = Path(path)
    marker = p.with_name(p.name + ".reviewed")
    if marker.exists():
        return []
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return []
    flagged = sorted({
        int(m.group(1))
        for line in text.splitlines() if "❌" in line
        for m in [re.search(r"\[h:(\d+)\]", line)] if m
    })
    if flagged:
        record_outcome(flagged, success=False)
        _telemetry({"event": "outcome_review", "downvoted": flagged, "epilogue": p.name})
    try:
        marker.write_text(str(int(time.time())), encoding="utf-8")
    except Exception:
        pass
    return flagged


def review_recent_epilogues(epilogue_dir=None, k: int = 3) -> dict:
    """Auto-apply ❌ habit flags from the most recent finalized epilogues.
    Finalized = a '*.md' file whose name does NOT start with 'draft-'. Idempotent
    via review_epilogue's per-file '.reviewed' marker, so re-scanning is free and
    never double-downvotes. Returns {'reviewed': [names], 'downvoted': [ids]} and
    emits one telemetry trace when anything was downvoted. Fail-soft per file."""
    d = Path(epilogue_dir) if epilogue_dir else (_META_DIR / "epilogues")
    reviewed: list[str] = []
    downvoted: list[int] = []
    try:
        finalized = sorted(p for p in d.glob("*.md") if not p.name.startswith("draft-"))
    except Exception:
        finalized = []
    for p in finalized[-k:]:
        try:
            ids = review_epilogue(p)
            reviewed.append(p.name)
            downvoted.extend(ids)
        except Exception:
            continue
    if downvoted:
        _telemetry({"event": "epilogue_review_boot", "reviewed": reviewed, "downvoted": downvoted})
    return {"reviewed": reviewed, "downvoted": downvoted}


def mark_used(ids: list[int]) -> None:
    """Record that heuristics were injected (use_count + last_used_ts), so the
    outcome loop knows which to reward/penalise at session end."""
    now = int(time.time())
    with me.db() as c:
        for hid in ids:
            c.execute("UPDATE heuristics SET use_count = use_count + 1, last_used_ts = ? WHERE id = ?",
                      (now, hid))
        c.commit()


def injected_since(ts: int) -> list[dict]:
    """Heuristics injected (mark_used) at or after `ts` — the 'this session' set.
    Active only; excludes never-injected and archived rows."""
    ensure_schema()
    with me.db() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT id, trigger, action, last_used_ts FROM heuristics "
            "WHERE status='active' AND last_used_ts IS NOT NULL AND last_used_ts >= ? "
            "ORDER BY last_used_ts DESC",
            (int(ts),),
        ).fetchall()
    return [dict(r) for r in rows]


# ─── Observability: telemetry + index stats ──────────────────────────────────
_META_DIR = _paths.META_DIR
_TELEMETRY_PATH = _META_DIR / "v3_2_telemetry.jsonl"


def _telemetry(record: dict) -> None:
    """Fail-soft append to the shared telemetry log (component='procedural')."""
    try:
        record.setdefault("component", "procedural")
        record.setdefault("ts", int(time.time()))
        with open(_TELEMETRY_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


def index_stats() -> dict:
    """Counts by status — surfaced in the boot health line."""
    ensure_schema()
    with me.db() as c:
        rows = dict(c.execute("SELECT status, count(*) FROM heuristics GROUP BY status").fetchall())
        unembedded = c.execute(
            "SELECT count(*) FROM heuristics WHERE status='active' AND embedding IS NULL"
        ).fetchone()[0]
    return {"active": rows.get("active", 0),
            "archived": rows.get("archived", 0),
            "superseded": rows.get("superseded", 0),
            "unembedded": unembedded}


# ─── Designer: production LLM + orchestration entrypoint ──────────────────────
EXTRACTION_FLAG = "procedural_extraction_enabled"

# Max LLM distill calls per designer run, shared across the failure/feedback/
# epilogue passes. The idempotent per-source skips resume the backlog on the
# next cycle, so a single sleep-cycle can never balloon into a ~30-min LLM run
# (the failure mode that, re-queued by the reaper, burned tokens continuously).
MAX_DISTILLS_PER_RUN = 8


def default_llm(prompt: str) -> str:
    """Production extractor LLM — routes to the multi_doc_synthesis tier (Opus),
    sleep-time. Fail-soft to '' (which yields no heuristic) if unavailable."""
    try:
        from providers import get_provider
        from tier_router import route
        provider_name, model, _params = route("multi_doc_synthesis")
        provider = get_provider(provider_name)
        if not provider.health_check():
            _telemetry({"event": "designer_provider_unhealthy", "provider": provider_name})
            return ""
        resp = provider.generate(prompt, model=model, max_tokens=1024, temperature=0.3)
        return resp.text or ""
    except Exception as e:
        _telemetry({"event": "designer_llm_error", "error": str(e)[:200]})
        return ""


def _obs_already_mined(obs_id: int) -> bool:
    with me.db() as c:
        for (raw,) in c.execute(
            "SELECT source_obs_ids FROM heuristics WHERE source_obs_ids IS NOT NULL"
        ):
            try:
                if obs_id in json.loads(raw):
                    return True
            except (ValueError, TypeError):
                pass
    return False


def run_designer(*, obs_conn=None, memory_dir=None, epilogue_dir=None, llm_fn=None) -> dict:
    """Sleep-cycle entrypoint: failure-pass (obs error->recovery) + feedback-pass
    + epilogue-pass. Gated by procedural_extraction_enabled. Idempotent per
    source (obs-id / file). llm_fn defaults to the production router."""
    if not _enabled(EXTRACTION_FLAG):
        return {"skipped": "flag_off"}
    ensure_schema()
    llm_fn = llm_fn or default_llm
    summary = {"failure": 0, "feedback": 0, "epilogue": 0}
    budget = MAX_DISTILLS_PER_RUN  # LLM distill calls remaining this cycle (shared)

    if obs_conn is not None:
        try:
            for pair in detect_error_recoveries(obs_conn):
                if budget <= 0:
                    break
                eid, rid = pair["error"]["id"], pair["recovery"]["id"]
                if _obs_already_mined(eid):
                    continue
                budget -= 1  # an LLM distill call is about to happen
                h = extract_heuristic(pair, llm_fn)
                if not h:
                    continue
                upsert_heuristic(h, origin="tool_recovery", polarity="failure_derived",
                                 source_session=pair["error"].get("session_id"),
                                 source_obs_ids=[eid, rid])
                summary["failure"] += 1
        except Exception as e:
            _telemetry({"event": "designer_failure_pass_error", "error": str(e)[:200]})

    if memory_dir and budget > 0:
        try:
            r = run_feedback_pass(memory_dir, llm_fn, limit=budget)
            summary["feedback"] = r["added"]
            budget -= r["distilled"]
        except Exception as e:
            _telemetry({"event": "designer_feedback_pass_error", "error": str(e)[:200]})
    if epilogue_dir and budget > 0:
        try:
            r = run_epilogue_pass(epilogue_dir, llm_fn, limit=budget)
            summary["epilogue"] = r["added"]
            budget -= r["distilled"]
        except Exception as e:
            _telemetry({"event": "designer_epilogue_pass_error", "error": str(e)[:200]})

    summary["capped"] = budget <= 0  # backlog remains; resumes next cycle
    _telemetry({"event": "designer_run", **summary})
    return summary


# ─── CLI ─────────────────────────────────────────────────────────────────────
def main():
    import argparse
    ap = argparse.ArgumentParser(description="Procedural (L2) memory layer")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("stats", help="show heuristic pool stats")
    sub.add_parser("list", help="list active heuristics")
    sub.add_parser("designer", help="run the designer (extraction) pass now")
    rp = sub.add_parser("retrieve", help="retrieve heuristics for a task context")
    rp.add_argument("query")
    bp = sub.add_parser("burn", help="immediately archive a bad heuristic by id")
    bp.add_argument("id", type=int)
    op = sub.add_parser("outcome", help="manually reinforce: upvote/downvote injected heuristics")
    op.add_argument("ids", help="comma-separated heuristic ids")
    op.add_argument("--success", dest="success", action="store_true")
    op.add_argument("--fail", dest="success", action="store_false")
    op.set_defaults(success=True)
    rvp = sub.add_parser("review", help="apply ❌ habit flags from a finalized epilogue")
    rvp.add_argument("path")
    args = ap.parse_args()

    if args.cmd == "stats":
        print(json.dumps(index_stats(), indent=2))
    elif args.cmd == "list":
        ensure_schema()
        with me.db() as c:
            c.row_factory = sqlite3.Row
            for r in c.execute("SELECT id, importance, corroboration, polarity, trigger, action "
                               "FROM heuristics WHERE status='active' "
                               "ORDER BY importance*corroboration DESC"):
                print(f"  [{r['id']}] imp={r['importance']} cor={r['corroboration']} "
                      f"({r['polarity']}) When {r['trigger']} -> {r['action']}")
    elif args.cmd == "designer":
        from observer_lib import get_connection
        with get_connection() as oc:
            print(json.dumps(run_designer(
                obs_conn=oc, memory_dir=_META_DIR.parent,
                epilogue_dir=_META_DIR / "epilogues"), indent=2))
    elif args.cmd == "retrieve":
        for h in retrieve(args.query):
            print(f"  [{h['score']:.3f}] When {h['trigger']} -> {h['action']}")
    elif args.cmd == "burn":
        burn(args.id)
        print(f"  burned heuristic {args.id} (archived)")
    elif args.cmd == "outcome":
        ids = [int(x) for x in args.ids.split(",") if x.strip()]
        record_outcome(ids, args.success)
        print(f"  {'upvoted' if args.success else 'downvoted'} {ids}")
    elif args.cmd == "review":
        downvoted = review_epilogue(args.path)
        print(f"  downvoted {downvoted}" if downvoted else "  no flagged habits")
    else:
        ap.print_help()


if __name__ == "__main__":
    main()


# ─── Designer: success-pass (distilled lessons from feedback + epilogues) ────
_TEXT_EXTRACT_PROMPT = """Distil ONE reusable how-to-act heuristic from this {kind} note an AI agent saved.

NOTE:
{text}

Output strict JSON only:
{{"trigger": "When <general situation>", "action": "do <the action>", "insight": "<one sentence why>"}}
If the note contains no actionable lesson, output exactly: null"""


def iter_feedback_files(memory_dir) -> list[Path]:
    return sorted(Path(memory_dir).glob("feedback_*.md"))


def extract_heuristic_from_text(text: str, llm_fn, kind: str = "feedback") -> dict | None:
    """Distil a heuristic from a free-text lesson (feedback/epilogue)."""
    return _parse_heuristic_json(llm_fn(_TEXT_EXTRACT_PROMPT.format(kind=kind, text=text[:4000])))


def _already_ingested(source_ref: str) -> bool:
    with me.db() as c:
        row = c.execute(
            "SELECT 1 FROM heuristics WHERE source_refs LIKE ? LIMIT 1",
            (f'%"{source_ref}"%',),
        ).fetchone()
    return row is not None


def run_feedback_pass(memory_dir, llm_fn, limit=None) -> dict:
    """Mint heuristics from feedback_*.md. Idempotent per source file. `limit`
    caps the number of LLM distill calls (new files processed) this pass."""
    ensure_schema()
    added = 0
    distilled = 0
    for f in iter_feedback_files(memory_dir):
        if limit is not None and distilled >= limit:
            break
        if _already_ingested(f.name):
            continue
        distilled += 1
        h = extract_heuristic_from_text(f.read_text(encoding="utf-8"), llm_fn, kind="feedback")
        if not h:
            continue
        upsert_heuristic(h, origin="feedback", polarity="failure_derived",
                         source_refs=[f.name])
        added += 1
    return {"added": added, "distilled": distilled}


def run_epilogue_pass(epilogue_dir, llm_fn, limit=None) -> dict:
    """Mint success-derived heuristics from epilogue 'what mattered' lessons.
    Idempotent per source file. `limit` caps LLM distill calls this pass."""
    ensure_schema()
    added = 0
    distilled = 0
    for f in sorted(Path(epilogue_dir).glob("*.md")):
        if limit is not None and distilled >= limit:
            break
        if _already_ingested(f.name):
            continue
        distilled += 1
        h = extract_heuristic_from_text(f.read_text(encoding="utf-8"), llm_fn, kind="epilogue")
        if not h:
            continue
        upsert_heuristic(h, origin="epilogue", polarity="success_derived",
                         source_refs=[f.name])
        added += 1
    return {"added": added, "distilled": distilled}
