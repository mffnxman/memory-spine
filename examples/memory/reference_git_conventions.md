---
name: Git conventions
description: Commit message format (type: summary under 72 chars, body explains why), branch naming (feat/, fix/, chore/), squash merges to main, never force-push shared branches
type: reference
weight: low
created: 2026-01-25
related: procedural_test_first.md
---

Commit messages: `type: short summary` on the first line, under 72 characters, where type is one of feat, fix, chore, docs, test, refactor. The body explains why the change exists, not what the diff shows.

Branches: `feat/<slug>`, `fix/<slug>`, `chore/<slug>`. One topic per branch. Delete after merge.

Merging: squash merge to main so history reads as one entry per change. Rebase a personal branch freely; never force-push a branch someone else has pulled.

Tags: `vMAJOR.MINOR.PATCH`, created only from main.
