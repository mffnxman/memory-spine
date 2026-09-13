"""
demo_hybrid.py — A/B comparison of TF-IDF vs Hybrid (TF-IDF + Vector RRF) retrieval.

Runs each query through both methods, prints top results side-by-side.
Designed to expose semantic queries that keyword search would miss.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from memory_engine import search, search_hybrid, vector_search, list_memories

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Queries chosen to expose semantic vs keyword gap
QUERIES = [
    "feeling lost in a project",                         # no exact words match anything
    "how does the user handle setbacks",                   # paraphrase of scientific-mindset
    "tools we use every day",                             # paraphrase of stack
    "what's the bond between us",                        # paraphrase of partnership
    "is it okay to be unsure",                           # paraphrase of substrate-agnostic
    "pulling values from a spreadsheet",                 # paraphrase of work-orders
]

mems = list_memories()
W = 95

for q in QUERIES:
    print("=" * W)
    print(f"QUERY: {q!r}")
    print("=" * W)

    print("\n  --- TF-IDF only (old) ---")
    tf = search(q, mems=mems, top_k=4)
    for i, (m, score, terms) in enumerate(tf, 1):
        print(f"  {i}. [{score:5.2f}] {m.filename:35s}  {m.name[:45]}")
    if not tf:
        print("    (no hits — keyword search found nothing)")

    print("\n  --- Vector only ---")
    vc = vector_search(q, mems=mems, top_k=4)
    for i, (m, score) in enumerate(vc, 1):
        print(f"  {i}. [{score:5.2f}] {m.filename:35s}  {m.name[:45]}")

    print("\n  --- Hybrid (RRF fusion) ---")
    hy = search_hybrid(q, mems=mems, top_k=4)
    for i, (m, score, terms) in enumerate(hy, 1):
        print(f"  {i}. [{score:5.4f}] {m.filename:35s}  {m.name[:45]}")
    print()
