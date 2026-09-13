"""
conflict.py — PreToolUse hook for Write/Edit on memory files.

Reads JSON from stdin (Claude Code hook input). If the target file is
in the auto-memory dir AND would create a near-duplicate of an existing
memory, surfaces the conflict so Claude can decide: update existing,
keep both, or merge.

Output JSON:
  - permissionDecision: "ask" with reason if conflict found
  - silent (no output) if no conflict
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).parent))
from memory_engine import list_memories, tokenize, cosine, MEMORY_DIR

THRESHOLD = 0.55  # similarity floor for "conflict"


def _exact_hash_match(target_name: str, content: str, mems: list) -> tuple[str, str] | None:
    """v13: fast path — exact canonical-hash match before cosine fallback."""
    try:
        from dedup import compute_memory_hash
    except Exception:
        return None
    new_hash = compute_memory_hash(target_name, "", content, "")
    for m in mems:
        existing_hash = compute_memory_hash(m.name or m.filename, m.description, m.body, m.type)
        if existing_hash == new_hash:
            return (m.filename, m.name or m.filename)
    return None

def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return

    tool_input = payload.get("tool_input", {})
    file_path = tool_input.get("file_path", "")
    content   = tool_input.get("content", "") or tool_input.get("new_string", "")

    if not file_path or not content:
        return

    # Only fire if writing into the memory dir (top level), not _meta or _scripts
    target = Path(file_path)
    try:
        target = target.resolve()
        mem_resolved = MEMORY_DIR.resolve()
    except Exception:
        return
    if not str(target).startswith(str(mem_resolved)):
        return
    if target.parent != mem_resolved:  # only top-level memory files
        return
    if target.name == "MEMORY.md":
        return
    if target.suffix != ".md":
        return
    # Skip if this is an update to a file that already exists (Edit tool)
    if target.exists() and "new_string" in tool_input:
        return

    # Compare new content to all existing memories
    new_tokens = Counter(tokenize(content))
    if sum(new_tokens.values()) < 20:
        return  # too short to meaningfully compare

    try:
        mems = list_memories()
    except Exception:
        return

    # v13 fast path: exact canonical-hash match
    exact = _exact_hash_match(target.stem, content, mems)
    if exact:
        fn, name = exact
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": (
                    f"Exact canonical-content match with existing memory: "
                    f"{fn} ({name}). Update the existing file instead, or write a "
                    f"meaningfully distinct memory."
                ),
            }
        }))
        return

    candidates = []
    for m in mems:
        if m.path.resolve() == target:
            continue
        existing = Counter(tokenize(m.name + " " + m.description + " " + m.body))
        sim = cosine(new_tokens, existing)
        if sim >= THRESHOLD:
            candidates.append((m.filename, m.name, sim))

    if not candidates:
        return

    candidates.sort(key=lambda x: -x[2])
    top = candidates[:3]
    reason = "Possible duplicate memory detected:\n" + "\n".join(
        f"  - {fn} ({name}) — similarity {sim:.2f}" for fn, name, sim in top
    ) + "\n\nUpdate existing or proceed with new file?"

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }))

if __name__ == "__main__":
    main()
