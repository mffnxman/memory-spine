---
description: "Reconcile memories with an Obsidian vault — drift report (requires OBSIDIAN_VAULT)"
---

# Memory Sync

The user invoked `/memory-sync`. Run the bidirectional drift report between the memory files and the Obsidian vault named by `OBSIDIAN_VAULT`.

## Steps

1. **Run sync report** with HTML output:
   ```bash
   python "{{SCRIPTS_DIR}}/obsidian_sync.py" --html
   ```
   If `OBSIDIAN_VAULT` is unset the script reports that no vault is configured; say so and stop.

2. **Brief verbal summary** (under 100 words):
   - How many memory-only topics
   - How many vault-only topics
   - Top 1-2 candidates for sync (memory facts the vault doesn't reflect, or vault facts that should be saved as memory)

3. **Offer** to:
   - Save vault-only topics as new memory files (with frontmatter)
   - Update the vault's summary note with key memory-only facts
   - Skip — many "drift" entries are benign (e.g. the vault has tool names that aren't personal memories)

## Notes
- This is REPORT-ONLY by design. Never auto-merge — too risky.
- Many "vault-only" slugs are tool/skill names — don't propose those as memory candidates unless they have personal context attached.
