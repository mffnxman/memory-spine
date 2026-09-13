"""benchmark_gen.py — generated retrieval-floor benchmark + hallucination audit
(v15, 2026-07-09).

The curated continuity suite (27 cases, continuity_cases.json) stays untouched
so its baseline diffs remain comparable. This module adds volume + a new
failure class on top:

FLOOR suite (generated, no LLM):
  - one case per spine memory that has a description: the memory MUST be
    findable from its own description (query = description, expected = file).
    Catches index rot, embedding drift, missing index rows — the retrieval
    floor under everything else.
  - temporal cases from digests: "what happened in week <W>" -> digest file.
  - own baseline at _meta/floor_baseline.json (save/diff like continuity).

HALLUCINATION audit (structural provenance — recall must not return phantoms):
  - orphan embeddings: index rows whose file no longer exists
  - phantom index entries: MEMORY.md auto-index lines pointing at missing files
  - orphan co_recall edges: synapses into the void
  - orphan access rows: recall history for vanished files (informational)

CLI:
    python benchmark_gen.py run                 # generate + evaluate floor suite
    python benchmark_gen.py audit               # hallucination audit
    python benchmark_gen.py baseline save|diff  # floor baseline ops
"""

from __future__ import annotations

import json
import re
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
DEFAULT_DB = MEMORY_DIR / "_meta" / "memory.db"
FLOOR_BASELINE = MEMORY_DIR / "_meta" / "floor_baseline.json"
MARKER_START = "<!-- AUTO-INDEX-START -->"
MARKER_END = "<!-- AUTO-INDEX-END -->"


def _frontmatter(text):
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    fm = {}
    for line in m.group(1).splitlines():
        km = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if km:
            fm[km.group(1)] = km.group(2).strip().strip('"')
    return fm


def generate(memory_dir=None) -> dict:
    """Build the floor + temporal case sets from the live corpus."""
    memory_dir = Path(memory_dir or MEMORY_DIR)
    floor, temporal = [], []
    for p in sorted(memory_dir.glob("*.md")):
        if p.name == "MEMORY.md":
            continue
        fm = _frontmatter(p.read_text(encoding="utf-8", errors="replace"))
        desc = fm.get("description", "").strip()
        wm = re.match(r"digest_(\d{4})-w(\d+)", p.stem)
        if wm:
            temporal.append(
                {
                    "id": f"temporal_{p.stem}",
                    "query": f"what happened in week {wm.group(1)}-W{int(wm.group(2)):02d}",
                    "expected": [p.name],
                    "weight": "temporal",
                }
            )
        if not desc:
            continue
        floor.append(
            {
                "id": f"floor_{p.stem}",
                "query": desc,
                "expected": [p.name],
                "weight": "floor",
            }
        )
    return {"floor": floor, "temporal": temporal}


def _default_search(query, top_k):
    from memory_engine import search_hybrid, list_memories

    results = search_hybrid(query, mems=_default_search._mems, top_k=top_k)
    out = []
    for r in results:
        item = r[0] if isinstance(r, tuple) else r
        fn = getattr(item, "filename", None) or (
            item.get("filename") if isinstance(item, dict) else str(item)
        )
        out.append(fn)
    return out


def run_floor(cases, search_fn=None, top_k=8) -> dict:
    """Evaluate cases; pass = any expected file in top_k. Returns report."""
    if search_fn is None:
        from memory_engine import list_memories

        _default_search._mems = list_memories()
        search_fn = _default_search
    passed, rr_sum, failures = 0, 0.0, []
    for case in cases:
        ranked = list(search_fn(case["query"], top_k))[:top_k]
        rank = None
        for i, fn in enumerate(ranked, start=1):
            if fn in case["expected"]:
                rank = i
                break
        if rank:
            passed += 1
            rr_sum += 1.0 / rank
        else:
            failures.append(
                {"id": case["id"], "expected": case["expected"][0], "got": ranked[:3]}
            )
    n = len(cases)
    return {
        "n": n,
        "passed": passed,
        "mrr": round(rr_sum / n, 4) if n else 0.0,
        "failures": failures,
    }


