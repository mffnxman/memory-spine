---
name: Locked decisions
description: Running log of decisions that should not be relitigated: sqlite over postgres for the recipe bot, no cloud copy of the notes vault, memory consolidation runs on Sundays, restic over plain mirroring for backups
type: reference
weight: high
created: 2026-01-30
related: project_recipe_bot.md, project_homelab_backup.md, feedback_build_not_buy.md
---

Decisions Alex has made and does not want reopened unless the facts change. Each has the one-line reason so a future session can tell whether the facts did change.

- **sqlite over postgres for the recipe bot** (2026-03-20). Single user, under a million rows, zero ops. Revisit only if the bot goes multi-user.
- **No cloud copy of the notes vault** (2026-02-01). The vault stays on the NAS and the offsite box. Convenience is not worth a third party holding personal notes.
- **Memory consolidation runs on Sundays** (2026-04-05). Weekly is often enough to distill; daily produced noise and duplicate candidates.
- **restic with dated generations, not a plain mirror** (2026-03-08). A mirror propagates a wipe on the next run. See the backup project note.
- **Build-not-buy is the default, purchase is the exception** (2026-01-18). See the feedback note; listed here because it keeps coming up.
