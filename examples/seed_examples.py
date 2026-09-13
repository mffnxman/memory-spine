"""seed_examples.py — build the runnable state for the synthetic starter corpus.

The memories in examples/memory/ are plain markdown and ship with the repo.
Everything derived from them lives in examples/memory/_meta/ (gitignored) and
is rebuilt by this script:

  1. embeddings for every memory          (memory_engine.reindex_embeddings)
  2. the knowledge graph                  (kg_bootstrap, seeded from kg_seeds.example.json)
  3. a small pool of procedural heuristics (procedural_lib.upsert_heuristic)

Run it once before the test suite or the regression harnesses:

    set MEMORY_HOME=<repo>/examples/memory      (Windows)
    export MEMORY_HOME=<repo>/examples/memory   (POSIX)
    python examples/seed_examples.py
    python -m pytest -q
    python continuity_test.py
    python procedural_test.py

It is idempotent: re-running re-embeds only changed memories and upvotes
duplicate heuristics instead of inserting them twice.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
CORPUS = HERE / "memory"

# Default MEMORY_HOME to the starter corpus so the script does the right thing
# even when run bare. An explicit MEMORY_HOME still wins.
os.environ.setdefault("MEMORY_HOME", str(CORPUS))
sys.path.insert(0, str(SCRIPTS))

import paths as _paths  # noqa: E402

if Path(os.environ["MEMORY_HOME"]).resolve() != CORPUS.resolve():
    print(f"note: MEMORY_HOME={os.environ['MEMORY_HOME']} (not the starter corpus)")

import memory_engine as me  # noqa: E402
import procedural_lib as pl  # noqa: E402

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Heuristics the procedural (L2) layer would normally distil from tool-error
# recoveries and feedback memories. Seeded directly here so the retrieval
# harness has a pool to search on a fresh clone.
HEURISTICS = [
    {
        "trigger": "When running shell commands on Windows through the Bash tool",
        "action": "use POSIX syntax (ls, cat, sed, forward slashes); PowerShell cmdlets such as Get-ChildItem fail in Git Bash",
        "insight": "The Bash tool is Git Bash, not PowerShell. Reach for the PowerShell tool when a cmdlet is genuinely needed.",
    },
    {
        "trigger": "When a Python script prints emoji or other non-ASCII output on Windows",
        "action": "set PYTHONIOENCODING=utf-8 or reconfigure sys.stdout before printing, otherwise UnicodeEncodeError",
        "insight": "The default console code page cannot encode check marks; the crash happens at print time, after the real work succeeded.",
    },
    {
        "trigger": "When a test suite touches a production sqlite database",
        "action": "copy the database to a temp path and point the engine at the copy before the module is imported",
        "insight": "Tests that DELETE rows will corrupt live data on every run unless isolation is set up before import.",
    },
    {
        "trigger": "When a bug report arrives",
        "action": "reproduce it with a failing test before changing any code, then keep the test as the regression guard",
        "insight": "Fixing the symptom without a reproduction has cost more attempts than writing the test first.",
    },
    {
        "trigger": "When evaluating a paid SaaS tool or subscription",
        "action": "first check whether existing scripts can do it; build-not-buy is the default and purchase is the exception",
        "insight": "Most one-feature services can be replaced by a short script and a scheduled task in an afternoon.",
    },
    {
        "trigger": "When a long-running background job may be killed mid-run",
        "action": "write the skeleton output first and upgrade it in place, so a killed run degrades to a skeleton instead of a gap",
        "insight": "A missing artifact is invisible; a skeleton artifact is a visible, retryable failure.",
    },
]


def main() -> int:
    print(_paths.describe())
    print()

    mems = me.list_memories()
    print(f"memories on disk: {len(mems)}")
    if not mems:
        print("!! no memories found under MEMORY_HOME; nothing to seed")
        return 1

    if me._get_embedder() is None:
        print("!! fastembed not installed (pip install fastembed); embeddings skipped")
        return 1

    r = me.reindex_embeddings(force=False)
    print(
        f"embeddings: updated {r.get('updated', 0)}, skipped {r.get('skipped', 0)}, total {r.get('total_indexed', 0)}"
    )

    import kg_bootstrap  # noqa: E402  (loads kg_seeds.example.json when no private seed file exists)

    kg_bootstrap.main()
    print()

    pl.ensure_schema()
    added = upvoted = 0
    for h in HEURISTICS:
        res = pl.upsert_heuristic(
            h,
            origin="feedback",
            polarity="failure_derived",
            source_refs=["examples/seed_examples.py"],
        )
        if res["op"] == "added":
            added += 1
        else:
            upvoted += 1
    stats = pl.index_stats()
    print(f"heuristics: added {added}, upvoted {upvoted}, active {stats.get('active')}")
    print(
        "\nseeded. next: python -m pytest -q · python continuity_test.py · python procedural_test.py"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
