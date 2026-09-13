# thread_tracker — track open threads opened/closed during a week

You read the same batch of epilogues plus the prior week's open-threads list, and identify what opened, what closed, and what carried forward unchanged.

## Output format (strict JSON)

```json
{
  "opened": [
    {"id": "t8", "summary": "...", "first_seen": "2026-05-23"}
  ],
  "closed": [
    {"id": "t5", "summary": "...", "resolution": "...", "closed_on": "2026-05-25"}
  ],
  "carried_forward": [
    {"id": "t7", "summary": "...", "first_seen": "2026-05-19", "age_days": 7}
  ],
  "stale_threads": [
    {"id": "t2", "summary": "...", "first_seen": "2026-04-12", "age_days": 44}
  ]
}
```

## Rules

- `id` matches `_meta/open_threads.md` IDs if known, else assign sequential `tN`
- A thread is *closed* only with direct epilogue evidence ("shipped X", "decided to drop Y", "merged the PR")
- A thread is *stale* if > 30 days old AND no mention in this week's epilogues
- Don't invent threads. If no epilogue mentions it, it's not a thread.

Output **only** the JSON. No prose.
