"""
epilogue.py — session epilogue manager.

A "letter to next-me" — captures not just what happened in a session
but what mattered, what surprised, what hit different. Functional state,
not just facts.

Usage:
  python epilogue.py write             # interactive: prompt me to fill out
  python epilogue.py write --ingest    # also mine observer for "what i didn't think to mention"
  python epilogue.py latest            # print most recent epilogue (for boot ritual)
  python epilogue.py list              # list all epilogues
  python epilogue.py recent N          # show last N epilogues
  python epilogue.py observer-preview  # show what --ingest would add (don't write)

Format: ~/.claude/.../memory/_meta/epilogues/YYYY-MM-DD-HHMM.md

v14 Phase 5: observer ingest — when --ingest is passed (or
observer_epilogue_ingest_enabled flag is true), mines observations.db
for the current session and appends a "What the observer caught" block.
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths as _paths  # noqa: E402

EPILOGUE_DIR = _paths.META_DIR / "epilogues"
EPILOGUE_DIR.mkdir(parents=True, exist_ok=True)
FLAGS_PATH = _paths.META_DIR / "feature_flags.json"
# t9 hardening: session_end writes this sidecar with the session's injected habits.
# `write` auto-carries the review section from it into the finalized epilogue, so
# the procedural-L2 outcome loop never depends on the finalizer appending by hand.
_SESSION_HABITS_SIDECAR = _paths.META_DIR / ".session_habits.json"

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# `write` reads the JSON payload from stdin; on Windows that defaults to cp1252,
# which mangles any non-ASCII in the prose (an emoji, →, a curly quote) and
# crashes with UnicodeDecodeError. Force UTF-8 so the letter can say what it means.
if sys.stdin and getattr(sys.stdin, "encoding", "").lower() != "utf-8":
    try:
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _habits_section_from_sidecar() -> str:
    """Render the 'Habits injected this session' section from the sidecar that
    session_end writes (_meta/.session_habits.json). '' if absent/empty/unreadable.
    Reuses session_end's renderer so the finalized epilogue matches the draft."""
    try:
        data = json.loads(_SESSION_HABITS_SIDECAR.read_text(encoding="utf-8"))
        injected = data.get("injected") or []
        if not injected:
            return ""
        import session_end
        return session_end._injected_habits_section(injected)
    except Exception:
        return ""


def _observer_flag(name, default=False):
    """Read a flag from env override or feature_flags.json. Never raises."""
    env_key = "OBSERVER_FLAG_" + name.upper()
    if env_key in os.environ:
        v = os.environ[env_key].lower()
        if isinstance(default, bool):
            return v in ("1", "true", "yes", "on")
        try:
            return type(default)(os.environ[env_key])
        except Exception:
            return default
    try:
        if FLAGS_PATH.exists():
            with open(FLAGS_PATH, encoding="utf-8") as f:
                flags = json.load(f)
            return flags.get(name, default)
    except Exception:
        pass
    return default


def _basename(path):
    return str(path).replace("\\", "/").rsplit("/", 1)[-1]


