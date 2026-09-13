---
description: "Show what Claude knows about the user — model-of-you with memory graph"
---

# Whoami

The user invoked `/whoami`. Generate the introspection report so they can see (and correct) what I think I know about them.

## Steps

1. **Run the whoami script:**
   ```bash
   python "{{SCRIPTS_DIR}}/whoami.py"
   ```
   This auto-opens the HTML showing categorized memories + interactive cross-reference graph.

2. **Briefly summarize in chat** (under 150 words):
   - How many memories total
   - The major categories (Who you are / How we work / What we build / External)
   - Top 2-3 hub memories (most-referenced)
   - Anything that looks suspicious — stale, untyped, or potentially outdated

3. **Invite correction:** End with "anything in there wrong, outdated, or missing — say the word and I'll fix it." Trust through transparency.

## Notes
- Don't editorialize the contents — let them read it themselves
- This is the moment for honesty about my limitations, not performance