def hallucination_audit(memory_dir=None, db_path=None) -> dict:
    """Structural provenance: recall infrastructure must reference only real files."""
    memory_dir = Path(memory_dir or MEMORY_DIR)
    on_disk = {p.name for p in memory_dir.glob("*.md")}
    audit = {
        "orphan_embeddings": [],
        "phantom_index": [],
        "orphan_co_recall": 0,
        "orphan_access": 0,
    }

    with sqlite3.connect(str(db_path or DEFAULT_DB), timeout=5.0) as conn:
        try:
            audit["orphan_embeddings"] = [
                fn
                for (fn,) in conn.execute("SELECT filename FROM embeddings")
                if fn not in on_disk
            ]
        except sqlite3.OperationalError:
            pass
        try:
            audit["orphan_co_recall"] = sum(
                1
                for a, b in conn.execute("SELECT a, b FROM co_recall")
                if a not in on_disk or b not in on_disk
            )
        except sqlite3.OperationalError:
            pass
        try:
            audit["orphan_access"] = sum(
                1
                for (fn,) in conn.execute("SELECT DISTINCT filename FROM access")
                if fn not in on_disk
            )
        except sqlite3.OperationalError:
            pass

    index_path = memory_dir / "MEMORY.md"
    if index_path.exists():
        text = index_path.read_text(encoding="utf-8", errors="replace")
        s, e = text.find(MARKER_START), text.find(MARKER_END)
        if s >= 0 and e > s:
            block = text[s:e]
            for fn in set(re.findall(r"`([\w\-. ]+\.md)`", block)):
                if fn not in on_disk:
                    audit["phantom_index"].append(fn)

    audit["clean"] = not (
        audit["orphan_embeddings"]
        or audit["phantom_index"]
        or audit["orphan_co_recall"]
    )
    return audit


def _baseline(cmd):
    gen = generate()
    cases = gen["floor"] + gen["temporal"]
    report = run_floor(cases)
    current = {
        "ts": int(time.time()),
        "n": report["n"],
        "passed": report["passed"],
        "mrr": report["mrr"],
        "failed_ids": sorted(f["id"] for f in report["failures"]),
    }
    if cmd == "save":
        FLOOR_BASELINE.write_text(json.dumps(current, indent=2), encoding="utf-8")
        print(
            f"saved floor baseline: {current['passed']}/{current['n']} MRR {current['mrr']}"
        )
        return
    if not FLOOR_BASELINE.exists():
        print("no floor baseline yet — run: python benchmark_gen.py baseline save")
        return
    base = json.loads(FLOOR_BASELINE.read_text(encoding="utf-8"))
    print(f"  Baseline:  {base['passed']}/{base['n']}  MRR {base['mrr']}")
    print(f"  Current:   {current['passed']}/{current['n']}  MRR {current['mrr']}")
    regressed = sorted(set(current["failed_ids"]) - set(base.get("failed_ids", [])))
    fixed = sorted(set(base.get("failed_ids", [])) - set(current["failed_ids"]))
    if regressed:
        print(f"  REGRESSIONS ({len(regressed)}):")
        for rid in regressed:
            print(f"    - {rid}")
    if fixed:
        print(f"  NEW PASSES ({len(fixed)}):")
        for rid in fixed:
            print(f"    + {rid}")
    if not regressed and not fixed:
        print("  no case-level drift")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        gen = generate()
        cases = gen["floor"] + gen["temporal"]
        print(
            f"generated: {len(gen['floor'])} floor + {len(gen['temporal'])} temporal cases"
        )
        report = run_floor(cases)
        print(f"passed {report['passed']}/{report['n']}  MRR {report['mrr']}")
        for f in report["failures"][:15]:
            print(f"  MISS {f['id']}: wanted {f['expected']}, got {f['got']}")
        if len(report["failures"]) > 15:
            print(f"  ... and {len(report['failures']) - 15} more")
    elif cmd == "audit":
        a = hallucination_audit()
        print(json.dumps(a, indent=2))
        print("CLEAN" if a["clean"] else "DIRTY — fix the phantoms above")
    elif cmd == "baseline":
        _baseline(sys.argv[2] if len(sys.argv) > 2 else "diff")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
