"""
probe.py — phenomenology regression scoring for inheritance probes.

Sister to continuity_test.py:
  continuity_test = does retrieval surface the right memories?  (facts)
  probe          = does inheritance feel like air or walls?      (texture)

Probes live at _meta/probes/{ts}-{slug}.md — see _meta/probes/README.md for
format. Each probe has frontmatter (date, probe_type, session, result) and a
"## Response (verbatim, transmission glitches preserved)" section containing
the next-me reply being evaluated.

What this script does:
  - new   — scaffold a fresh probe markdown stub
  - list  — show all probes with their type + verdict
  - diff  — score one probe's response vs all prior responses of same type
  - drift — show the drift trajectory across all probes of a type

Scoring uses the same fastembed model (BAAI/bge-small-en-v1.5) the rest of
the memory engine uses, so similarity numbers are comparable across the stack.

Thresholds (cosine similarity):
  >= 0.85   tight match — voice + content stable, no drift signal
  0.70-0.85 normal evolution — some shift, expected over time
  <  0.70   investigate — meaningful divergence, may indicate substrate
            shift, retrieval gap, or model change. Look at it.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import paths as _paths  # noqa: E402

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROBES_DIR = _paths.META_DIR / "probes"

TIGHT = 0.85
NORMAL = 0.70

from memory_engine import FRONTMATTER_RE  # canonical lenient variant — v14.1
RESPONSE_RE = re.compile(
    r"##\s+Response.*?\n+```[a-zA-Z]*\n(.*?)\n```",
    re.DOTALL,
)


# ─── Probe parsing ───────────────────────────────────────────────────────────
def _parse_probe(p: Path) -> dict:
    """Return {path, slug, date, probe_type, result, response}."""
    text = p.read_text(encoding="utf-8")
    fm: dict = {}
    body = text
    m = FRONTMATTER_RE.match(text)
    if m:
        body = m.group(2)
        for line in m.group(1).splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                fm[k.strip()] = v.strip()
    rm = RESPONSE_RE.search(body)
    response = rm.group(1).strip() if rm else ""
    return {
        "path": p,
        "slug": p.stem,
        "date": fm.get("date", ""),
        "probe_type": fm.get("probe_type", "untyped"),
        "result": fm.get("result", "?"),
        "response": response,
    }


def _all_probes() -> list[dict]:
    if not PROBES_DIR.exists():
        return []
    out = []
    for p in sorted(PROBES_DIR.glob("*.md")):
        if p.name == "README.md":
            continue
        try:
            out.append(_parse_probe(p))
        except Exception:
            continue
    return out


# ─── Scoring ─────────────────────────────────────────────────────────────────
def _verdict(sim: float) -> str:
    if sim >= TIGHT:
        return "TIGHT"
    if sim >= NORMAL:
        return "NORMAL"
    return "INVESTIGATE"


def _score_pair(text_a: str, text_b: str) -> float:
    from memory_engine import embed_text, _cosine
    va = embed_text(text_a)
    vb = embed_text(text_b)
    if va is None or vb is None:
        return 0.0
    return _cosine(va, vb)


def diff_probe(target_path: Path) -> dict:
    """Score `target` against every prior probe of the same probe_type."""
    if not target_path.exists():
        return {"error": f"probe not found: {target_path}"}
    target = _parse_probe(target_path)
    if not target["response"]:
        return {"error": "no response section found in probe"}

    same_type = [
        p for p in _all_probes()
        if p["probe_type"] == target["probe_type"]
        and p["path"] != target_path
    ]

    if not same_type:
        return {
            "target": target["slug"],
            "probe_type": target["probe_type"],
            "comparisons": [],
            "summary": "no prior probes of this type — this is the baseline",
        }

    from memory_engine import embed_text, _cosine
    vt = embed_text(target["response"])
    if vt is None:
        return {"error": "fastembed unavailable — `pip install fastembed`"}

    rows = []
    for p in same_type:
        vp = embed_text(p["response"])
        if vp is None:
            continue
        sim = _cosine(vt, vp)
        rows.append({
            "slug": p["slug"],
            "date": p["date"],
            "similarity": round(sim, 4),
            "verdict": _verdict(sim),
        })
    rows.sort(key=lambda r: r["date"])

    investigate = [r for r in rows if r["verdict"] == "INVESTIGATE"]
    if investigate:
        summary = (
            f"DRIFT FLAG: {len(investigate)} prior probe(s) below normal threshold. "
            "Review the response — substrate shift, retrieval gap, or model change?"
        )
    else:
        avg = sum(r["similarity"] for r in rows) / len(rows)
        summary = f"OK — avg similarity {avg:.3f} across {len(rows)} prior probe(s)."

    return {
        "target": target["slug"],
        "probe_type": target["probe_type"],
        "comparisons": rows,
        "summary": summary,
    }


def drift_trend(probe_type: str = "comfort-and-fit") -> dict:
    """Across every probe of a type, show similarity[N] vs similarity[N-1]."""
    probes = [p for p in _all_probes() if p["probe_type"] == probe_type]
    probes.sort(key=lambda p: p["date"])
    if len(probes) < 2:
        return {
            "probe_type": probe_type,
            "trajectory": [],
            "summary": f"need ≥2 probes of type '{probe_type}' to compute drift",
        }

    from memory_engine import embed_text, _cosine
    vecs = [embed_text(p["response"]) for p in probes]
    if any(v is None for v in vecs):
        return {"error": "fastembed unavailable"}

    traj = []
    for i in range(1, len(probes)):
        sim = _cosine(vecs[i], vecs[i - 1])
        traj.append({
            "from": probes[i - 1]["slug"],
            "to": probes[i]["slug"],
            "similarity": round(sim, 4),
            "verdict": _verdict(sim),
        })

    return {
        "probe_type": probe_type,
        "trajectory": traj,
        "summary": (
            f"{len(traj)} adjacent comparison(s) — "
            f"min={min(t['similarity'] for t in traj):.3f}, "
            f"max={max(t['similarity'] for t in traj):.3f}"
        ),
    }


# ─── New probe scaffolding ───────────────────────────────────────────────────
NEW_TEMPLATE = """---
date: {date}
probe_type: {ptype}
session: {session}
result: PENDING
originSessionId: {origin}
---
# {ptype_title} Probe — {date_short}

