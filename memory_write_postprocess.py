"""
memory_write_postprocess.py — PostToolUse hook for Write/Edit on memory files.

When a memory file is written or edited inside the auto-memory dir, run a
single-memory entity-extraction pass so the KG stays current passively. No
need to remember to re-run kg_bootstrap.py.

Operations performed (all idempotent via upsert):
  1. Add the memory itself as a 'concept' entity (so frontmatter related: links
     have a node to point at).
  2. Mention-detect against canonical SEEDS from kg_bootstrap.py.
  3. Walk frontmatter `related:` field — register related_to edges between
     this memory and each linked memory (auto-creating nodes as needed).
  4. Apply the curated VERB_PATTERNS to this memory's body.

Quiet — must never block the write or pollute stdout.

Hook contract:
  PostToolUse(Write|Edit) — receives JSON via stdin
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import paths as _paths  # noqa: E402

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _read_stdin_json() -> dict:
    try:
        data = sys.stdin.read()
        if not data:
            return {}
        return json.loads(data)
    except Exception:
        return {}


def _is_memory_path(file_path: str) -> bool:
    """Only fire for top-level *.md files inside the memory directory."""
    if not file_path:
        return False
    try:
        from memory_engine import MEMORY_DIR
        target = Path(file_path).resolve()
        mem = MEMORY_DIR.resolve()
    except Exception:
        return False
    if not str(target).startswith(str(mem)):
        return False
    if target.parent != mem:
        return False
    if target.name == "MEMORY.md":
        return False
    if target.suffix != ".md":
        return False
    return True


def _process(file_path: str) -> None:
    """Process a single newly-written memory file. Best-effort, swallows errors."""
    try:
        from memory_engine import load_memory, list_memories
        from kg import upsert_entity, upsert_relationship, add_mention
        from kg_bootstrap import SEEDS, VERB_PATTERNS
    except Exception:
        return

    p = Path(file_path)
    if not p.exists():
        return

    try:
        m = load_memory(p)
    except Exception:
        return

    # 1. Register the memory itself as a concept node so related: edges land.
    try:
        upsert_entity(m.name or m.filename, "concept", confidence=0.9)
    except Exception:
        pass

    # 2. Mention detection against curated seeds.
    try:
        import re
        body_lower = m.body.lower()
        lookup: dict[str, str] = {}
        for name, _etype, aliases in SEEDS:
            lookup[name.lower()] = name
            for a in aliases:
                lookup[a.lower()] = name
        seen: set[str] = set()
        for key in sorted(lookup.keys(), key=len, reverse=True):
            if len(key) < 3:
                continue
            if re.search(rf"\b{re.escape(key)}\b", body_lower):
                canonical = lookup[key]
                if canonical in seen:
                    continue
                seen.add(canonical)
                excerpt = ""
                for line in m.body.splitlines():
                    if key in line.lower():
                        excerpt = line.strip()[:200]
                        break
                add_mention(canonical, m.filename, excerpt)
    except Exception:
        pass

    # 3. Frontmatter related: links.
    try:
        if m.related:
            mems = list_memories()
            name_by_filename = {mm.filename: mm.name for mm in mems}
            for r in m.related:
                r = r.strip()
                if not r:
                    continue
                other = name_by_filename.get(r)
                if not other:
                    continue
                upsert_entity(other, "concept", confidence=0.9)
                upsert_relationship(
                    m.name or m.filename, other, "related_to",
                    from_type="concept", to_type="concept",
                    source_memory=m.filename,
                )
    except Exception:
        pass

    # 4. Verb-pattern extraction (curated, low-recall, high-precision).
    try:
        import re
        for pattern, rel_type in VERB_PATTERNS:
            for match in re.finditer(pattern, m.body):
                target = match.group(1).strip()
                if len(target) < 3 or len(target) > 50:
                    continue
                upsert_relationship(
                    _paths.USER_NAME, target, rel_type,
                    from_type="person", to_type="concept",
                    confidence=0.6, source_memory=m.filename,
                )
    except Exception:
        pass


def main():
    payload = _read_stdin_json()
    if not payload:
        return
    inp = payload.get("tool_input") or payload.get("toolInput") or {}
    file_path = inp.get("file_path") or inp.get("filePath") or ""
    if not _is_memory_path(file_path):
        return
    # v14.1: emit event for the outbox (durable replay log) but DON'T call
    # _process() directly — the outbox worker will dispatch the event and
    # run _process() exactly once. Eliminates the double-processing bug
    # where every memory write ran entity extraction twice.
    try:
        import event_bus
        event_bus.emit_event("memory_write", {"file_path": str(file_path)})
        # v14.1: enqueue a reindex job so the changed memory becomes
        # vector-recallable within one tool tick. Rides the durable, fail-soft
        # outbox; idempotent downstream (content-hash + INSERT OR REPLACE).
        event_bus.emit_event("reindex", {"file_path": str(file_path)})
        # v3.2 Phase 3: also emit importance_score event for the same write.
        # Gated by feature flag so we don't burn LLM tokens until ready.
        try:
            from feature_flags import is_enabled
            if is_enabled("importance_scoring_enabled"):
                event_bus.emit_event("importance_score", {"file_path": str(file_path)})
        except Exception:
            pass  # importance is opt-in; never break entity extraction
    except Exception:
        # If event_bus fails, fall back to direct processing so we never
        # silently drop entity extraction on a memory write.
        _process(file_path)


if __name__ == "__main__":
    main()
