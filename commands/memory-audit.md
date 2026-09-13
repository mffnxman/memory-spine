---
description: "Run memory health audit — stale, orphaned, duplicate, expired memories"
---

# Memory Audit

The user invoked `/memory-audit`. Run the health check and surface anything needing attention.

## Steps

1. **Run the audit:**
   ```bash
   python "{{SCRIPTS_DIR}}/audit.py"
   ```
   This auto-opens the HTML triage view.

2. **Brief verbal summary** (under 100 words):
   - Health score
   - Top 1-2 issues to address
   - One concrete next step (e.g. "5 untyped memories — want me to add frontmatter?")

3. **If `--prune` was passed**, offer to:
   - Add frontmatter to untyped memories
   - Remove or merge near-duplicates
   - Add to MEMORY.md the unindexed files

## Notes
- Don't auto-delete anything — always confirm with the user first
- Stale ≠ bad — some memories are evergreen (values, working agreements, etc.)
