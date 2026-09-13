"""
decay.py — retrieval-time attention decay.

Multiplies each candidate's RRF score by exp(-ln(2) * age_days / halflife).
Halflife is per-weight-class:
  - "high" memories     -> 60 day halflife (core, slow to fade)
  - default              -> 21 day halflife
  - "low" memories       -> 7 day halflife (noise, fade fast)

Combined with corroboration_count (from v13 dedup): score gets a log-scale
boost when a memory has been independently corroborated multiple times.

Implementation is a pure-function library — no side effects. Callers
(memory_engine.search_hybrid, recall.py) opt in by importing + calling
apply_decay() on their RRF scores.

To disable globally: set MEMORY_DECAY_DISABLED=1 in env.
"""
from __future__ import annotations

import math
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

# v14 Phase 1 — Attention decay is OPT-IN (default off).
# Reason: the user's memories are curated markdown files (persistent knowledge),
# not episodic conversation events. Time-based decay is the wrong primitive
# for this corpus — superseded_by from Phase 3 (bi-temporal) does the right
# work for "this fact replaced that fact". Decay infrastructure ships here
# so future episodic memory types can opt in; corroboration boost stays on
# because it strengthens validated memories without penalizing anything.
#
# To enable decay: set MEMORY_DECAY_ENABLED=1
# To disable corroboration boost: set MEMORY_CORROB_DISABLED=1
DECAY_ENABLED = bool(os.environ.get("MEMORY_DECAY_ENABLED"))
CORROB_DISABLED = bool(os.environ.get("MEMORY_CORROB_DISABLED"))

HALFLIFE_DAYS_BY_WEIGHT = {
    "high":   90,   # core memories — slow fade when decay enabled
    "medium": 60,
    "":       45,   # default — no weight specified
    "low":    14,
}

# Decay is a TIEBREAKER when enabled. Floor prevents stale memories falling out.
DECAY_FLOOR = 0.70

# Boost factor coefficient — final = score * decay_mult * (1 + B * log1p(corrob - 1))
CORROBORATION_BOOST_COEF = 0.05


def _db() -> sqlite3.Connection:
    """Reuse memory_engine's db helper without circular import side effects."""
    from memory_engine import db
    return db()


def last_access_ts(filename: str) -> Optional[int]:
    """Return UNIX ts of last logged access for filename, or None."""
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT MAX(ts) FROM access WHERE filename = ?",
                (filename,),
            ).fetchone()
            return row[0] if row and row[0] else None
    except Exception:
        return None


def corroboration_for(memory_name: str) -> int:
    """Return corroboration_count for a memory's name from observation_corroboration."""
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT MAX(corroboration_count) FROM observation_corroboration WHERE subject = ?",
                (memory_name.lower().strip(),),
            ).fetchone()
            return int(row[0]) if row and row[0] else 1
    except Exception:
        return 1


def halflife_for(weight: str) -> float:
    return float(HALFLIFE_DAYS_BY_WEIGHT.get((weight or "").lower(), HALFLIFE_DAYS_BY_WEIGHT[""]))


def decay_multiplier(age_days: float, halflife_days: float) -> float:
    if halflife_days <= 0:
        return 1.0
    return math.exp(-math.log(2) * max(age_days, 0.0) / halflife_days)


def apply_decay(rrf_score: float, filename: str, weight: str, name: str = "",
                mtime: float = 0.0, now_ts: Optional[int] = None) -> float:
    """Apply temporal decay + corroboration boost to an RRF score.

    Decay logic (v14 — corrected):
      - If a memory has NEVER been accessed (no row in access table): NO decay.
        Untouched != stale. Could be fresh, niche, or just unused. Punishing
        it would push useful-but-rare memories off the top-k.
      - If accessed at least once: decay from the LAST access timestamp.
        This rewards recency of attention.

    Corroboration boost is always applied.
    Returns a new score. Never raises.
    """
    try:
        now = now_ts if now_ts is not None else int(time.time())
        mult = 1.0
        if DECAY_ENABLED:
            last = last_access_ts(filename)
            if last is not None:
                age_days = max(0.0, (now - last) / 86400.0)
                hl = halflife_for(weight)
                raw_mult = decay_multiplier(age_days, hl)
                mult = max(DECAY_FLOOR, raw_mult)
        boost = 1.0
        if not CORROB_DISABLED:
            corrob = corroboration_for(name or filename)
            boost = 1.0 + CORROBORATION_BOOST_COEF * math.log1p(max(0, corrob - 1))
        return rrf_score * mult * boost
    except Exception:
        return rrf_score


def explain(filename: str, weight: str, name: str = "", mtime: float = 0.0) -> dict:
    """Return a breakdown for debugging — what numbers fed into the score."""
    now = int(time.time())
    last = last_access_ts(filename) or int(mtime) or now
    age_days = max(0.0, (now - last) / 86400.0)
    hl = halflife_for(weight)
    mult = decay_multiplier(age_days, hl)
    corrob = corroboration_for(name or filename)
    boost = 1.0 + CORROBORATION_BOOST_COEF * math.log1p(max(0, corrob - 1))
    return {
        "filename": filename,
        "weight": weight or "(none)",
        "halflife_days": hl,
        "age_days": round(age_days, 2),
        "decay_multiplier": round(mult, 4),
        "corroboration_count": corrob,
        "corroboration_boost": round(boost, 4),
        "final_multiplier": round(mult * boost, 4),
    }


if __name__ == "__main__":
    import argparse
    import json
    ap = argparse.ArgumentParser(description="decay module CLI")
    sub = ap.add_subparsers(dest="cmd")
    p_explain = sub.add_parser("explain", help="Show decay breakdown for a memory")
    p_explain.add_argument("filename")
    p_explain.add_argument("--weight", default="")
    p_explain.add_argument("--name", default="")
    sub.add_parser("test", help="Run smoke tests")
    args = ap.parse_args()

    if args.cmd == "explain":
        print(json.dumps(explain(args.filename, args.weight, args.name), indent=2))
    elif args.cmd == "test":
        # Decay sanity: newer memories should keep ~100% of score, ancient ones lose most
        m0 = decay_multiplier(0, 21)
        m21 = decay_multiplier(21, 21)
        m60 = decay_multiplier(60, 21)
        print(f"age 0d:  multiplier = {m0:.4f}  (expect ~1.0)")
        print(f"age 21d: multiplier = {m21:.4f}  (expect ~0.5)")
        print(f"age 60d: multiplier = {m60:.4f}  (expect very small)")
        assert abs(m0 - 1.0) < 0.01
        assert abs(m21 - 0.5) < 0.01
        assert m60 < 0.15
        # High-weight memories decay slower
        m21_hi = decay_multiplier(21, 60)
        print(f"age 21d, weight high: multiplier = {m21_hi:.4f}  (expect ~0.78)")
        assert m21_hi > 0.7
        print("Decay tests passed.")
    else:
        ap.print_help()
