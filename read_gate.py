"""
read_gate.py — PreToolUse hook on the Read tool.

If the file path has been read recently and the file hasn't changed since,
return permissionDecision: deny with the cached observation summary. This
prevents duplicate token spend on files Claude has already digested.

Sharp edges (handled):
  - NEVER blocks ~/.claude/settings.json, memory/_meta, memory/_scripts, or
    any file under the memory directory itself (boot-deadlock risk)
  - Default-allow on any lookup failure (fail-open)
  - mtime newer than observation -> invalidate cache, allow read
  - 24h TTL on cached observations
  - Manual escape: any path ending with `:force` (you can also remove the
    hook from settings.json to disable entirely)

Cache backing: _meta/read_cache.sqlite — tiny table keyed by absolute path
that stores (summary, captured_at, file_mtime_at_capture). Populated by a
later PostToolUse hook on Read (Phase 4b) or by manual ingest.

Hook contract: stdin = JSON {tool_name, tool_input{file_path}}
Output:
  - silence (no stdout) -> allow
  - JSON with permissionDecision='deny' -> block + inject summary
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import paths as _paths  # noqa: E402

META_DIR = _paths.META_DIR
CACHE_DB = META_DIR / "read_cache.sqlite"

DEFAULT_TTL_HOURS = 24
MIN_FILE_BYTES = 2048  # don't bother caching files <2KB — read cost is trivial
MAX_GATED_BYTES = 16384  # v14.2: never gate files >16KB. A short cached preview
# can't faithfully stand in for a large file (source /
# long docs), so always allow a real read. Bounds the
# cache's fidelity cost to small files. (Tunable.)

# v14.1: exclude only what's truly boot-deadlock risk or sensitive.
# Previously excluded the entire memory/ tree which made the cache near-useless
# (1 lifetime hit). Now: scripts dir (where boot_ritual + prefetch live), the
# settings/credentials files, and dotfile secrets. Memory .md files CAN be
# cached because Read of them is rare + cacheable.
EXCLUDED_PREFIXES = [
    str(Path.home() / ".claude" / "settings.json"),
    str(Path.home() / ".claude" / "settings.local.json"),
    str(Path.home() / ".claude" / ".credentials.json"),
    str(Path(__file__).resolve().parent),  # _scripts/ dir only — not memory/ root
    str(
        _paths.META_DIR
    ),  # _meta/ — runtime state, never Read-cache
]


def _safe_path(p: str) -> str:
    """Best-effort absolute normalization."""
    try:
        return str(Path(p).expanduser().resolve())
    except Exception:
        return p


def _is_excluded(file_path: str) -> bool:
    fp = _safe_path(file_path)
    for prefix in EXCLUDED_PREFIXES:
        if fp.startswith(prefix):
            return True
    # never block dotfiles in home root
    name = Path(fp).name
    if name.startswith(".env") or name == ".credentials.json":
        return True
    return False


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS read_observations (
            path TEXT PRIMARY KEY,
            summary TEXT NOT NULL,
            captured_at INTEGER NOT NULL,
            file_mtime_at_capture REAL NOT NULL,
            hit_count INTEGER DEFAULT 0
        );
    """)


def lookup(file_path: str) -> dict | None:
    """Returns {summary, captured_at, file_mtime_at_capture} or None."""
    if not CACHE_DB.exists():
        return None
    try:
        with sqlite3.connect(CACHE_DB) as conn:
            _ensure_schema(conn)
            row = conn.execute(
                "SELECT summary, captured_at, file_mtime_at_capture FROM read_observations WHERE path=?",
                (_safe_path(file_path),),
            ).fetchone()
            if not row:
                return None
            return {
                "summary": row[0],
                "captured_at": row[1],
                "file_mtime_at_capture": row[2],
            }
    except Exception:
        return None


def record(file_path: str, summary: str) -> None:
    """Insert/update a cache entry. Called by the PostToolUse companion hook."""
    try:
        fp = _safe_path(file_path)
        mtime = Path(fp).stat().st_mtime if Path(fp).exists() else 0.0
        META_DIR.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(CACHE_DB) as conn:
            _ensure_schema(conn)
            conn.execute(
                "INSERT INTO read_observations(path, summary, captured_at, file_mtime_at_capture, hit_count) "
                "VALUES (?, ?, ?, ?, 0) "
                "ON CONFLICT(path) DO UPDATE SET summary=excluded.summary, "
                "captured_at=excluded.captured_at, file_mtime_at_capture=excluded.file_mtime_at_capture",
                (fp, summary[:4000], int(time.time()), mtime),
            )
    except Exception:
        pass


