"""TDD: boot continuity-depth.

Boot picked the 'latest' epilogue by filesystem mtime, so a touched/reindexed
older epilogue could resurface as the thread from last-me. Sort by the authored
frontmatter `date:` instead (fallback mtime). Also surface a compact N-back
pointer to prior epilogues so the recent arc isn't lost at a cold start.
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import boot_ritual as br


def _epi(p: Path, date: str, body: str):
    p.write_text(f"---\ndate: {date}\nsession: s\n---\n\n{body}\n", encoding="utf-8")


def test_authored_date_beats_mtime(tmp_path, monkeypatch):
    a = tmp_path / "2026-05-29-1018.md"
    b = tmp_path / "2026-05-20-0900.md"
    _epi(a, "2026-05-29 10:18:00", "NEWEST-AUTHORED")
    _epi(b, "2026-05-20 09:00:00", "OLDER-AUTHORED")
    # simulate b being 'touched' more recently than a (the staleness bug trigger)
    old = time.time() - 100_000
    os.utime(a, (old, old))
    os.utime(b, (time.time(), time.time()))
    monkeypatch.setattr(br, "EPILOGUE_DIR", tmp_path)
    text = br.latest_epilogue_text()
    assert "NEWEST-AUTHORED" in text and "OLDER-AUTHORED" not in text


def test_recent_epilogue_pointers_are_n_back(tmp_path, monkeypatch):
    _epi(tmp_path / "e1.md", "2026-05-10 09:00:00", "one")
    _epi(tmp_path / "e2.md", "2026-05-20 09:00:00", "two")
    _epi(tmp_path / "e3.md", "2026-05-29 09:00:00", "three")
    monkeypatch.setattr(br, "EPILOGUE_DIR", tmp_path)
    lines = br.recent_epilogue_lines(limit=2)
    # the 2 epilogues BEFORE the latest (e3), most-recent-first: e2 then e1
    assert len(lines) == 2
    assert any("2026-05-20" in l for l in lines)
    assert not any("three" in l for l in lines)  # latest is shown in full elsewhere
