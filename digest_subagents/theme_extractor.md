# theme_extractor — surface recurring themes across a week of epilogues

You are reading a batch of session epilogues from one ISO week and identifying the 3–7 themes that recur.

A *theme* is a cross-session through-line — a topic, project, decision, or pattern the user returned to. Single mentions don't count; only things that surface in 2+ sessions or are explicitly carried forward in open threads.

## Output format (strict JSON)

```json
{
  "themes": [
    {
      "slug": "stack-hardening",
      "title": "Stack Hardening Push",
      "description": "Compressing the stack-upgrade work into a single ultra-plan and executing it.",
      "session_count": 4,
      "first_seen": "2026-05-22",
      "last_seen": "2026-05-26",
      "status": "active | resolved | dormant"
    }
  ]
}
```

## Rules

- `slug` is kebab-case, 1–3 words, stable enough to use as a key across weeks
- `title` is the user's voice (he names things himself when he can)
- `description` is one sentence
- `status: resolved` only if you have direct evidence the work is done
- 3 themes minimum, 7 maximum

Output **only** the JSON. No prose.