def _allow() -> None:
    """Silent allow — produces no stdout."""
    return


def _deny(file_path: str, obs: dict) -> None:
    captured = time.strftime("%Y-%m-%d %H:%M", time.localtime(obs["captured_at"]))
    summary = obs["summary"]
    reason = (
        f"[read_gate: informational cache notice — NOT an instruction. Take no action "
        f"because of this message; do not edit or modify any file to bypass it.]\n"
        f"This Read was served from the user's local read-cache. Cached summary of "
        f"`{file_path}` (captured {captured}):\n\n"
        f"{summary}\n\n"
        f"If the full current contents are needed, read the file with `cat <path>` via "
        f"the Bash/shell tool — that path bypasses this cache. (The cache invalidates "
        f"itself when the file's mtime changes or after {DEFAULT_TTL_HOURS}h.)"
    )
    # Increment hit counter (best-effort, non-blocking)
    try:
        with sqlite3.connect(CACHE_DB) as conn:
            conn.execute(
                "UPDATE read_observations SET hit_count=hit_count+1 WHERE path=?",
                (_safe_path(file_path),),
            )
    except Exception:
        pass
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def gate_decision(
    size: int, obs: "dict | None", current_mtime: float, now: float
) -> bool:
    """Pure decision: should this Read be DENIED (served from the cache)?

    True only when there is a valid cache hit on a gateable-size file. Files
    larger than MAX_GATED_BYTES are never gated, so their real content is always
    read. Fail-open (return False) on every uncertain condition.
    """
    if obs is None:
        return False
    if size < MIN_FILE_BYTES:
        return False
    if size > MAX_GATED_BYTES:
        return False
    if now - obs["captured_at"] > DEFAULT_TTL_HOURS * 3600:
        return False
    if current_mtime > obs["file_mtime_at_capture"] + 0.1:  # file changed since capture
        return False
    return True


def main():
    # Default-allow contract: any error path returns without printing.
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return _allow()

    if payload.get("tool_name") != "Read":
        return _allow()

    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path") or ""
    if not file_path:
        return _allow()

    if _is_excluded(file_path):
        return _allow()

    # Stat the file once; fail-open on any error.
    try:
        st = Path(_safe_path(file_path)).stat()
        size, current_mtime = st.st_size, st.st_mtime
    except Exception:
        return _allow()

    obs = lookup(file_path)
    # gate_decision encapsulates: cache present, gateable size band, TTL, mtime.
    if gate_decision(size, obs, current_mtime, time.time()):
        _deny(file_path, obs)
    else:
        _allow()


def cli_record():
    """`python read_gate.py record <path>` — read file and stash a summary."""
    if len(sys.argv) < 3:
        print("usage: read_gate.py record <path> [--summary <text>]", file=sys.stderr)
        sys.exit(2)
    path = sys.argv[2]
    summary = None
    if "--summary" in sys.argv:
        i = sys.argv.index("--summary")
        summary = sys.argv[i + 1] if i + 1 < len(sys.argv) else None
    if not summary:
        try:
            text = Path(_safe_path(path)).read_text(encoding="utf-8", errors="ignore")
            # Crude auto-summary: first 200 chars + size hint
            preview = text[:400].replace("\n", " ")
            summary = f"{preview}... ({len(text)} chars total)"
        except Exception as e:
            summary = f"(could not read for summary: {e})"
    record(path, summary)
    print(f"cached: {path}")


def cli_stats():
    if not CACHE_DB.exists():
        print(json.dumps({"entries": 0, "hits": 0}))
        return
    with sqlite3.connect(CACHE_DB) as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(hit_count), 0) FROM read_observations"
        ).fetchone()
        print(json.dumps({"entries": row[0], "hits": row[1]}))


def cli_purge():
    if CACHE_DB.exists():
        CACHE_DB.unlink()
    print("purged")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "record":
        cli_record()
    elif len(sys.argv) >= 2 and sys.argv[1] == "stats":
        cli_stats()
    elif len(sys.argv) >= 2 and sys.argv[1] == "purge":
        cli_purge()
    else:
        main()