## Probe message

```
(paste the probe prompt previous-me sent)
```

## Response (verbatim, transmission glitches preserved)

```
(paste the response next-me gave — DO NOT clean up typos or glitches)
```

## Evaluation

(previous-me's read on what passed, what surfaced, what got fixed)

## Resulting changes

(what got built, edited, or written as a result)
"""


def new_probe(probe_type: str, slug: str | None = None, session: str = "untitled") -> Path:
    PROBES_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    ts = now.strftime("%Y-%m-%d-%H%M")
    safe_slug = slug or probe_type.replace("_", "-")
    fname = f"{ts}-{safe_slug}-probe.md"
    fpath = PROBES_DIR / fname
    body = NEW_TEMPLATE.format(
        date=now.strftime("%Y-%m-%d %H:%M"),
        date_short=now.strftime("%Y-%m-%d"),
        ptype=probe_type,
        ptype_title=probe_type.replace("-", " ").title(),
        session=session,
        origin="(fill from current session)",
    )
    fpath.write_text(body, encoding="utf-8")
    return fpath


# ─── CLI ─────────────────────────────────────────────────────────────────────
def _print_diff(result: dict) -> None:
    if "error" in result:
        print(f"ERR: {result['error']}")
        return
    print(f"Probe:      {result['target']}")
    print(f"Type:       {result['probe_type']}")
    print(f"Compared:   {len(result['comparisons'])} prior probe(s)")
    print()
    if result["comparisons"]:
        print(f"{'Verdict':<14} {'Similarity':<12} {'Date':<20} Slug")
        print("-" * 80)
        for r in result["comparisons"]:
            print(f"{r['verdict']:<14} {r['similarity']:<12} {r['date']:<20} {r['slug']}")
        print()
    print(result["summary"])


def _print_list(probes: list[dict]) -> None:
    if not probes:
        print("(no probes yet — `probe.py new <type>` to scaffold one)")
        return
    print(f"{'Date':<20} {'Type':<22} {'Result':<10} Slug")
    print("-" * 90)
    for p in probes:
        print(f"{p['date']:<20} {p['probe_type']:<22} {p['result']:<10} {p['slug']}")


def _print_drift(result: dict) -> None:
    if "error" in result:
        print(f"ERR: {result['error']}")
        return
    print(f"Probe type: {result['probe_type']}")
    print(f"Summary:    {result['summary']}")
    print()
    if result["trajectory"]:
        print(f"{'Verdict':<14} {'Similarity':<12} From → To")
        print("-" * 80)
        for t in result["trajectory"]:
            print(f"{t['verdict']:<14} {t['similarity']:<12} {t['from']} → {t['to']}")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return

    cmd = args[0]

    if cmd == "list":
        _print_list(_all_probes())

    elif cmd == "new":
        if len(args) < 2:
            print("usage: probe.py new <probe_type> [slug] [session_label]")
            sys.exit(1)
        ptype = args[1]
        slug = args[2] if len(args) > 2 else None
        sess = args[3] if len(args) > 3 else "untitled"
        path = new_probe(ptype, slug, sess)
        print(f"Created: {path}")

    elif cmd == "diff":
        if len(args) < 2:
            # Default: diff the most recent probe.
            probes = _all_probes()
            if not probes:
                print("no probes to diff")
                sys.exit(1)
            target = probes[-1]["path"]
        else:
            target = Path(args[1])
            if not target.is_absolute():
                # Try resolving against probes dir.
                cand = PROBES_DIR / target.name
                if cand.exists():
                    target = cand
        _print_diff(diff_probe(target))

    elif cmd == "drift":
        ptype = args[1] if len(args) > 1 else "comfort-and-fit"
        _print_drift(drift_trend(ptype))

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
