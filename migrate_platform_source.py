"""
migrate_platform_source.py — backfill platform_source on existing memories.

Idempotent one-shot. Adds `platform_source: <source>` to frontmatter of every
top-level memory .md that doesn't already have one. Heuristic:

  - originSessionId looks like a Claude session id (uuid-ish)   -> claude_code
  - filename starts with `dartagnan_`                           -> dartagnan
  - filename starts with `duck-`                                -> duck_sentinel
  - frontmatter mentions d'Artagnan in name/description         -> dartagnan
  - default                                                     -> claude_code

Reads memory files via plain text parse (no memory_engine import dependency)
so it can run even in degraded states. Backs up each modified file to
`_meta/migrations_backups/2026_05_26_platform_source/<filename>.orig`.

Usage:
  python migrate_platform_source.py --dry-run   # report only
  python migrate_platform_source.py             # apply
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

MEMORY_DIR = _paths.MEMORY_DIR
BACKUP_DIR = MEMORY_DIR / "_meta" / "migrations_backups" / "2026_05_26_platform_source"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths as _paths  # noqa: E402
from memory_engine import FRONTMATTER_RE  # canonical lenient variant
PLATFORM_RE = re.compile(r"^platform_source:", re.MULTILINE)
ORIGIN_RE = re.compile(r"^originSessionId:\s*(.+)$", re.MULTILINE)


def detect_source(filename: str, frontmatter: str) -> str:
    fn = filename.lower()
    if fn.startswith("dartagnan_") or fn.startswith("dartagnan-"):
        return "dartagnan"
    if fn.startswith("duck-") or fn.startswith("duck_"):
        return "duck_sentinel"
    fm_lower = frontmatter.lower()
    if "d'artagnan" in fm_lower or "dartagnan" in fm_lower:
        return "dartagnan"
    if "duck" in fm_lower and ("sentinel" in fm_lower or "co-pilot" in fm_lower):
        return "duck_sentinel"
    # Default for anything with a Claude session id or anything else.
    if ORIGIN_RE.search(frontmatter):
        return "claude_code"
    return "claude_code"


def patch_frontmatter(text: str, source: str) -> str:
    """Insert `platform_source: <source>` into the frontmatter block."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        # No frontmatter — prepend one.
        return f"---\nplatform_source: {source}\n---\n{text}"
    fm = m.group(1)
    if PLATFORM_RE.search(fm):
        return text  # already present
    new_fm = fm + f"\nplatform_source: {source}"
    return f"---\n{new_fm}\n---\n" + text[m.end():]


def process_file(path: Path, dry_run: bool) -> tuple[str, str | None]:
    """Returns (status, source) for reporting."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as e:
        return (f"read-error: {e}", None)

    m = FRONTMATTER_RE.match(text)
    fm = m.group(1) if m else ""

    if PLATFORM_RE.search(fm):
        return ("already-has", None)

    source = detect_source(path.name, fm)
    new_text = patch_frontmatter(text, source)

    if dry_run:
        return (f"would-add", source)

    # Back up before write.
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup_path = BACKUP_DIR / f"{path.name}.orig"
    if not backup_path.exists():
        try:
            shutil.copy2(path, backup_path)
        except Exception as e:
            return (f"backup-error: {e}", source)

    try:
        path.write_text(new_text, encoding="utf-8")
    except Exception as e:
        return (f"write-error: {e}", source)

    return ("added", source)


def main():
    ap = argparse.ArgumentParser(description="Backfill platform_source on memory files")
    ap.add_argument("--dry-run", action="store_true", help="Report only, don't write")
    args = ap.parse_args()

    counts = {"added": 0, "would-add": 0, "already-has": 0, "errors": 0}
    by_source = {"claude_code": 0, "dartagnan": 0, "duck_sentinel": 0, "other": 0}

    md_files = sorted(MEMORY_DIR.glob("*.md"))
    md_files = [f for f in md_files if f.name not in ("MEMORY.md",)]

    for path in md_files:
        status, source = process_file(path, args.dry_run)
        if status.startswith("read") or status.startswith("write") or status.startswith("backup"):
            counts["errors"] += 1
            print(f"  ERROR {path.name}: {status}")
            continue
        counts[status] = counts.get(status, 0) + 1
        if source:
            by_source[source] = by_source.get(source, 0) + 1
        if status in ("added", "would-add"):
            print(f"  {status:10s} {path.name:50s} -> {source}")

    print()
    print(f"Files scanned: {len(md_files)}")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    print()
    print("By source:")
    for k, v in by_source.items():
        if v:
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
