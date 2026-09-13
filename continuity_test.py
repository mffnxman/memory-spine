"""
continuity_test.py — regression harness for the memory retrieval system.

Reads curated query→expected-memory pairs from continuity_cases.json and
runs each through `search_hybrid`. Pass criterion: any of `expected` must
appear in the top-K results. Reports per-case rank, MRR, and pass rate.

Usage:
  python continuity_test.py                       # run all cases
  python continuity_test.py --weight core         # only core cases
  python continuity_test.py --json                # machine-readable output
  python continuity_test.py --baseline save out.json  # save current results as baseline
  python continuity_test.py --baseline diff out.json  # diff against a baseline

Pass criterion is intentionally loose-ish (top-3 contains) — too strict and
ties become flaky; too loose and the test is meaningless. MRR is reported as
a soft trend metric.

Designed to be cheap so the user runs it freely after memory-system changes.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from memory_engine import search_hybrid, list_memories

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

CASES_PATH = Path(__file__).parent / "continuity_cases.json"


def _load_cases() -> dict:
    with CASES_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def _run_case(case: dict, mems, default_top_k: int) -> dict:
    query = case["query"]
    expected = set(case.get("expected", []))
    top_k = case.get("top_k", default_top_k)

    try:
        results = search_hybrid(query, mems=mems, top_k=max(top_k, 10))
    except Exception as e:
        return {
            "id": case["id"],
            "query": query,
            "expected": list(expected),
            "passed": False,
            "rank": None,
            "matched": None,
            "top_results": [],
            "error": f"{type(e).__name__}: {e}",
        }

    ranked = [(m.filename, score) for m, score, _ in results]
    matched = None
    rank = None
    for i, (fn, _) in enumerate(ranked[:top_k], start=1):
        if fn in expected:
            matched = fn
            rank = i
            break

    return {
        "id": case["id"],
        "query": query,
        "expected": sorted(expected),
        "weight": case.get("weight", "core"),
        "top_k": top_k,
        "passed": matched is not None,
        "rank": rank,
        "matched": matched,
        "top_results": ranked[:5],
    }


def _mrr(results: list[dict]) -> float:
    """Mean reciprocal rank — soft quality metric across all cases."""
    if not results:
        return 0.0
    total = 0.0
    for r in results:
        if r.get("rank"):
            total += 1.0 / r["rank"]
    return total / len(results)


def run(weight_filter: str | None = None) -> dict:
    cfg = _load_cases()
    cases = cfg.get("cases", [])
    if weight_filter:
        cases = [c for c in cases if c.get("weight") == weight_filter]
    default_top_k = cfg.get("default_top_k", 3)

    mems = list_memories()

    results = [_run_case(c, mems, default_top_k) for c in cases]
    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    mrr = _mrr(results)

    return {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": (passed / total) if total else 0.0,
        "mrr": mrr,
        "results": results,
    }


def _print_human(report: dict) -> None:
    failed = [r for r in report["results"] if not r["passed"]]
    pct = report["pass_rate"] * 100
    summary_line = f"Pass: {report['passed']}/{report['total']} ({pct:.0f}%)  |  MRR: {report['mrr']:.3f}"

    print()
    if report["failed"] == 0:
        print(f"  ✓ {summary_line}")
    else:
        print(f"  ✗ {summary_line}")
    print()

    for r in report["results"]:
        if r["passed"]:
            mark = "✓"
            detail = f"rank {r['rank']} → {r['matched']}"
        else:
            mark = "✗"
            err = r.get("error")
            if err:
                detail = f"ERROR: {err}"
            else:
                top3 = ", ".join(fn for fn, _ in r["top_results"][:3]) or "(no results)"
                detail = f"missed | top-3: {top3}"
        weight = r.get("weight", "core")
        print(f"  [{mark}] [{weight:>7s}] {r['id']:<24s} {detail}")

    if failed:
        print()
        print("  Failed cases (top-5 actual):")
        for r in failed[:6]:
            print(f"    [{r['id']}] {r['query']}")
            print(f"       expected: {r['expected']}")
            for fn, score in r["top_results"][:5]:
                print(f"         {fn}  (score {score:.4f})")
    print()


def _diff_against(report: dict, baseline_path: Path) -> None:
    if not baseline_path.exists():
        print(f"  Baseline not found: {baseline_path}", file=sys.stderr)
        return
    with baseline_path.open("r", encoding="utf-8") as f:
        baseline = json.load(f)

    base_by_id = {r["id"]: r for r in baseline.get("results", [])}
    new_failures = []
    new_passes = []
    rank_drift = []
    for r in report["results"]:
        b = base_by_id.get(r["id"])
        if not b:
            continue
        if b["passed"] and not r["passed"]:
            new_failures.append(r)
        elif not b["passed"] and r["passed"]:
            new_passes.append(r)
        elif r["rank"] and b.get("rank") and r["rank"] != b["rank"]:
            rank_drift.append((r["id"], b["rank"], r["rank"]))

    print()
    print(f"  Baseline:  passed {baseline.get('passed', '?')}/{baseline.get('total', '?')}, MRR {baseline.get('mrr', 0):.3f}")
    print(f"  Current:   passed {report['passed']}/{report['total']}, MRR {report['mrr']:.3f}")
    print()
    if not new_failures and not new_passes and not rank_drift:
        print("  No drift detected.")
        return
    if new_failures:
        print(f"  REGRESSIONS ({len(new_failures)}):")
        for r in new_failures:
            print(f"    - {r['id']}: {r['query']}")
    if new_passes:
        print(f"  NEW PASSES ({len(new_passes)}):")
        for r in new_passes:
            print(f"    + {r['id']}: now rank {r.get('rank')}")
    if rank_drift:
        print(f"  RANK DRIFT ({len(rank_drift)}):")
        for cid, b_rank, r_rank in rank_drift:
            arrow = "↑" if r_rank < b_rank else "↓"
            print(f"    {arrow} {cid}: rank {b_rank} → {r_rank}")
    print()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weight", choices=["core", "project"], default=None)
    parser.add_argument("--json", action="store_true", help="machine-readable JSON only")
    parser.add_argument("--baseline", nargs=2, metavar=("MODE", "PATH"),
                        help="MODE=save|diff, PATH=baseline.json file")
    args = parser.parse_args()

    report = run(weight_filter=args.weight)

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
    sys.exit(0 if report["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
