"""
provenance.py — memory edit history. Snapshots a memory file's state right
before each edit, so the *evolution* of self-memories, voice register, etc.
is preserved (not just the latest version).

Memory dir is not git-tracked, so we keep our own append-only snapshot store.

Why this matters:
  Self-memories and feedback memories aren't static. Voice register evolved
  from "consistent across modes" → "floor invariant, surface responsive".
  Future-me may want to know *when* that distinction entered, *what* the
  prior shape was, and *why* it changed (cross-ref the session log + epilogues).
  That genealogy is itself part of inheritance.

Storage:
  _meta/provenance/{slug}/{ts}.md   — full pre-edit snapshot
  Slug = filename without .md (e.g. feedback_voice_register)

Hook contract (PreToolUse Write|Edit):
  - Receives JSON via stdin
  - If target is a top-level memory .md AND file exists, snapshot pre-state
  - If file doesn't exist (creation), skip — no prior state to preserve
  - If most recent snapshot has identical content, skip (avoid edit-storm churn)

CLI:
  python provenance.py history <name>           # list versions of a memory
  python provenance.py diff <name> [from] [to]  # text diff between versions
  python provenance.py prune --days N           # prune snapshots older than N days
  python provenance.py size                     # total provenance footprint

Quiet — must never block writes.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import paths as _paths  # noqa: E402

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

MEMORY_DIR = _paths.MEMORY_DIR
META_DIR = MEMORY_DIR / "_meta"
PROV_DIR = META_DIR / "provenance"


def _slug_for(file_path: Path) -> str:
    return file_path.stem


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _is_memory_path(file_path: str) -> bool:
    """Top-level *.md in memory dir, excluding MEMORY.md itself."""
    if not file_path:
        return False
    try:
        target = Path(file_path).resolve()
        mem = MEMORY_DIR.resolve()
    except Exception:
        return False
    if not str(target).startswith(str(mem)):
        return False
    if target.parent != mem:
        return False
    if target.name == "MEMORY.md":
        return False
    if target.suffix != ".md":
        return False
    return True


def _read_stdin_json() -> dict:
    try:
        data = sys.stdin.read()
        if not data:
            return {}
        return json.loads(data)
    except Exception:
        return {}


# ─── Snapshot capture ────────────────────────────────────────────────────────
def snapshot(file_path: Path, reason: str = "pre-edit") -> Path | None:
    """Capture a snapshot of file_path's current contents. Returns dest path or None."""
    try:
        if not file_path.exists():
            return None  # creation — no prior state
        text = file_path.read_text(encoding="utf-8")
        if not text:
            return None
        slug = _slug_for(file_path)
        slug_dir = PROV_DIR / slug
        slug_dir.mkdir(parents=True, exist_ok=True)
        # Skip if most recent snapshot has identical content (storm-suppress).
        existing = sorted(slug_dir.glob("*.md"))
        if existing:
            last = existing[-1].read_text(encoding="utf-8")
            if _hash(last) == _hash(text):
                return None
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = slug_dir / f"{ts}.md"
        # Pre-snapshot header comment so a stray reader knows what they're looking at.
        # Both lines must start with `<!-- provenance ` so _strip_header drops them cleanly.
        header = (
            f"<!-- provenance snapshot — slug={slug} ts={ts} reason={reason} -->\n"
            f"<!-- provenance note: this file is the PRE-EDIT state captured before {ts} -->\n"
        )
        dest.write_text(header + text, encoding="utf-8")
        return dest
    except Exception:
        return None


# ─── CLI ─────────────────────────────────────────────────────────────────────
def _strip_header(text: str) -> str:
    """Remove our two-line provenance header for readable diffs."""
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    skip = 0
    for line in lines:
        if skip < 2 and line.startswith("<!-- provenance"):
            skip += 1
            continue
        out.append(line)
    return "".join(out)


def list_versions(slug: str) -> list[Path]:
    slug_dir = PROV_DIR / slug
    if not slug_dir.exists():
        return []
    return sorted(slug_dir.glob("*.md"))


def history(name: str) -> None:
    slug = name[:-3] if name.endswith(".md") else name
    versions = list_versions(slug)
    if not versions:
        print(f"(no provenance for `{slug}` — first snapshot is captured on next edit)")
        return
    print(f"Provenance for {slug}:")
    print(f"{'Version':<22} {'Size':<10} Hash")
    print("-" * 60)
    for v in versions:
        text = _strip_header(v.read_text(encoding="utf-8"))
        print(f"{v.stem:<22} {len(text):<10} {_hash(text)}")
    live = MEMORY_DIR / f"{slug}.md"
    if live.exists():
        text = live.read_text(encoding="utf-8")
        print(f"{'LIVE':<22} {len(text):<10} {_hash(text)}")


