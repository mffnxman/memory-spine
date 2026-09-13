"""
2026_05_26_a_purge_orphan_probes — delete probe drafts >30 days old with no
matching epilogue link.

Probe drafts live in _meta/probes/. Many were created mid-May during the
inheritance probe genesis work. Anything orphan + ancient is safe to drop.
Backup tarball lands in _meta/migrations_backups/.
"""
from __future__ import annotations

import shutil
import tarfile
import time
from pathlib import Path
import sys as _sys  # noqa: E402
_sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import paths as _paths  # noqa: E402

MIGRATION_ID = "2026_05_26_a_purge_orphan_probes"

META_DIR = _paths.META_DIR
PROBES_DIR = META_DIR / "probes"
EPILOGUES_DIR = META_DIR / "epilogues"
BACKUP_DIR = META_DIR / "migrations_backups"

AGE_THRESHOLD_DAYS = 30


def _has_epilogue_link(probe_text: str) -> bool:
    return "epilogue" in probe_text.lower() or "session_logs" in probe_text.lower()


def run(dry_run: bool = False) -> dict:
    if not PROBES_DIR.exists():
        return {"orphans": 0, "deleted": 0, "note": "no probes dir"}

    now = time.time()
    cutoff = now - AGE_THRESHOLD_DAYS * 86400

    candidates = []
    for p in PROBES_DIR.glob("*.md"):
        try:
            if p.stat().st_mtime > cutoff:
                continue
            text = p.read_text(encoding="utf-8", errors="ignore")
            if _has_epilogue_link(text):
                continue
            candidates.append(p)
        except Exception:
            continue

    if dry_run:
        return {"orphans": len(candidates), "deleted": 0, "would_delete": [p.name for p in candidates]}

    if candidates:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        tar_path = BACKUP_DIR / f"{MIGRATION_ID}.tar.gz"
        with tarfile.open(tar_path, "w:gz") as tar:
            for p in candidates:
                tar.add(p, arcname=f"probes/{p.name}")

    deleted = 0
    for p in candidates:
        try:
            p.unlink()
            deleted += 1
        except Exception:
            pass

    return {"orphans": len(candidates), "deleted": deleted}
