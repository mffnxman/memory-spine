---
description: "Unified memory search — memories + Obsidian vault + observer (use: /recall <topic>)"
---

# Recall

The user invoked `/recall <query>`. Unified search across the memory layers.

## Steps

1. **Run the recall script** for memories + vault (if `OBSIDIAN_VAULT` is set) + observer:
   ```bash
   python "{{SCRIPTS_DIR}}/recall.py" "$ARGUMENTS"
   ```

2. **Synthesize the merged result** for the user:
   - Lead with the highest-signal hit (whichever layer it came from)
   - Group by layer: memories (curated), vault (notes), observer (live activity)
   - If two layers agree → high confidence; if they conflict → flag it
   - Note the file path so they can dig deeper if needed

3. **If `--html` was passed**, also generate the HTML view:
   ```bash
   python "{{SCRIPTS_DIR}}/recall.py" "$ARGUMENTS" --html
   ```

4. **If `--observer` was passed**, only show observer hits (skip memories + vault).

5. **If `--deep` was passed**, show full body of top memory hits.

## Notes
- Don't truncate aggressively — the user absorbs context fast
- If a memory is stale (>60d) flag it with a quick "(memory from N days ago, may need refresh)" note
- This is a thinking aid, not a final answer — show your work
- The observer layer is FTS5 search across observations.db — it captures activity that was never curated into a memory