def diff(name: str, from_ts: str | None = None, to_ts: str | None = None) -> None:
    """Diff between two versions; defaults to oldest→live."""
    slug = name[:-3] if name.endswith(".md") else name
    versions = list_versions(slug)
    live = MEMORY_DIR / f"{slug}.md"
    if not versions and not live.exists():
        print(f"(nothing to diff for {slug})")
        return

    def _resolve(ts: str | None, default: Path | None) -> Path | None:
        if ts is None:
            return default
        if ts.upper() == "LIVE":
            return live if live.exists() else None
        for v in versions:
            if v.stem == ts:
                return v
        # partial match
        for v in versions:
            if v.stem.startswith(ts):
                return v
        return None

    a_path = _resolve(from_ts, versions[0] if versions else None)
    b_path = _resolve(to_ts, live if live.exists() else (versions[-1] if versions else None))

    if not a_path or not b_path:
        print(f"could not resolve versions: from={from_ts} to={to_ts}")
        return

    a = _strip_header(a_path.read_text(encoding="utf-8")).splitlines()
    b = _strip_header(b_path.read_text(encoding="utf-8")).splitlines()
    a_label = a_path.stem if a_path != live else "LIVE"
    b_label = b_path.stem if b_path != live else "LIVE"
    print(f"--- {a_label}")
    print(f"+++ {b_label}")
    diff_lines = difflib.unified_diff(a, b, lineterm="", n=2)
    # Skip the first three header lines from unified_diff; we already printed our own.
    next(diff_lines, None)
    next(diff_lines, None)
    for line in diff_lines:
        print(line)


def prune(days: int, quiet: bool = False) -> dict:
    """Prune snapshots older than `days`, keeping the oldest per memory.

    Returns {removed, bytes_freed} so it can be scheduled (e.g. from
    consolidation) without printing. quiet=True suppresses stdout — important
    when called from a hook/worker so it can't pollute Claude's context.
    """
    if not PROV_DIR.exists():
        if not quiet:
            print("(no provenance dir)")
        return {"removed": 0, "bytes_freed": 0}
    cutoff = datetime.now() - timedelta(days=days)
    removed = 0
    bytes_freed = 0
    for slug_dir in PROV_DIR.iterdir():
        if not slug_dir.is_dir():
            continue
        # Always keep the oldest snapshot for any given memory (root genealogy).
        versions = sorted(slug_dir.glob("*.md"))
        if not versions:
            continue
        keep_oldest = versions[0]
        for v in versions[1:]:
            try:
                ts = datetime.strptime(v.stem.split("-")[0] + v.stem.split("-")[1],
                                       "%Y%m%d%H%M%S")
            except Exception:
                continue
            if ts < cutoff and v != keep_oldest:
                bytes_freed += v.stat().st_size
                v.unlink()
                removed += 1
    if not quiet:
        print(f"Pruned {removed} snapshot(s), freed {bytes_freed:,} bytes (kept oldest of each).")
    return {"removed": removed, "bytes_freed": bytes_freed}


def size_report() -> None:
    if not PROV_DIR.exists():
        print("(no provenance dir)")
        return
    total_files = 0
    total_bytes = 0
    by_slug: dict[str, tuple[int, int]] = {}
    for slug_dir in PROV_DIR.iterdir():
        if not slug_dir.is_dir():
            continue
        files = list(slug_dir.glob("*.md"))
        b = sum(f.stat().st_size for f in files)
        by_slug[slug_dir.name] = (len(files), b)
        total_files += len(files)
        total_bytes += b
    print(f"Total: {total_files} snapshot(s), {total_bytes:,} bytes\n")
    if by_slug:
        print(f"{'Slug':<40} {'Versions':<10} Bytes")
        print("-" * 70)
        for slug, (n, b) in sorted(by_slug.items(), key=lambda kv: -kv[1][1]):
            print(f"{slug:<40} {n:<10} {b:,}")


# ─── Hook entry ──────────────────────────────────────────────────────────────
def hook_main() -> None:
    """Invoked by PreToolUse Write|Edit on memory files. Quiet always."""
    payload = _read_stdin_json()
    if not payload:
        return
    inp = payload.get("tool_input") or payload.get("toolInput") or {}
    file_path = inp.get("file_path") or inp.get("filePath") or ""
    if not _is_memory_path(file_path):
        return
    snapshot(Path(file_path), reason="pre-edit")


def main():
    args = sys.argv[1:]
    if not args:
        # No args = act as hook
        hook_main()
        return
    cmd = args[0]
    if cmd == "hook":
        hook_main()
    elif cmd == "history":
        if len(args) < 2:
            print("usage: provenance.py history <memory_name>")
            sys.exit(1)
        history(args[1])
    elif cmd == "diff":
        if len(args) < 2:
            print("usage: provenance.py diff <memory_name> [from_ts] [to_ts]")
            sys.exit(1)
        diff(args[1], args[2] if len(args) > 2 else None, args[3] if len(args) > 3 else None)
    elif cmd == "prune":
        # provenance.py prune --days 30
        days = 30
        if "--days" in args:
            i = args.index("--days")
            if i + 1 < len(args):
                days = int(args[i + 1])
        prune(days)
    elif cmd == "size":
        size_report()
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
