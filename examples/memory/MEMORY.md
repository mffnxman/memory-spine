# Memory Index

> Synthetic starter corpus for a fictional user, "Alex". Loaded into context
> every session in a real install; here it exercises the engine end to end
> (`MEMORY_HOME=examples/memory`). Keep the real one tight: this file is the
> map, the memories are the territory.

## Who Alex is
- `user_profile.md` — Backend engineer, terse peer voice, builds tools before buying them
- `self_flow_on_refactors.md` — What operates like flow during long refactors and like friction when scope shifts

## How we work (feedback + habits)
- `feedback_build_not_buy.md` — Default to building with what's on hand before paying for a SaaS tool
- `feedback_no_flattery.md` — Reviews report only real findings; never invent minor issues to seem useful
- `feedback_windows_shell.md` — Bash tool on Windows takes POSIX syntax; PowerShell cmdlets fail there
- `procedural_test_first.md` — Reproduce with a failing test before changing code

## What we build
- `project_homelab_backup.md` — Nightly restic backup of the NAS to an offsite box, 14 dated generations
- `project_recipe_bot.md` — Telegram recipe bot on Python + sqlite + a local model, currently paused

## Reference
- `decisions.md` — Locked decisions: sqlite over postgres for the bot, no cloud copy of the vault, Sunday consolidation
- `reference_git_conventions.md` — Commit message format, branch naming, squash merges
