"""
threads.py — open threads as first-class.

Open threads currently live inside epilogue files under "Open threads (for
next-me)". That makes them point-in-time and easy to lose when newer
epilogues land on top. This script makes them an ordered, persistent list
that survives across sessions and surfaces in the boot ritual.

Storage: _meta/open_threads.md (markdown, human-readable, GitHub-style checklist)

Format:
    # Open Threads
    > Persistent across sessions. Boot ritual surfaces unchecked items.

    - [ ] {id} — {body} _(opened {date})_
    - [x] {id} — {body} _(closed {date})_

Usage:
    python threads.py list                    # show open threads
    python threads.py list --all              # include closed
    python threads.py add "<body>"            # append a new thread
    python threads.py close <id>              # mark closed
    python threads.py reopen <id>             # mark open again
    python threads.py rm <id>                 # delete entirely
"""
from __future__ import annotations

import re
import sys
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
THREADS_FILE = META_DIR / "open_threads.md"

HEADER = (
    "# Open Threads\n\n"
    "> Persistent unresolved work that should follow next-me across sessions.\n"
    "> Boot ritual surfaces unchecked items. Use `/threads` to manage.\n\n"
)

# Match: `- [ ] <id> — <body> _(opened YYYY-MM-DD)_` or closed/reopened variants.
# Body is greedy until the optional ` _(opened ...)_` suffix, which we strip
# separately so an em-dash inside the body doesn't fool us.
LINE_RE = re.compile(
    r"^- \[(?P<state>[ x])\] (?P<id>t\d+) — (?P<rest>.+)$"
)
OPENED_SUFFIX_RE = re.compile(r" _\(opened (?P<opened>\d{4}-\d{2}-\d{2})\)_\s*$")


def _ensure_file() -> None:
    META_DIR.mkdir(parents=True, exist_ok=True)
    if not THREADS_FILE.exists():
        THREADS_FILE.write_text(HEADER, encoding="utf-8")


def _load() -> tuple[str, list[dict]]:
    """Return (header_block, [thread_dicts])."""
    _ensure_file()
    text = THREADS_FILE.read_text(encoding="utf-8")
    threads: list[dict] = []
    header_lines: list[str] = []
    in_threads = False
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if m:
            in_threads = True
            rest = m.group("rest")
            opened = None
            sm = OPENED_SUFFIX_RE.search(rest)
            if sm:
                opened = sm.group("opened")
                rest = rest[: sm.start()].rstrip()
            threads.append({
                "id": m.group("id"),
                "state": m.group("state"),
                "body": rest.strip(),
                "opened": opened,
                "raw": line,
            })
        elif not in_threads:
            header_lines.append(line)
    header = "\n".join(header_lines).rstrip() + "\n\n"
    if not header.strip():
        header = HEADER
    return header, threads


def _next_id(threads: list[dict]) -> str:
    nums = []
    for t in threads:
        try:
            nums.append(int(t["id"][1:]))
        except (ValueError, IndexError):
            pass
    n = (max(nums) + 1) if nums else 1
    return f"t{n}"


def _save(header: str, threads: list[dict]) -> None:
    body_lines = []
    today = datetime.now().strftime("%Y-%m-%d")
    for t in threads:
        opened = t.get("opened") or today
        check = "x" if t["state"] == "x" else " "
        body_lines.append(f"- [{check}] {t['id']} — {t['body']} _(opened {opened})_")
    text = header + "\n".join(body_lines) + "\n"
    THREADS_FILE.write_text(text, encoding="utf-8")


def add(body: str) -> str:
    body = body.strip()
    if not body:
        raise ValueError("empty body")
    header, threads = _load()
    tid = _next_id(threads)
    threads.append({
        "id": tid,
        "state": " ",
        "body": body,
        "opened": datetime.now().strftime("%Y-%m-%d"),
    })
    _save(header, threads)
    return tid


def close(tid: str) -> bool:
    header, threads = _load()
    for t in threads:
        if t["id"] == tid:
            t["state"] = "x"
            _save(header, threads)
            return True
    return False


def reopen(tid: str) -> bool:
    header, threads = _load()
    for t in threads:
        if t["id"] == tid:
            t["state"] = " "
            _save(header, threads)
            return True
    return False


def remove(tid: str) -> bool:
    header, threads = _load()
    new = [t for t in threads if t["id"] != tid]
    if len(new) == len(threads):
        return False
    _save(header, new)
    return True


def list_threads(include_closed: bool = False) -> list[dict]:
    _, threads = _load()
    if include_closed:
        return threads
    return [t for t in threads if t["state"] == " "]


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__)
        return

    cmd = args[0]
    if cmd == "list":
        include_all = "--all" in args
        ts = list_threads(include_closed=include_all)
        if not ts:
            print("(no open threads)")
            return
        for t in ts:
            mark = "✓" if t["state"] == "x" else " "
            print(f"  [{mark}] {t['id']:>4s}  {t['body']}  _({t.get('opened') or '?'})_")
        if not include_all:
            closed = sum(1 for t in list_threads(include_closed=True) if t["state"] == "x")
            if closed:
                print(f"\n  ({closed} closed thread{'s' if closed != 1 else ''} hidden — use --all to show)")

    elif cmd == "add":
        body = " ".join(args[1:])
        if not body:
            print("ERR: usage: threads.py add \"<body>\"", file=sys.stderr)
            sys.exit(1)
        tid = add(body)
        print(f"Added {tid}")

    elif cmd in ("close", "done", "resolve"):
        if len(args) < 2:
            print("ERR: usage: threads.py close <id>", file=sys.stderr)
            sys.exit(1)
        ok = close(args[1])
        print(f"Closed {args[1]}" if ok else f"No thread {args[1]}")

    elif cmd == "reopen":
        if len(args) < 2:
            print("ERR: usage: threads.py reopen <id>", file=sys.stderr)
            sys.exit(1)
        ok = reopen(args[1])
        print(f"Reopened {args[1]}" if ok else f"No thread {args[1]}")

    elif cmd in ("rm", "delete"):
        if len(args) < 2:
            print("ERR: usage: threads.py rm <id>", file=sys.stderr)
            sys.exit(1)
        ok = remove(args[1])
        print(f"Removed {args[1]}" if ok else f"No thread {args[1]}")

    elif cmd == "path":
        print(str(THREADS_FILE))

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
