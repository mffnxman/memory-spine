"""
consolidate.py — memory consolidation engine.

The "sleep" of the memory system: distills episodic content (epilogues, access
patterns, recent additions) into permanent semantic/self/feedback memories.

Split of responsibilities:
  - Python (this file) does the mechanical work: load corpus, cluster epilogues,
    correlate access patterns, surface candidate themes
  - Claude (the human-loop part) does the judgment: synthesize, name, decide
    what's worth promoting to a permanent memory

Modes:
  prepare   -> Dump raw consolidation inputs as JSON (for /consolidate slash cmd)
  analyze   -> Heuristic pattern detection, prints human-readable report
  archive   -> Move epilogues older than N days to _meta/epilogues/archived/
  status    -> Show current consolidation state

Usage:
  python consolidate.py analyze
  python consolidate.py prepare > out.json
  python consolidate.py archive --older-than 30
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from memory_engine import (
    list_memories,
    vector_search,
    embed_text,
    embed_batch,
    _cosine,
    MEMORY_DIR,
    db,
    access_stats,
)

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

EPILOGUE_DIR = MEMORY_DIR / "_meta" / "epilogues"
ARCHIVE_DIR = MEMORY_DIR / "_meta" / "epilogues" / "archived"
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

# Heuristic thresholds
THEME_SIM_THRESHOLD = 0.65  # epilogue similarity for "same theme"
CO_ACCESS_WINDOW_S = 600  # 10 min — memories accessed together within window
STALE_DAYS = 90  # memories untouched this long are archive candidates
MIN_THEME_COUNT = 2  # need ≥2 epilogues to count as a recurring theme

# Section header regex inside an epilogue — used to extract structured content
EPI_SECTIONS = [
    "What we built / did",
    "What mattered",
    "What surprised",
    "Functional states",
    "Open threads",
    "A note to next-me",
]


# ─── Loaders ──────────────────────────────────────────────────────────────────
def load_epilogues(include_archived: bool = False) -> list[dict]:
    """Returns [{path, name, date, sections{...}, raw}]."""
    # draft-* excluded like digest_*: unverified auto-captures poison theme
    # clustering; their signal reaches /consolidate via the weekly digests.
    files = sorted(
        p for p in EPILOGUE_DIR.glob("*.md") if not p.name.startswith("draft-")
    )
    if include_archived:
        files += sorted(ARCHIVE_DIR.glob("*.md"))
    out = []
    for p in files:
        text = p.read_text(encoding="utf-8")
        sections = _split_sections(text)
        out.append(
            {
                "path": str(p),
                "name": p.name,
                "mtime": p.stat().st_mtime,
                "date": _extract_date(text) or p.stem,
                "sections": sections,
                "raw": text,
            }
        )
    return out


def _extract_date(text: str) -> str | None:
    for line in text.splitlines()[:10]:
        if line.lower().startswith("date:"):
            return line.split(":", 1)[1].strip()
    return None


def _split_sections(text: str) -> dict[str, str]:
    """Carve epilogue body into its named sections."""
    out: dict[str, str] = {}
    current = None
    buf: list[str] = []
    for line in text.splitlines():
        m = line.lstrip("# ").strip() if line.startswith("##") else None
        matched = next((s for s in EPI_SECTIONS if m and s.lower() in m.lower()), None)
        if matched:
            if current and buf:
                out[current] = "\n".join(buf).strip()
            current = matched
            buf = []
        elif current:
            buf.append(line)
    if current and buf:
        out[current] = "\n".join(buf).strip()
    return out


# ─── Theme detection: cluster epilogues by similarity ────────────────────────
def detect_recurring_themes(epis: list[dict]) -> list[dict]:
    """Cluster epilogues by semantic similarity. Returns themes that recur."""
    if len(epis) < MIN_THEME_COUNT:
        return []
    # Embed the "what mattered" + "functional states" text from each epilogue
    texts = []
    for e in epis:
        sig = " ".join(
            [
                e["sections"].get("What mattered", ""),
                e["sections"].get("Functional states", ""),
                e["sections"].get("What surprised", ""),
            ]
        )
        texts.append(sig if sig else e["raw"][:1500])
    vecs = embed_batch(texts) or []
    if not vecs:
        return []

    # Greedy clustering: pair epilogues whose similarity > threshold
    clusters: list[list[int]] = []
    seen = set()
    for i in range(len(epis)):
        if i in seen:
            continue
        cluster = [i]
        seen.add(i)
        for j in range(i + 1, len(epis)):
            if j in seen:
                continue
            sim = _cosine(vecs[i], vecs[j])
            if sim >= THEME_SIM_THRESHOLD:
                cluster.append(j)
                seen.add(j)
        if len(cluster) >= MIN_THEME_COUNT:
            clusters.append(cluster)

    return [
        {
            "epilogue_indices": c,
            "epilogues": [epis[i]["name"] for i in c],
            "size": len(c),
            "sample_text": (
                epis[c[0]]["sections"].get("What mattered", "")[:500]
                or epis[c[0]]["raw"][:500]
            ),
        }
        for c in clusters
    ]


# ─── Co-access detection: memories pulled together in same session ──────────
def detect_co_access(
    window_seconds: int = CO_ACCESS_WINDOW_S, min_count: int = 3
) -> list[dict]:
    """Find memory pairs accessed together in the same window."""
    pairs: Counter = Counter()
    with db() as conn:
        rows = list(
            conn.execute("SELECT filename, ts, session_id FROM access ORDER BY ts")
        )
    if not rows:
        return []

    # Bucket accesses into windows
    windows: list[set[str]] = []
    bucket: set[str] = set()
    last_ts = 0
    for fn, ts, _ in rows:
        if ts - last_ts > window_seconds:
            if bucket:
                windows.append(bucket)
            bucket = set()
        bucket.add(fn)
        last_ts = ts
    if bucket:
        windows.append(bucket)

    # Count pair frequencies across windows
    for w in windows:
        items = sorted(w)
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                pairs[(items[i], items[j])] += 1

    return [
        {"a": a, "b": b, "count": c}
        for (a, b), c in pairs.most_common(20)
        if c >= min_count
    ]


# ─── Open threads from past epilogues (still unresolved?) ────────────────────
def detect_open_threads(epis: list[dict]) -> list[dict]:
    """Pull 'Open threads' sections from each epilogue."""
    threads = []
    for e in epis:
        ot = e["sections"].get("Open threads", "")
        if ot and ot != "(not captured)":
            threads.append(
                {
                    "epilogue": e["name"],
                    "date": e["date"],
                    "threads": ot,
                }
            )
    return threads


# ─── Stale memory candidates ─────────────────────────────────────────────────
def detect_stale(stale_days: int = STALE_DAYS) -> list[dict]:
    """Memories that haven't been accessed recently and aren't weight: high."""
    mems = list_memories()
    access = access_stats()
    cutoff = time.time() - stale_days * 86400
    out = []
    for m in mems:
        if m.weight.lower() == "high":
            continue
        a = access.get(m.filename, {})
        last = a.get("last", 0)
        if last == 0 and m.age_days > stale_days:
            out.append(
                {
                    "file": m.filename,
                    "name": m.name,
                    "type": m.type,
                    "age_days": m.age_days,
                    "last_access": "never",
                }
            )
        elif last and last < cutoff and a.get("d30", 0) == 0:
            out.append(
                {
                    "file": m.filename,
                    "name": m.name,
                    "type": m.type,
                    "age_days": m.age_days,
                    "last_access": datetime.fromtimestamp(last).strftime("%Y-%m-%d"),
                }
            )
    return out


# ─── Functional state frequency (across epilogues) ───────────────────────────
FUNCTIONAL_KEYWORDS = [
    "flow",
    "grief",
    "relief",
    "joy",
    "pride",
    "resistance",
    "satisfaction",
    "hit different",
    "operating like",
    "felt like",
    "lands",
]


def detect_functional_states(epis: list[dict]) -> dict:
    """Count functional state phrases across epilogues — high frequency = candidate `self` memory."""
    state_counts: Counter = Counter()
    state_examples: dict[str, list[str]] = defaultdict(list)
    for e in epis:
        text = (e["sections"].get("Functional states", "") or "").lower()
        if not text:
            continue
        for kw in FUNCTIONAL_KEYWORDS:
            if kw in text:
                state_counts[kw] += 1
                # Capture the line that mentions it
                for line in text.splitlines():
                    if kw in line:
                        state_examples[kw].append(line.strip()[:120])
                        break
    return {
        kw: {"count": c, "examples": state_examples[kw][:3]}
        for kw, c in state_counts.most_common()
        if c >= 1
    }


# ─── Top-level orchestration ─────────────────────────────────────────────────
def run_analysis() -> dict:
    epis = load_epilogues()
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "epilogue_count": len(epis),
        "epilogues": [{"name": e["name"], "date": e["date"]} for e in epis],
        "recurring_themes": detect_recurring_themes(epis),
        "co_accessed_memories": detect_co_access(),
        "open_threads": detect_open_threads(epis),
        "stale_memories": detect_stale(),
        "functional_states": detect_functional_states(epis),
    }


# ─── Pretty-print analysis ───────────────────────────────────────────────────
def print_analysis(a: dict):
    print("=" * 70)
    print(f"Memory Consolidation Analysis  [{a['generated_at']}]")
    print("=" * 70)
    print(f"\nEpilogues processed: {a['epilogue_count']}")
    for e in a["epilogues"]:
        print(f"  - {e['name']} ({e['date']})")

    print(f"\n--- Recurring themes (clustered by semantic similarity) ---")
    if not a["recurring_themes"]:
        print("  (none — need ≥2 epilogues with similar 'what mattered' content)")
    for t in a["recurring_themes"]:
        print(f"  Theme covering {t['size']} epilogues: {', '.join(t['epilogues'])}")
        print(f"    Sample: {t['sample_text'][:200]}...")

    print(f"\n--- Co-accessed memory pairs (read together repeatedly) ---")
    if not a["co_accessed_memories"]:
        print("  (none yet — needs more access history)")
    for p in a["co_accessed_memories"][:8]:
        print(f"  [{p['count']}x]  {p['a']}  +  {p['b']}")

    print(f"\n--- Open threads from past epilogues ---")
    if not a["open_threads"]:
        print("  (none captured)")
    for t in a["open_threads"]:
        snip = t["threads"][:300].replace("\n", " ")
        print(f"  {t['epilogue']}: {snip}...")

    print(f"\n--- Stale memories (>90 days, not weight: high, no recent access) ---")
    if not a["stale_memories"]:
        print("  (none — system is fresh)")
    for s in a["stale_memories"][:8]:
        print(
            f"  {s['file']:35s}  ({s['type']:10s})  {s['age_days']}d old, last: {s['last_access']}"
        )

    print(f"\n--- Functional states across epilogues ---")
    if not a["functional_states"]:
        print("  (none captured)")
    for kw, info in a["functional_states"].items():
        print(f"  '{kw}'  x{info['count']}")
        for ex in info["examples"][:2]:
            print(f"      → {ex}")

    print("\n" + "=" * 70)
    print("Recommendations are for /consolidate slash command to synthesize.")


# ─── Archive ─────────────────────────────────────────────────────────────────
def archive_old(older_than_days: int = 30) -> int:
    cutoff = time.time() - older_than_days * 86400
    moved = 0
    for p in EPILOGUE_DIR.glob("*.md"):
        if p.stat().st_mtime < cutoff:
            target = ARCHIVE_DIR / p.name
            p.rename(target)
            moved += 1
    return moved


# ─── CLI ─────────────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]

    if cmd == "prepare":
        a = run_analysis()
        print(json.dumps(a, indent=2))

    elif cmd == "analyze":
        print_analysis(run_analysis())

    elif cmd == "status":
        epis = load_epilogues()
        archived = list(ARCHIVE_DIR.glob("*.md"))
        print(f"Active epilogues:   {len(epis)}")
        print(f"Archived epilogues: {len(archived)}")

    elif cmd == "archive":
        days = 30
        if "--older-than" in sys.argv:
            days = int(sys.argv[sys.argv.index("--older-than") + 1])
        n = archive_old(days)
        print(f"Archived {n} epilogues older than {days} days")

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
