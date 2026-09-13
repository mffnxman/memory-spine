"""
reindex.py — (re)compute vector embeddings for all memory files.

Idempotent: skips memories whose content hash hasn't changed unless --force.

Usage:
  python reindex.py            # incremental
  python reindex.py --force    # rebuild everything
  python reindex.py --status   # just report current state
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from memory_engine import (
    list_memories, reindex_embeddings, _get_embedder, EMBEDDING_MODEL, db
)

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def status():
    mems = list_memories()
    with db() as conn:
        indexed = {row[0] for row in conn.execute("SELECT filename FROM embeddings")}
    on_disk = {m.filename for m in mems}
    print(f"Model:           {EMBEDDING_MODEL}")
    print(f"Memories:        {len(mems)}")
    print(f"Indexed:         {len(indexed)}")
    print(f"Missing index:   {len(on_disk - indexed)}")
    print(f"Stale index:     {len(indexed - on_disk)} (file deleted)")
    if on_disk - indexed:
        print("\nMissing:")
        for f in sorted(on_disk - indexed):
            print(f"  - {f}")


def main():
    if "--status" in sys.argv:
        status()
        return

    force = "--force" in sys.argv
    print(f"Loading model {EMBEDDING_MODEL}...")
    t0 = time.time()
    if _get_embedder() is None:
        print("ERR: fastembed not installed. Run: pip install fastembed")
        sys.exit(1)
    print(f"  loaded in {time.time()-t0:.1f}s")

    print("Reindexing...")
    t0 = time.time()
    result = reindex_embeddings(force=force)
    elapsed = time.time() - t0
    print(f"  done in {elapsed:.1f}s")
    print(f"  updated:       {result.get('updated', 0)}")
    print(f"  skipped:       {result.get('skipped', 0)}")
    print(f"  total indexed: {result.get('total_indexed', 0)}")


if __name__ == "__main__":
    main()
