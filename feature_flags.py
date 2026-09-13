"""
feature_flags.py — small flag loader for v3.2 rollout.

Reads _meta/feature_flags.json with defensive defaults. Each phase of the
v3.2 upgrade gates behind a flag so we can ship incrementally and roll back
instantly.

Defaults are conservative: any flag missing from the file is treated as
False. So an empty or missing file means v3.1 behavior — nothing new is on.

Usage:
  from feature_flags import is_enabled
  if is_enabled("rerank_enabled"):
      ...

For tests / scripts, you can also force a flag:
  os.environ["MEMORY_FLAG_RERANK_ENABLED"] = "1"  # override
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
import sys as _sys  # noqa: E402
_sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths as _paths  # noqa: E402

MEMORY_DIR = _paths.MEMORY_DIR
FLAGS_PATH = MEMORY_DIR / "_meta" / "feature_flags.json"

_cache: dict | None = None
_cache_loaded_at: float = 0
_CACHE_TTL_SEC = 5  # re-read flags every 5s; cheap enough


def _load() -> dict:
    """Read flags from disk. Returns empty dict on any failure."""
    if not FLAGS_PATH.exists():
        return {}
    try:
        with FLAGS_PATH.open("r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _get_flags() -> dict:
    global _cache, _cache_loaded_at
    now = time.time()
    if _cache is None or (now - _cache_loaded_at) > _CACHE_TTL_SEC:
        _cache = _load()
        _cache_loaded_at = now
    return _cache


def is_enabled(name: str) -> bool:
    """Returns True iff the flag is explicitly set truthy. Env override wins.

    Env var format: MEMORY_FLAG_<NAME_UPPERCASE>  (e.g. MEMORY_FLAG_RERANK_ENABLED).
    Any non-empty, non-"0" value enables.
    """
    env_key = "MEMORY_FLAG_" + name.upper()
    if env_key in os.environ:
        v = os.environ[env_key].strip()
        return bool(v) and v != "0" and v.lower() not in ("false", "no", "off")

    flags = _get_flags()
    val = flags.get(name)
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        return val.lower() in ("true", "1", "yes", "on")
    return False


def all_flags() -> dict:
    """Return a copy of current flag state. For diagnostics."""
    return dict(_get_flags())


def main():
    """CLI: python feature_flags.py [list|set <name> <true|false>]"""
    import sys
    if len(sys.argv) < 2 or sys.argv[1] == "list":
        flags = _get_flags()
        if not flags:
            print(f"No flags set. ({FLAGS_PATH} doesn't exist or is empty)")
            return
        print(f"Flags from {FLAGS_PATH}:")
        for k, v in sorted(flags.items()):
            print(f"  {k}: {v}")
        return

    if sys.argv[1] == "set" and len(sys.argv) >= 4:
        name = sys.argv[2]
        raw = sys.argv[3].lower()
        val = raw in ("true", "1", "yes", "on")
        flags = _load()
        flags[name] = val
        FLAGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with FLAGS_PATH.open("w", encoding="utf-8") as f:
            json.dump(flags, f, indent=2, sort_keys=True)
        print(f"Set {name} = {val}")
        return

    print("Usage: python feature_flags.py [list | set <name> <true|false>]")


if __name__ == "__main__":
    main()
