---
description: "Open threads — persistent unresolved work across sessions (use: /threads [list|add|close|reopen|rm])"
---

# Threads

The user invoked `/threads`. Manage the persistent open-threads list at
`_meta/open_threads.md`.

## Usage

- `/threads`              → list open threads (default)
- `/threads add "<body>"` → append a new open thread
- `/threads close <id>`   → mark a thread closed (keeps history)
- `/threads reopen <id>`  → re-open a closed thread
- `/threads rm <id>`      → delete entirely
- `/threads --all`        → list including closed

## Steps

1. Parse `$ARGUMENTS`. If empty → run list. Otherwise pass through.

2. Run the threads script:
   ```bash
   python "{{SCRIPTS_DIR}}/threads.py" $ARGUMENTS
   ```

3. After any mutation (`add`/`close`/`reopen`/`rm`), follow up with a fresh `list`
   so the user sees the new state.

4. Be brief — these are status outputs, not analysis. If the user asks for context
   on a specific thread, only then dig deeper.

## Notes
- Open threads also surface in the boot ritual at session start.
- Closing a thread in this list does NOT propagate to the related epilogue —
  epilogues are point-in-time snapshots; this list is the source of truth for
  current state.
- If the user says "I finished X" or "X is done", proactively offer to close
  the matching thread without making them type the id.
