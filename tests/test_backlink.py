"""TDD: auto-backlinking sleep pass.

70+ spine memories have no related: links — the graph the PPR/viz walks is
starving. Each cycle, memories with fewer than CAP related links get their
top embedding neighbors (cosine >= SIM_MIN) appended to related: frontmatter.
Append-only (hand-curated links never removed), digest_/session_handoff_
files excluded (window summaries, not semantic peers), idempotent.
"""

import sqlite3
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backlink


def _blob(vec):
    return struct.pack(f"{len(vec)}f", *vec)


# ---- pure logic: suggest() ---------------------------------------------------

VECS = {
    "a.md": [1.0, 0.0, 0.0],
    "close_to_a.md": [0.98, 0.2, 0.0],
    "far.md": [0.0, 1.0, 0.0],
    "b.md": [0.7, 0.7, 0.0],
}


def test_suggest_picks_nearest_above_threshold():
    s = backlink.suggest(VECS, existing={}, sim_min=0.8, cap=5)
    assert "close_to_a.md" in s.get("a.md", [])
    assert "far.md" not in s.get("a.md", [])


def test_suggest_respects_existing_and_cap():
    existing = {"a.md": ["close_to_a.md"]}
    s = backlink.suggest(VECS, existing, sim_min=0.5, cap=2)
    # cap 2, one slot used -> at most 1 new suggestion, never re-suggesting existing
    assert len(s.get("a.md", [])) <= 1
    assert "close_to_a.md" not in s.get("a.md", [])


def test_suggest_excludes_self_and_full_files():
    existing = {"a.md": ["x.md", "y.md", "z.md", "w.md", "v.md"]}  # already at cap 5
    s = backlink.suggest(VECS, existing, sim_min=0.5, cap=5)
    assert "a.md" not in s, "file at cap must not receive suggestions"
    for fn, targets in s.items():
        assert fn not in targets, "self-link"


# ---- integration: run() -------------------------------------------------------


def _env(tmp_path, files, vectors):
    mem = tmp_path / "memory"
    mem.mkdir()
    for fn, related in files.items():
        rel_line = "related: {}\n".format(", ".join(related)) if related else ""
        (mem / fn).write_text(
            "---\nname: {}\ndescription: test\ntype: project\n{}---\nbody\n".format(
                fn, rel_line
            ),
            encoding="utf-8",
        )
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE embeddings (filename TEXT PRIMARY KEY, mtime REAL, model TEXT, dim INT, vector BLOB, content_hash TEXT)"
    )
    for fn, vec in vectors.items():
        conn.execute(
            "INSERT INTO embeddings VALUES (?,0,'bge',3,?, 'h')", (fn, _blob(vec))
        )
    conn.commit()
    conn.close()
    return mem, db


def test_run_appends_related_frontmatter(tmp_path, monkeypatch):
    mem, db = _env(
        tmp_path,
        files={"a.md": [], "close_to_a.md": [], "far.md": []},
        vectors=VECS,
    )
    monkeypatch.setattr(backlink, "_emit_reindex", lambda p: None)
    monkeypatch.setattr(backlink, "_snapshot", lambda p: None)
    summary = backlink.run(memory_dir=mem, db_path=db, sim_min=0.8)
    assert summary["links_added"] >= 2  # a <-> close_to_a both directions
    text = (mem / "a.md").read_text(encoding="utf-8")
    assert "related:" in text and "close_to_a.md" in text
    # far.md got nothing
    assert "related:" not in (mem / "far.md").read_text(encoding="utf-8")


def test_run_is_idempotent(tmp_path, monkeypatch):
    mem, db = _env(tmp_path, files={"a.md": [], "close_to_a.md": []}, vectors=VECS)
    monkeypatch.setattr(backlink, "_emit_reindex", lambda p: None)
    monkeypatch.setattr(backlink, "_snapshot", lambda p: None)
    backlink.run(memory_dir=mem, db_path=db, sim_min=0.8)
    second = backlink.run(memory_dir=mem, db_path=db, sim_min=0.8)
    assert second["links_added"] == 0, "second pass must add nothing"
    text = (mem / "a.md").read_text(encoding="utf-8")
    assert text.count("close_to_a.md") == 1, "duplicate link written"


def test_run_skips_digests_and_handoffs(tmp_path, monkeypatch):
    mem, db = _env(
        tmp_path,
        files={"digest_2026-w20.md": [], "session_handoff_x.md": [], "a.md": []},
        vectors={
            "digest_2026-w20.md": [1.0, 0.0, 0.0],
            "session_handoff_x.md": [0.99, 0.1, 0.0],
            "a.md": [0.98, 0.15, 0.0],
        },
    )
    monkeypatch.setattr(backlink, "_emit_reindex", lambda p: None)
    monkeypatch.setattr(backlink, "_snapshot", lambda p: None)
    backlink.run(memory_dir=mem, db_path=db, sim_min=0.5)
    assert "related:" not in (mem / "digest_2026-w20.md").read_text(encoding="utf-8")
    a_text = (mem / "a.md").read_text(encoding="utf-8")
    assert "digest_2026-w20" not in a_text and "session_handoff_x" not in a_text


def test_run_preserves_existing_links(tmp_path, monkeypatch):
    mem, db = _env(
        tmp_path,
        files={"a.md": ["hand_curated.md"], "close_to_a.md": []},
        vectors=VECS,
    )
    monkeypatch.setattr(backlink, "_emit_reindex", lambda p: None)
    monkeypatch.setattr(backlink, "_snapshot", lambda p: None)
    backlink.run(memory_dir=mem, db_path=db, sim_min=0.8)
    text = (mem / "a.md").read_text(encoding="utf-8")
    assert "hand_curated.md" in text, "hand-curated link removed!"
    assert "close_to_a.md" in text