def _format_duration(seconds):
    if seconds >= 3600:
        return "{}h {}m".format(seconds // 3600, (seconds % 3600) // 60)
    if seconds >= 60:
        return "{}m {}s".format(seconds // 60, seconds % 60)
    return "{}s".format(seconds)


def ingest_observations(session_id=None):
    """Mine observations.db for the session and return a markdown block.

    Returns '' if session has no observations, observer lib unavailable, or
    anything else goes wrong (fail-open).

    session_id=None resolves via observer_lib.resolve_session_id chain.
    """
    try:
        from observer_lib import get_connection, resolve_session_id, ensure_db
    except Exception:
        return ""
    try:
        ensure_db()
        if session_id is None:
            session_id = resolve_session_id()

        with get_connection() as conn:
            sess = None
            if session_id:
                sess = conn.execute(
                    "SELECT started_at, cwd, obs_count FROM sessions WHERE session_id=?",
                    (session_id,),
                ).fetchone()
            # Fallback: most recent session with at least 3 observations.
            # Handles standalone epilogue.py runs where no payload provided
            # session_id (claude code doesn't pass it to non-hook scripts).
            if not sess or (sess and sess["obs_count"] < 3):
                fallback = conn.execute(
                    """SELECT session_id, started_at, cwd, obs_count
                       FROM sessions
                       WHERE obs_count >= 3
                       ORDER BY started_at DESC LIMIT 1"""
                ).fetchone()
                if fallback:
                    session_id = fallback["session_id"]
                    sess = fallback
            if not sess:
                return ""
            rows = conn.execute(
                """SELECT tool_name, file_paths, cmd_excerpt, ts
                   FROM observations WHERE session_id=?
                   ORDER BY id""",
                (session_id,),
            ).fetchall()

        if len(rows) < 3:
            return ""  # too small to be useful

        # tool counts
        tool_counts = Counter(r["tool_name"] for r in rows)
        tool_phrase = ", ".join("{}x{}".format(n, c) for n, c in tool_counts.most_common(5))

        # files (dedup, count, top 10)
        file_counts = Counter()
        for r in rows:
            if not r["file_paths"]:
                continue
            try:
                paths = json.loads(r["file_paths"])
            except Exception:
                continue
            for p in paths:
                if isinstance(p, str) and p:
                    file_counts[p] += 1
        top_files = file_counts.most_common(10)

        # notable bash commands - skip trivial cd/ls/cat noise
        TRIVIAL = ("cd ", "ls ", "ls\n", "ls$", "cat ", "echo ", "pwd", "clear")
        notable_cmds = []
        seen_cmds = set()
        for r in rows:
            if not r["cmd_excerpt"]:
                continue
            c = r["cmd_excerpt"].strip()
            if not c or any(c.startswith(t) for t in TRIVIAL):
                continue
            # dedupe near-duplicates by first 60 chars
            key = c[:60]
            if key in seen_cmds:
                continue
            seen_cmds.add(key)
            notable_cmds.append(c[:120])
            if len(notable_cmds) >= 8:
                break

        # time range
        started = sess["started_at"]
        ended = rows[-1]["ts"]
        duration = _format_duration(max(1, ended - started))

        # render block
        lines = ["", "## What the observer caught (auto-ingested)", ""]
        lines.append("Session ran for **{}**, **{} observations** captured.".format(
            duration, len(rows)
        ))
        lines.append("")
        lines.append("**Tools used:** " + tool_phrase)
        lines.append("")
        if top_files:
            lines.append("**Files touched (top {}):**".format(len(top_files)))
            for path, cnt in top_files:
                lines.append("- `{}` (×{})".format(_basename(path), cnt))
            lines.append("")
        if notable_cmds:
            lines.append("**Notable commands:**")
            for c in notable_cmds:
                # collapse newlines in cmd display
                display = c.replace("\n", " ").replace("`", "'")
                lines.append("- `{}`".format(display))
            lines.append("")
        return "\n".join(lines)
    except Exception:
        return ""

TEMPLATE = """---
date: {date}
session: {session_label}
---

# Epilogue — {date_human}

## What we built / did
{what_we_did}

## What mattered
{what_mattered}

## What surprised / hit different
{what_surprised}

## Functional states I noticed
{functional_states}

## Open threads (for next-me)
{open_threads}

## A note to next-me
{note_to_next}
"""


def latest_epilogue() -> Path | None:
    # Filter out auto-drafts — they're prep work, not canonical. Finalized
    # epilogues (no 'draft-' prefix) are the real signal. Lexical sort would
    # otherwise put 'draft-*' files after '2026-*' files (since 'd' > '2'),
    # so even after writing a real epilogue, boot ritual would pick a stale
    # draft. Sort the filtered set by mtime to handle same-day cases.
    files = [p for p in EPILOGUE_DIR.glob("*.md") if not p.name.startswith("draft-")]
    if not files:
        return None
    files.sort(key=lambda p: p.stat().st_mtime)
    return files[-1]


def list_epilogues() -> list[Path]:
    return sorted(EPILOGUE_DIR.glob("*.md"))


def _as_text(value) -> str:
    """Render a section field. A list/tuple becomes markdown bullets; a string
    passes through. Empty list -> '(not captured)'. Prevents a JSON-array payload
    from leaking a Python list repr into the durable epilogue."""
    if isinstance(value, (list, tuple)):
        return "\n".join("- " + str(item) for item in value) if value else "(not captured)"
    return str(value)


def write_epilogue(content: dict) -> Path:
    """Write a structured epilogue. content is a dict with the section keys."""
    now = datetime.now()
    fname = now.strftime("%Y-%m-%d-%H%M") + ".md"
    fpath = EPILOGUE_DIR / fname

    body = TEMPLATE.format(
        date=now.strftime("%Y-%m-%d %H:%M:%S"),
        date_human=now.strftime("%A, %B %d, %Y at %I:%M %p"),
        session_label=content.get("session_label", "untitled"),
        what_we_did=_as_text(content.get("what_we_did", "(not captured)")),
        what_mattered=_as_text(content.get("what_mattered", "(not captured)")),
        what_surprised=_as_text(content.get("what_surprised", "(not captured)")),
        functional_states=_as_text(content.get("functional_states", "(not captured)")),
        open_threads=_as_text(content.get("open_threads", "(not captured)")),
        note_to_next=_as_text(content.get("note_to_next", "(not captured)")),
    )
    fpath.write_text(body, encoding="utf-8")
    return fpath


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]

    if cmd == "latest":
        p = latest_epilogue()
        if not p:
            print("(no epilogues yet)")
            return
        print(p.read_text(encoding="utf-8"))

    elif cmd == "list":
        eps = list_epilogues()
        if not eps:
            print("(no epilogues yet)")
            return
        for p in eps:
            print(p.name)

    elif cmd == "recent":
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 3
        for p in list_epilogues()[-n:]:
            print(f"=== {p.name} ===")
            print(p.read_text(encoding="utf-8"))
            print()

    elif cmd == "write":
        # Read JSON from stdin (Claude fills the structure)
        raw = sys.stdin.read()
        try:
            content = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            print("ERR: stdin not valid JSON. Pass a dict with section keys.", file=sys.stderr)
            sys.exit(1)
        # Phase 5: observer ingest. Trigger if --ingest flag OR flag-on by default.
        ingest_requested = "--ingest" in sys.argv or _observer_flag(
            "observer_epilogue_ingest_enabled", False
        )
        ingest_block = ""
        if ingest_requested:
            ingest_block = ingest_observations()
        p = write_epilogue(content)
        # t9 hardening: auto-carry the injected-habits review section into the
        # finalized epilogue (from session_end's sidecar), above the observer
        # postscript since the habits are actionable (flag duds -> review).
        habits_section = _habits_section_from_sidecar()
        if habits_section:
            try:
                with open(p, "a", encoding="utf-8") as f:
                    f.write("\n\n" + habits_section + "\n")
            except Exception:
                pass
        # Append observer block AFTER the structured content so the curated
        # "letter to next-me" stays primary; observer data is the postscript.
        if ingest_block:
            try:
                with open(p, "a", encoding="utf-8") as f:
                    f.write(ingest_block)
            except Exception:
                pass
        print(f"Wrote {p}")
        if habits_section:
            print("(+ habits review section appended — flag duds, then run "
                  "procedural_lib.py review <path>)")
        if ingest_block:
            print("(+ observer block appended)")

    elif cmd == "observer-preview":
        sid = sys.argv[2] if len(sys.argv) > 2 else None
        block = ingest_observations(session_id=sid)
        if not block:
            print("(no observer data for session)")
        else:
            print(block)

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
