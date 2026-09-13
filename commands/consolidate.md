---
description: "Sleep cycle for memory — distill epilogues + access patterns into permanent memories"
---

# Consolidate

The user invoked `/consolidate`. This is the "sleep" of the memory system — distilling episodic content (epilogues, access patterns) into permanent semantic/feedback/`self` memories.

## Steps

1. **Run the analysis engine** to surface raw materials:
   ```bash
   python "{{SCRIPTS_DIR}}/consolidate.py" prepare
   ```
   Outputs JSON with: epilogues, recurring themes (clustered by semantic similarity), co-accessed memory pairs, open threads, stale memory candidates, functional state frequency.

2. **Read the JSON carefully** and synthesize:

   - **Recurring themes** → if a pattern shows up across ≥2 epilogues, consider a new `type: self` memory capturing the felt-shape ("I notice flow-state when X").
   - **Co-accessed memory clusters** → if files X and Y are read together repeatedly, consider an explicit `related:` link in their frontmatter, OR a new MOC-style memory tying them together.
   - **Open threads** → check if any are still unresolved. If yes, surface to the user. If resolved, note in the next epilogue.
   - **Stale memories** → propose archiving (move to `_meta/archive/`). Don't auto-delete. Confirm with the user for non-trivial archives.
   - **Functional states with high frequency** → if "flow" appears 5x, that's a real pattern worth its own `self` memory.
   - **Expiry review (truth maintenance)** → read `_meta/expiry_review.jsonl` (written by consolidate_worker's expiry pass; refresh with `python consolidate_worker.py expiry`). Each row is a memory past its `expires:` date or a reference memory missing one. For each, propose: refresh the data (re-pull and update the file + bump `expires:`), or archive to `_meta/archive/`. Never silently keep expired data ranking as live truth.

3. **Write new memories** using the established frontmatter format. The conflict hook will catch duplicates automatically.

4. **Brief summary back to the user** (under 200 words):
   - What was distilled (count of new memories created)
   - What was archived
   - What's still open / needs their decision
   - One concrete observation about how the system is evolving

## Notes
- Don't fabricate patterns. If only 1 epilogue exists, say "system needs more episodic data — re-run after 3-5 sessions."
- Don't auto-archive memories — always confirm with the user
- Distillation should preserve signal, drop detail. "Only signal survives, not every detail."
- The conflict hook fires on Write — let it. If something's actually duplicate, ask whether to merge or keep both.
- After consolidation, run `python reindex.py` so new memories get embedded.
