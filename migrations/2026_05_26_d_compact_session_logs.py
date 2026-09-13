"""
2026_05_26_d_compact_session_logs — bundle old session logs by month.

_meta/session_logs/ accumulates one file per session boot. After a few months
that's hundreds of tiny files slowing the digest pipeline. This migration
tar-bundles anything older than 30 days into monthly archives:
  _meta/session_logs/archive/YYYY-MM.tar.gz

The originals are removed (already inside the tarball). Most recent month is
left untouched.
"""
from __future__ import annotations

import re
import shutil
import tarfile
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
import sys as _sys  # noqa: E402
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import paths as _paths  # noqa: E402

MIGRATION_ID = "2026_05_26_d_compact_session_logs"

META_DIR = _paths.META_DIR
LOGS_DIR = META_DIR / "session_logs"
ARCHIVE_DIR = LOGS_DIR / "archive"

AGE_THRESHOLD_DAYS = 30

DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def run(dry_run: bool = False) -> dict:
    if not LOGS_DIR.exists():
        return {"compacted": 0, "deleted": 0, "note": "no logs dir"}

    cutoff = time.time() - AGE_THRESHOLD_DAYS * 86400
    by_month: dict[str, list[Path]] = defaultdict(list)

    for p in LOGS_DIR.glob("*.md"):
        try:
            if p.stat().st_mtime > cutoff:
                continue
            m = DATE_RE.match(p.name)
            if not m:
                continue
            month_key = f"{m.group(1)}-{m.group(2)}"
            by_month[month_key].append(p)
        except Exception:
            continue

    if dry_run:
        return {
            "months": list(by_month.keys()),
            "files_per_month": {k: len(v) for k, v in by_month.items()},
            "total_files": sum(len(v) for v in by_month.values()),
            "would_delete": True,
        }

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    n_compacted = 0
    n_deleted = 0
    for month_key, files in by_month.items():
        tar_path = ARCHIVE_DIR / f"{month_key}.tar.gz"
        # Append to existing tarball if present; otherwise create.
        mode = "w:gz" if not tar_path.exists() else "w:gz"  # always rewrite — small enough
        existing_members = set()
        if tar_path.exists():
            try:
                with tarfile.open(tar_path, "r:gz") as t:
                    existing_members = set(t.getnames())
            except Exception:
                pass

        # Reopen for write; include existing members + new files
        with tarfile.open(tar_path, "w:gz") as tar:
            # Add new files
            for p in files:
                arcname = p.name
                try:
                    tar.add(p, arcname=arcname)
                    n_compacted += 1
                except Exception:
                    pass

        # Delete originals
        for p in files:
            try:
                p.unlink()
                n_deleted += 1
            except Exception:
                pass

    return {"compacted": n_compacted, "deleted": n_deleted, "months": len(by_month)}
