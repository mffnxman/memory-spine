"""
kg_bootstrap.py — populate the KG from existing memories using pattern-based
extraction + curated seed entities.

Strategy:
  1. Seed canonical entities from `kg_seeds.json` (the user, their employer,
     teammates, key tools, values). The file is private and gitignored;
     `kg_seeds.example.json` shows the shape.
  2. Pattern-extract candidates from each memory:
       - Code blocks / file paths -> tool/file entities
       - Capitalized phrases of 1-3 words -> candidate entities
       - Inline code with `slash-commands` -> skill entities
  3. Pattern-extract relationships:
       - "X uses Y", "X built Y", "X manages Y" — verb-pattern matches
       - frontmatter `related:` field -> related_to relationships
       - `type: feedback` memories -> <user> values <concept>
  4. Optimize for precision over recall — better to have fewer high-quality
     entities than a sea of noise.

Re-runnable: idempotent via upsert.

Seed file format (kg_seeds.json, next to this script or at $MEMORY_HOME):
  {
    "entities": [ {"name": "Alex", "type": "person", "aliases": ["A."]}, ... ],
    "relationships": [
      {"from": "Alex", "to": "Acme Corp", "rel": "works_at",
       "from_type": "person", "to_type": "company"}, ...
    ]
  }
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import paths as _paths  # noqa: E402
from memory_engine import list_memories  # noqa: E402
from kg import (
    upsert_entity,
    upsert_relationship,
    add_mention,
    graph_stats,
)  # noqa: E402

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ─── Seed entities (curated high-confidence, loaded from file) ───────────────
SEEDS_CANDIDATES = [
    Path(__file__).parent / "kg_seeds.json",
    _paths.MEMORY_DIR / "kg_seeds.json",
    Path(__file__).parent / "kg_seeds.example.json",
]

# Entities the engine itself always knows about, independent of the seed file.
BUILTIN_SEEDS = [
    (_paths.USER_NAME, "person", []),
    ("Claude", "person", ["Claude (AI)"]),
    ("Claude Code", "tool", []),
    ("memory_engine", "tool", []),
    ("epilogue system", "tool", []),
    ("knowledge graph", "concept", []),
    ("session continuity", "concept", ["continuity stack"]),
    ("memory inheritance", "concept", []),
]
BUILTIN_RELS = [
    (_paths.USER_NAME, "Claude", "partners_with", "person", "person"),
    ("Claude", "Claude Code", "runs_on", "person", "tool"),
    ("epilogue system", "memory_engine", "depends_on", "tool", "tool"),
    ("knowledge graph", "memory_engine", "depends_on", "concept", "tool"),
]


def _load_seed_file() -> tuple[list, list]:
    for cand in SEEDS_CANDIDATES:
        if cand.exists():
            try:
                data = json.loads(cand.read_text(encoding="utf-8"))
            except Exception as e:  # a broken seed file must not kill the bootstrap
                print(f"  ! could not parse {cand.name}: {e}")
                continue
            ents = [
                (e["name"], e.get("type", "concept"), list(e.get("aliases", [])))
                for e in data.get("entities", [])
                if e.get("name")
            ]
            rels = [
                (
                    r["from"],
                    r["to"],
                    r.get("rel", "related_to"),
                    r.get("from_type", "concept"),
                    r.get("to_type", "concept"),
                )
                for r in data.get("relationships", [])
                if r.get("from") and r.get("to")
            ]
            print(
                f"  seeds from {cand.name}: {len(ents)} entities, {len(rels)} relationships"
            )
            return ents, rels
    return [], []


_FILE_ENTS, _FILE_RELS = _load_seed_file()
SEEDS = BUILTIN_SEEDS + _FILE_ENTS
SEED_RELS = BUILTIN_RELS + _FILE_RELS


def seed_entities():
    n = 0
    for name, etype, aliases in SEEDS:
        upsert_entity(name, etype, aliases=aliases, confidence=1.0)
        n += 1
    return n


def seed_relationships():
    n = 0
    for from_name, to_name, rel_type, from_type, to_type in SEED_RELS:
        upsert_relationship(
            from_name, to_name, rel_type, from_type=from_type, to_type=to_type
        )
        n += 1
    return n


# ─── Memory mention extraction ───────────────────────────────────────────────
def extract_mentions(mems):
    """For each memory, find which seed entities are mentioned in body."""
    n_mentions = 0
    # Build lookup: lower(name|alias) -> canonical_name
    lookup: dict[str, str] = {}
    for name, etype, aliases in SEEDS:
        lookup[name.lower()] = name
        for a in aliases:
            lookup[a.lower()] = name

    # Sort by length descending so longer aliases match first
    sorted_keys = sorted(lookup.keys(), key=len, reverse=True)

    for m in mems:
        body_lower = m.body.lower()
        seen_in_this_mem: set[str] = set()
        for key in sorted_keys:
            if len(key) < 3:
                continue
            if re.search(rf"\b{re.escape(key)}\b", body_lower):
                canonical = lookup[key]
                if canonical in seen_in_this_mem:
                    continue
                seen_in_this_mem.add(canonical)
                # Pull the line containing the match for excerpt
                excerpt = ""
                for line in m.body.splitlines():
                    if key in line.lower():
                        excerpt = line.strip()[:200]
                        break
                add_mention(canonical, m.filename, excerpt)
                n_mentions += 1
    return n_mentions


# ─── Frontmatter relationships ───────────────────────────────────────────────
def extract_frontmatter_rels(mems):
    """`related: a.md, b.md` -> Memory(name) related_to Memory(name)."""
    n = 0
    name_by_filename = {m.filename: m.name for m in mems}
    for m in mems:
        for r in m.related:
            r = r.strip()
            if not r:
                continue
            other_name = name_by_filename.get(r)
            if not other_name:
                continue
            # Create memory entities for each side
            upsert_entity(m.name, "concept", confidence=0.9)
            upsert_entity(other_name, "concept", confidence=0.9)
            upsert_relationship(
                m.name,
                other_name,
                "related_to",
                from_type="concept",
                to_type="concept",
                source_memory=m.filename,
            )
            n += 1
    return n


# ─── Heuristic verb-pattern extraction (precision over recall) ──────────────
_U = re.escape(_paths.USER_NAME)
VERB_PATTERNS = [
    (rf"\b{_U}\s+(?:uses|runs|relies\s+on|loves)\s+([A-Z][\w/_.-]+)", "uses"),
    (rf"\b{_U}\s+manages\s+([A-Z][\w\s,.-]+?)(?:\.|,| and|;|$)", "manages"),
    (rf"\b([A-Z][\w_-]+)\s+(?:built\s+by|written\s+by|by)\s+{_U}", "built_by"),
]


def extract_verb_patterns(mems):
    n = 0
    for m in mems:
        for pattern, rel_type in VERB_PATTERNS:
            for match in re.finditer(pattern, m.body):
                target = match.group(1).strip()
                if len(target) < 3 or len(target) > 50:
                    continue
                upsert_relationship(
                    _paths.USER_NAME,
                    target,
                    rel_type,
                    from_type="person",
                    to_type="concept",
                    confidence=0.6,
                    source_memory=m.filename,
                )
                n += 1
    return n


def main():
    print("Bootstrapping knowledge graph...")
    n_seed_e = seed_entities()
    print(f"  Seeded {n_seed_e} entities")
    n_seed_r = seed_relationships()
    print(f"  Seeded {n_seed_r} relationships")

    mems = list_memories()
    n_mention = extract_mentions(mems)
    print(f"  Extracted {n_mention} entity mentions across {len(mems)} memories")
    n_fm = extract_frontmatter_rels(mems)
    print(f"  Extracted {n_fm} frontmatter `related:` links")
    n_verb = extract_verb_patterns(mems)
    print(f"  Extracted {n_verb} verb-pattern relationships")

    print("\nFinal stats:")
    s = graph_stats()
    print(f"  Entities:      {s['entities']}")
    print(f"  Relationships: {s['relationships']}")
    print(f"  Mentions:      {s['mentions']}")
    print(f"  By type:       {s['by_type']}")
    print(f"  Top hubs:")
    for h in s["top_hubs"][:8]:
        print(f"    {h['name']:25s}  ({h['type']:8s})  deg={h['degree']}")


if __name__ == "__main__":
    main()
