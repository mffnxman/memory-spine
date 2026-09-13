"""backlink.py — auto-backlinking sleep pass (v15, 2026-07-09).

The related: graph is the spine's synapse layer, and most memories have none
(70+ of 108 at ship time). Each consolidate cycle this pass gives every
under-linked memory its top embedding neighbors:

  - candidates ranked by cosine over the embeddings table
  - threshold SIM_MIN (default 0.55) — no weak links
  - cap CAP total related links per memory (default 5)
  - APPEND-ONLY: hand-curated links are never touched or reordered
  - digest_* / session_handoff_* excluded both directions (window summaries,
    not semantic peers — same exclusion auto_promote uses)
  - provenance snapshot before each edit, reindex event after

CLI:
    python backlink.py run        # apply
    python backlink.py dry-run    # report only
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import sys
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

SIM_MIN = 0.55
CAP = 5
EXCLUDE_PREFIXES = ("digest_", "session_handoff_")


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _excluded(fn):
    return fn.startswith(EXCLUDE_PREFIXES)


def suggest(vectors, existing, sim_min=SIM_MIN, cap=CAP):
    """Pure logic: {filename: [new_related, ...]} for under-linked files.

    vectors: {filename: vec}; existing: {filename: [related filenames]}."""
    out = {}
    files = [fn for fn in vectors if not _excluded(fn)]
    for fn in files:
        have = list(existing.get(fn, []))
        slots = cap - len(have)
        if slots <= 0:
            continue
        ranked = sorted(
            (
                (other, _cosine(vectors[fn], vectors[other]))
                for other in files
                if other != fn and other not in have
            ),
            key=lambda t: -t[1],
        )
        picks = [other for other, sim in ranked[:slots] if sim >= sim_min]
        if picks:
            out[fn] = picks
    return out


def _snapshot(path):
    """Provenance snapshot before edit (fail-soft)."""
    try:
        from provenance import snapshot

        snapshot(Path(path), reason="backlink-pass")
    except Exception:
        pass


def _emit_reindex(path):
    try:
        from event_bus import emit_event

        emit_event("reindex", {"file_path": str(path)})
    except Exception:
        pass


def _parse_related(text):
    m = re.search(r"^related:\s*(.*)$", text, re.M)
    if not m:
        return []
    return [t.strip() for t in m.group(1).split(",") if t.strip()]


def _append_related(text, additions):
    """Append additions to the related: line, creating it if missing."""
    m = re.search(r"^(related:\s*)(.*)$", text, re.M)
    if m:
        line = m.group(0)
        current = m.group(2).strip()
        new_line = (
            m.group(1) + current + (", " if current else "") + ", ".join(additions)
        )
        return text.replace(line, new_line, 1)
    # no related: key — insert before the closing frontmatter fence
    fm = re.match(r"^---\s*\n(.*?\n)---", text, re.S)
    if not fm:
        return text  # no frontmatter; don't guess
    insert_at = fm.end(1)
    return (
        text[:insert_at] + "related: " + ", ".join(additions) + "\n" + text[insert_at:]
    )


def run(memory_dir=None, db_path=None, sim_min=SIM_MIN, cap=CAP, write=True):
    """Load spine + embeddings, apply suggestions, write frontmatter."""
    memory_dir = Path(memory_dir or MEMORY_DIR)
    summary = {"files_considered": 0, "files_updated": 0, "links_added": 0}

    texts, existing = {}, {}
    for p in sorted(memory_dir.glob("*.md")):
        if p.name == "MEMORY.md":
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        texts[p.name] = text
        existing[p.name] = _parse_related(text)

    vectors = {}
    with sqlite3.connect(str(db_path or DEFAULT_DB), timeout=5.0) as conn:
        for fn, blob in conn.execute("SELECT filename, vector FROM embeddings"):
            if fn in texts:
                import struct

                vectors[fn] = list(struct.unpack(f"{len(blob) // 4}f", blob))

    summary["files_considered"] = len(vectors)
    suggestions = suggest(vectors, existing, sim_min=sim_min, cap=cap)
    summary["suggestions"] = {fn: adds for fn, adds in suggestions.items()}

    if not write:
        return summary

    for fn, adds in suggestions.items():
        path = memory_dir / fn
        new_text = _append_related(texts[fn], adds)
        if new_text == texts[fn]:
            continue
        _snapshot(path)
        path.write_text(new_text, encoding="utf-8")
        _emit_reindex(path)
        summary["files_updated"] += 1
        summary["links_added"] += len(adds)

    # refresh the refs cache so the graph layer sees the new edges
    try:
        from memory_engine import list_memories, detect_references, cache_references

        cache_references(detect_references(list_memories()))
    except Exception:
        pass

    return summary


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "dry-run"
    if cmd == "run":
        s = run(write=True)
        s.pop("suggestions", None)
        print(json.dumps(s, indent=2))
    elif cmd == "dry-run":
        s = run(write=False)
        print(json.dumps({k: v for k, v in s.items() if k != "suggestions"}, indent=2))
        for fn, adds in sorted(s.get("suggestions", {}).items()):
            print(f"  {fn} += {adds}")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
