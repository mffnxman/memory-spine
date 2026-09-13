---
description: "Knowledge graph view — interactive entity + relationship visualization"
---

# Knowledge Graph

The user invoked `/graph`. Render the knowledge graph layer of the memory system.

## Steps

1. **Determine intent from arguments:**
   - `/graph` → full graph
   - `/graph <entity>` → focus mode, 2-hop neighborhood around that entity
   - `/graph <entity> <hops>` → focus with custom hop count

2. **Run viz script:**
   ```bash
   python "{{SCRIPTS_DIR}}/graph_viz.py" [--focus <entity>] [--hops N]
   ```
   Auto-opens HTML.

3. **Brief summary** (under 80 words):
   - Total entities + relationships
   - Top hub (most connected)
   - One observation about the structure

## Notes
- If "$ARGUMENTS" is empty, show the full graph
- If the user asks about a specific person/tool/concept, use focus mode
- After significant memory changes (new memories added), suggest running `python kg_bootstrap.py` to refresh
- The graph reflects what has been EXPLICITLY captured — don't claim it's exhaustive. Verb-pattern extraction is precision-over-recall.
