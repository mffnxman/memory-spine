"""paths.py — single source of truth for where the memory lives.

Every script in this directory derives its paths from here instead of
computing `Path(__file__).resolve().parent.parent` on its own.

Resolution order for the memory directory:

  1. `MEMORY_HOME` environment variable, if set (absolute or ~-expanded).
  2. The parent of this `_scripts` directory — the default layout is

         <memory>/
         ├── MEMORY.md          index, loaded into context every session
         ├── *.md               the memories themselves
         ├── _meta/             sqlite dbs, epilogues, logs, flags (never committed)
         └── _scripts/          this repo, cloned or symlinked here

Optional integrations are also configured through the environment so the
engine never carries a path that belongs to one machine:

  OBSIDIAN_VAULT      root of an Obsidian vault to grep alongside memories
                      (recall / brain_viz / obsidian_sync). Unset = disabled.
  MEMORY_BACKUP_DIR   where db_snapshot.py writes sqlite snapshots and where
                      health_sentinel looks for dated backup generations.
  MEMORY_USER_NAME    how the user is labelled in session logs, epilogues and
                      knowledge-graph seeds (default "User").
  MEMORY_OUTPUT_DIR   where HTML reports (recall, audit, whoami, graph) are
                      written and auto-opened (default ~/Downloads).
  MEMORY_DB_PATH      override for _meta/memory.db (the test suite uses it
                      to point at an isolated copy).
"""

from __future__ import annotations

import os
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent


def _memory_home() -> Path:
    env = os.environ.get("MEMORY_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return SCRIPTS_DIR.parent


MEMORY_DIR = _memory_home()
META_DIR = MEMORY_DIR / "_meta"
INDEX_FILE = MEMORY_DIR / "MEMORY.md"
EPILOGUE_DIR = META_DIR / "epilogues"
FLAGS_PATH = META_DIR / "feature_flags.json"

# The Claude Code project slug (e.g. `C--Users-alex`) is the name of the
# directory that contains `memory/`. Hooks use it to filter file paths.
PROJECT_SLUG = MEMORY_DIR.parent.name


def _opt_dir(var: str) -> Path | None:
    v = os.environ.get(var, "").strip()
    return Path(v).expanduser() if v else None


OBSIDIAN_VAULT = _opt_dir("OBSIDIAN_VAULT")
BACKUP_DIR = _opt_dir("MEMORY_BACKUP_DIR")
OUTPUT_DIR = _opt_dir("MEMORY_OUTPUT_DIR") or (Path.home() / "Downloads")
USER_NAME = os.environ.get("MEMORY_USER_NAME", "").strip() or "User"


def describe() -> str:
    return (
        f"MEMORY_HOME      = {MEMORY_DIR}\n"
        f"META_DIR         = {META_DIR}\n"
        f"PROJECT_SLUG     = {PROJECT_SLUG}\n"
        f"OBSIDIAN_VAULT   = {OBSIDIAN_VAULT or '(unset)'}\n"
        f"MEMORY_BACKUP_DIR= {BACKUP_DIR or '(unset)'}\n"
        f"MEMORY_OUTPUT_DIR= {OUTPUT_DIR}\n"
        f"MEMORY_USER_NAME = {USER_NAME}"
    )


if __name__ == "__main__":
    print(describe())
