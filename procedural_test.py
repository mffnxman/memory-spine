"""procedural_test.py — retrieval regression harness for the L2 heuristic pool.

Sibling of continuity_test.py. Reads curated (task_context -> expected
heuristic) cases from procedural_cases.json and runs each through
`procedural_lib.retrieve`. A case PASSES if a retrieved heuristic within the
top-k carries any expected action/trigger marker (substring, case-insensitive).
Reports per-case rank, MRR, and pass rate.

Heuristic ids/importance churn as the designer learns, so cases match on stable
TEXT markers (e.g. "PYTHONIOENCODING"), never on row id — the same way
continuity_test matches on memory filename rather than rowid.

Usage:
  python procedural_test.py                       # run all cases (k=5)
  python procedural_test.py -k 3                  # tune top-k
  python procedural_test.py --weight core         # only core cases
  python procedural_test.py --json                # machine-readable output
  python procedural_test.py --baseline save out.json   # snapshot current results
  python procedural_test.py --baseline diff out.json   # diff vs a snapshot

The drift guard for going live: snapshot continuity_test with procedural
injection OFF, flip it ON, diff — require "No drift". procedural_test measures
that the L2 layer itself actually surfaces the right habit.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import procedural_lib as pl

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

CASES_PATH = Path(__file__).resolve().parent / "procedural_cases.json"
DEFAULT_K = 5


def _load_cases() -> dict:
    with CASES_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def _hay(h: dict) -> str:
    """Searchable text for a retrieved heuristic — trigger + action, lowered."""
    return f"{h.get('trigger', '')} {h.get('action', '')}".lower()


def _run_case(case: dict, k: int) -> dict:
    task = case["task_context"]
    markers = [m.lower() for m in case.get("expect_contains", [])]
    top_k = case.get("top_k", k)

    try:
        hits = pl.retrieve(task, k=max(top_k, 10))
    except Exception as e:  # never let one case abort the suite
        return {"id": case["id"], "task_context": task, "expect_contains": markers,
                "weight": case.get("weight", "core"), "top_k": top_k, "passed": False,
                "rank": None, "matched": None, "top_results": [],
                "error": f"{type(e).__name__}: {e}"}

    matched = None
    rank = None
    for i, h in enumerate(hits[:top_k], start=1):
        hay = _hay(h)
        if any(m in hay for m in markers):
            matched = h.get("action", "")
            rank = i
            break

    return {
        "id": case["id"],
        "task_context": task,
        "expect_contains": markers,
        "weight": case.get("weight", "core"),
        "top_k": top_k,
        "passed": matched is not None,
        "rank": rank,
        "matched": matched,
        "top_results": [(round(float(h.get("cosine", 0)), 4), h.get("action", "")[:70]) for h in hits[:5]],
    }


def _mrr(results: list[dict]) -> float:
    """Mean reciprocal rank across all cases — soft quality/trend metric."""
    if not results:
        return 0.0
    total = 0.0
    for r in results:
        if r.get("rank"):
            total += 1.0 / r["rank"]
    return total / len(results)


def run_precision(distractors: list[str], threshold: float | None = None,
                  k: int | None = None) -> dict:
    """False-injection guard. A distractor prompt has no applicable habit, so no
    retrieved heuristic should clear the injection threshold (procedural_lib's
    MATCH_MIN) within the injection window. Measures recall-lane precision at
    POOL SCALE — the isolated unit pool (3 heuristics) can't surface this, which
    is how the 0.45-threshold false-positive slipped past the green suite.

    k defaults to procedural_lib.RECALL_K (the actual injection window), so the
    metric matches production; pass a larger k to probe the safety margin."""
    thr = pl.MATCH_MIN if threshold is None else threshold
    kk = pl.RECALL_K if k is None else k
    leaks = []
    for d in distractors:
        try:
            hits = pl.retrieve(d, k=kk)
        except Exception:
            hits = []
        # a leak = ANY heuristic in the injection window clears the bar
        clearing = [h for h in hits if float(h.get("cosine", 0)) >= thr]
        if clearing:
            top = clearing[0]
            leaks.append({"prompt": d, "cosine": round(float(top["cosine"]), 4),
                          "action": top["action"][:60]})
    n = len(distractors)
    return {"distractors": n, "threshold": thr, "k": kk, "leaks": leaks,
            "false_inject_rate": (len(leaks) / n) if n else 0.0}


def run(cases: list[dict] | None = None, k: int = DEFAULT_K,
        weight_filter: str | None = None) -> dict:
    distractors: list[str] = []
    near_miss: list[str] = []
    if cases is None:
        cfg = _load_cases()
        cases = cfg.get("cases", [])
        k = cfg.get("default_k", k)
        distractors = cfg.get("distractors", [])
        near_miss = cfg.get("near_miss", [])
    if weight_filter:
        cases = [c for c in cases if c.get("weight") == weight_filter]

    results = [_run_case(c, k) for c in cases]
    passed = sum(1 for r in results if r["passed"])
    total = len(results)

    report = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "k": k,
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": (passed / total) if total else 0.0,
        "mrr": _mrr(results),
        "results": results,
    }
    if distractors:
        report["precision"] = run_precision(distractors)        # hard gate
    if near_miss:
        report["near_miss"] = run_precision(near_miss)          # soft / tracked
    return report


def diff_reports(current: dict, baseline: dict) -> dict:
    """Pure drift computation between two reports, keyed by case id.
    Returns regressions (was-pass now-fail), new_passes, and rank_drift."""
    base_by_id = {r["id"]: r for r in baseline.get("results", [])}
    regressions, new_passes, rank_drift, match_changed = [], [], [], []
    for r in current.get("results", []):
        b = base_by_id.get(r["id"])
        if not b:
            continue
        if b.get("passed") and not r.get("passed"):
            regressions.append(r)
        elif not b.get("passed") and r.get("passed"):
            new_passes.append(r)
        else:
            if r.get("rank") and b.get("rank") and r["rank"] != b["rank"]:
                rank_drift.append((r["id"], b["rank"], r["rank"]))
            # same rank but a DIFFERENT heuristic now satisfies the marker — a
            # silent semantic regression a rank-only diff would miss.
            if r.get("matched") and b.get("matched") and r["matched"] != b["matched"]:
                match_changed.append(r)
    return {"regressions": regressions, "new_passes": new_passes,
            "rank_drift": rank_drift, "match_changed": match_changed}


def _print_human(report: dict) -> None:
    pct = report["pass_rate"] * 100
    summary = f"Pass: {report['passed']}/{report['total']} ({pct:.0f}%)  |  MRR: {report['mrr']:.3f}  |  k={report['k']}"
    print()
    print(f"  {'✓' if report['failed'] == 0 else '✗'} {summary}")
    prec = report.get("precision")
    if prec is not None:
        leaks = prec["leaks"]
        mark = "✓" if not leaks else "✗"
        print(f"  {mark} Precision: {prec['distractors'] - len(leaks)}/{prec['distractors']} "
              f"pure distractors silent (>= {prec['threshold']:.2f})"
              + ("" if not leaks else f"  | LEAKS: " + "; ".join(f"{l['cosine']} {l['prompt']!r}" for l in leaks)))
    nm = report.get("near_miss")
    if nm is not None:
        silent = nm["distractors"] - len(nm["leaks"])
        note = "" if not nm["leaks"] else "  | topical: " + "; ".join(f"{l['cosine']} {l['prompt']!r}" for l in nm["leaks"])
        print(f"  ℹ Near-miss: {silent}/{nm['distractors']} silent (adjacent-topic leaks tolerated){note}")
    print()
    for r in report["results"]:
        if r["passed"]:
            detail = f"rank {r['rank']} → {r['matched'][:60]}"
        elif r.get("error"):
            detail = f"ERROR: {r['error']}"
        else:
            top = "; ".join(f"{a}" for _, a in r["top_results"][:2]) or "(no results)"
            detail = f"missed | top: {top}"
        print(f"  [{'✓' if r['passed'] else '✗'}] [{r.get('weight', 'core'):>7s}] {r['id']:<22s} {detail}")
    failed = [r for r in report["results"] if not r["passed"]]
    if failed:
        print()
        print("  Failed cases:")
        for r in failed:
            print(f"    [{r['id']}] {r['task_context']}")
            print(f"       expected marker: {r['expect_contains']}")
            for cos, act in r["top_results"]:
                print(f"         ({cos}) {act}")
    print()


def _diff_against(report: dict, baseline_path: Path) -> None:
    if not baseline_path.exists():
        print(f"  Baseline not found: {baseline_path}", file=sys.stderr)
        return
    with baseline_path.open("r", encoding="utf-8") as f:
        baseline = json.load(f)
    d = diff_reports(report, baseline)
    print()
    print(f"  Baseline:  passed {baseline.get('passed', '?')}/{baseline.get('total', '?')}, MRR {baseline.get('mrr', 0):.3f}")
    print(f"  Current:   passed {report['passed']}/{report['total']}, MRR {report['mrr']:.3f}")
    print()
    if not any(d.values()):
        print("  No drift detected.")
        return
    if d["regressions"]:
        print(f"  REGRESSIONS ({len(d['regressions'])}):")
        for r in d["regressions"]:
            print(f"    - {r['id']}: {r['task_context']}")
    if d["new_passes"]:
        print(f"  NEW PASSES ({len(d['new_passes'])}):")
        for r in d["new_passes"]:
            print(f"    + {r['id']}: now rank {r.get('rank')}")
    if d["rank_drift"]:
        print(f"  RANK DRIFT ({len(d['rank_drift'])}):")
        for cid, b_rank, r_rank in d["rank_drift"]:
            print(f"    {'↑' if r_rank < b_rank else '↓'} {cid}: rank {b_rank} → {r_rank}")
    if d.get("match_changed"):
        print(f"  MATCH CHANGED ({len(d['match_changed'])}) — same pass, different habit:")
        for r in d["match_changed"]:
            print(f"    ~ {r['id']}: now → {r.get('matched', '')[:60]}")
    print()


def main():
    ap = argparse.ArgumentParser(description="Procedural (L2) retrieval regression harness")
    ap.add_argument("-k", type=int, default=DEFAULT_K, help="top-k for retrieval (default 5)")
    ap.add_argument("--weight", choices=["core", "project"], default=None)
    ap.add_argument("--json", action="store_true", help="machine-readable JSON only")
    ap.add_argument("--baseline", nargs=2, metavar=("MODE", "PATH"),
                    help="MODE=save|diff, PATH=baseline.json")
    args = ap.parse_args()

    report = run(k=args.k, weight_filter=args.weight)

    if args.baseline:
        mode, path = args.baseline
        path = Path(path)
        if mode == "save":
            path.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(f"  Saved baseline: {path}  (passed {report['passed']}/{report['total']})")
            return
        if mode == "diff":
            _print_human(report)
            _diff_against(report, path)
            return
        print(f"  Unknown baseline mode: {mode}", file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps(report, indent=2))
        return

    _print_human(report)
    # hard gate: case failures OR a pure-distractor leak fail the run; near-miss is informational
    pure_leak = bool(report.get("precision", {}).get("leaks"))
    sys.exit(0 if report["failed"] == 0 and not pure_leak else 1)


if __name__ == "__main__":
    main()
