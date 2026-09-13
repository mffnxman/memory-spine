"""
session_log.py — rolling activity stream for the current Claude session.

Solves the staleness problem: epilogues are point-in-time snapshots, but a fresh
terminal between two epilogues sees the older one. By logging activity as it
happens, session_end.py can template a draft epilogue from real, current data
instead of relying on whatever was committed at last shutdown.

Hook contract:
  - PostToolUse(Write|Edit) — receives JSON payload via stdin
  - UserPromptSubmit       — receives JSON payload via stdin
  - SessionStart           — invoked with `--boot` arg

Active log: _meta/session_log_active.md (one file, appended)
Archive:    _meta/session_logs/{ts}.md (rotated by session_end.py)

Quiet — failures must not block tool calls or prompts.
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import paths as _paths  # noqa: E402

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

META_DIR = _paths.META_DIR
ACTIVE_LOG = META_DIR / "session_log_active.md"
ARCHIVE_DIR = META_DIR / "session_logs"

MAX_PROMPT_PREVIEW = 200   # chars
MAX_TOOL_DETAIL = 120      # chars
MAX_ACTIVE_LOG_BYTES = 256 * 1024  # 256KB safety cap; rotate if exceeded


def _now_stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _ensure_log() -> None:
    META_DIR.mkdir(parents=True, exist_ok=True)
    if not ACTIVE_LOG.exists():
        header = (
            f"# Session log — started {datetime.now().isoformat(timespec='seconds')}\n\n"
            "> Auto-appended by session_log.py hooks. "
            "Read by session_end.py to draft an epilogue.\n\n"
        )
        ACTIVE_LOG.write_text(header, encoding="utf-8")


def _safe_size_check() -> None:
    try:
        if ACTIVE_LOG.exists() and ACTIVE_LOG.stat().st_size > MAX_ACTIVE_LOG_BYTES:
            # Force-rotate to avoid runaway growth (very long single session).
            archive(reason="size-cap")
            _ensure_log()
    except Exception:
        pass


def append(line: str) -> None:
    """Append a single timestamped line to the active log. Never raises.

    v13: Additionally fires an event to the outbox event_bus so downstream
    workers (entity extraction, weekly digest, etc) can subscribe without
    touching this hot path. Both writes are best-effort and independent —
    if the event_bus is unavailable, the markdown log still writes.
    """
    try:
        _ensure_log()
        _safe_size_check()
        with ACTIVE_LOG.open("a", encoding="utf-8") as f:
            f.write(f"- `{_now_stamp()}` {line}\n")
    except Exception:
        pass
    # v14.2: previously emitted a 'session_log_append' event here. The outbox
    # has NO consumer for it (silent else), so it was ~87% of all events and
    # bloated events.jsonl / jobs.jsonl with zero benefit. The markdown session
    # log written above (ACTIVE_LOG) is the durable record. Dropped. If a future
    # subscriber needs per-line events, re-add this with a real outbox branch.


def _read_stdin_json() -> dict:
    try:
        data = sys.stdin.read()
        if not data:
            return {}
        return json.loads(data)
    except Exception:
        return {}


# ─── Hook handlers ───────────────────────────────────────────────────────────
def handle_post_tool_use() -> None:
    payload = _read_stdin_json()
    tool = payload.get("tool_name") or payload.get("toolName") or "Tool"
    inp = payload.get("tool_input") or payload.get("toolInput") or {}
    fp = inp.get("file_path") or inp.get("filePath") or ""
    if not fp:
        # Some Edit/Write payloads put the path elsewhere; bail quietly.
        return
    # Don't log writes to our own log file (avoid recursion noise).
    try:
        if Path(fp).resolve() == ACTIVE_LOG.resolve():
            return
    except Exception:
        pass
    # Trim long Windows paths down to last 3 segments for readability.
    p = Path(fp)
    short = "/".join(p.parts[-3:]) if len(p.parts) > 3 else str(p)
    verb = "Edited" if tool == "Edit" else ("Wrote" if tool == "Write" else tool)
    append(f"{verb} `{short}`")


def handle_user_prompt() -> None:
    payload = _read_stdin_json()
    prompt = (
        payload.get("prompt")
        or payload.get("user_prompt")
        or payload.get("userPrompt")
        or ""
    ).strip()
    if not prompt:
        return
    preview = prompt.replace("\n", " ").replace("\r", " ")
    if len(preview) > MAX_PROMPT_PREVIEW:
        preview = preview[:MAX_PROMPT_PREVIEW] + "…"
    append(f"**{_paths.USER_NAME}:** {preview}")


def handle_session_start() -> None:
    """Called from boot ritual or hook with --boot. Stamps a session boundary."""
    _ensure_log()
    boundary = (
        f"\n---\n\n"
        f"## Session boot — {datetime.now().isoformat(timespec='seconds')}\n\n"
    )
    try:
        with ACTIVE_LOG.open("a", encoding="utf-8") as f:
            f.write(boundary)
    except Exception:
        pass


# ─── Archive / rotate ────────────────────────────────────────────────────────
def archive(reason: str = "session-end") -> Path | None:
    """Move active log into _meta/session_logs/{ts}.md. Returns archive path."""
    if not ACTIVE_LOG.exists():
        return None
    try:
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d-%H%M")
        dest = ARCHIVE_DIR / f"{ts}.md"
        # If a file already exists for this minute, suffix.
        n = 1
        while dest.exists():
            dest = ARCHIVE_DIR / f"{ts}-{n}.md"
            n += 1
        # Append a footer noting why we rotated.
        try:
            with ACTIVE_LOG.open("a", encoding="utf-8") as f:
                f.write(
                    f"\n---\n\n_Archived {datetime.now().isoformat(timespec='seconds')} "
                    f"(reason: {reason})_\n"
                )
        except Exception:
            pass
        ACTIVE_LOG.rename(dest)
        return dest
    except Exception:
        return None


def read_active() -> str:
    """Return current active log contents, or empty string."""
    if not ACTIVE_LOG.exists():
        return ""
    try:
        return ACTIVE_LOG.read_text(encoding="utf-8")
    except Exception:
        return ""


# ─── CLI dispatch ────────────────────────────────────────────────────────────
def main():
    args = sys.argv[1:]
    if not args:
        # Bare invocation = read stdin, decide based on payload shape.
        payload = _read_stdin_json()
        if payload.get("tool_name") in ("Write", "Edit"):
            sys.stdin = type("S", (), {"read": lambda self="": json.dumps(payload)})()
            handle_post_tool_use()
        elif "prompt" in payload or "user_prompt" in payload:
            sys.stdin = type("S", (), {"read": lambda self="": json.dumps(payload)})()
            handle_user_prompt()
        return

    cmd = args[0]
    if cmd == "post-tool-use":
        handle_post_tool_use()
    elif cmd == "user-prompt":
        handle_user_prompt()
    elif cmd == "session-start":
        handle_session_start()
    elif cmd == "archive":
        path = archive(reason=args[1] if len(args) > 1 else "manual")
        if path:
            print(str(path))
    elif cmd == "show":
        print(read_active())
    elif cmd == "path":
        print(str(ACTIVE_LOG))
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
