"""TDD: generated retrieval-floor benchmark + hallucination audit.

The curated continuity suite is 27 cases — too small, and nothing checks that
recall can't return phantoms. Two additions:

  1. FLOOR suite: auto-generated — every spine memory must be findable from
     its own description (catches index rot / embedding drift / missing rows).
     Temporal cases from digests. Runs via the same search path, keeps its own
     baseline (curated suite + its baseline stay untouched and comparable).
  2. Hallucination audit: structural provenance — no orphan embeddings (index
     rows for files that don't exist), no phantom entries in MEMORY.md's
     auto-index block, no co_recall edges into the void.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import benchmark_gen as bg


def _corpus(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "alpha.md").write_text(
        "---\nname: alpha memory\ndescription: how the alpha subsystem boots\ntype: project\n---\nbody\n",
        encoding="utf-8",
    )
    (mem / "beta.md").write_text(
        "---\nname: beta\ndescription: beta trading rules\ntype: reference\n---\nbody\n",
        encoding="utf-8",
    )
    (mem / "digest_2026-w20.md").write_text(
        "---\nname: Week 2026-W20 Digest\ndescription: week of dart genesis\ntype: digest\n---\nbody\n",
        encoding="utf-8",
    )
    (mem / "no_desc.md").write_text(
        "---\nname: bare\ntype: project\n---\nbody\n", encoding="utf-8"
    )
    (mem / "MEMORY.md").write_text(
        "# index\n<!-- AUTO-INDEX-START -->\n- `alpha.md` — how the alpha subsystem boots\n"
        "- `ghost.md` — a memory that does not exist\n<!-- AUTO-INDEX-END -->\n",
        encoding="utf-8",
    )
    return mem


def test_generate_floor_cases_from_descriptions(tmp_path):
    mem = _corpus(tmp_path)
    gen = bg.generate(memory_dir=mem)
    floor = {c["expected"][0]: c for c in gen["floor"]}
    assert "alpha.md" in floor
    assert floor["alpha.md"]["query"] == "how the alpha subsystem boots"
    assert "no_desc.md" not in floor, "memories without descriptions can't self-query"
    assert "MEMORY.md" not in floor


def test_generate_temporal_cases_from_digests(tmp_path):
    mem = _corpus(tmp_path)
    gen = bg.generate(memory_dir=mem)
    assert any(
        c["expected"] == ["digest_2026-w20.md"] and "2026-W20" in c["query"]
        for c in gen["temporal"]
    ), "digest temporal case missing"


def test_run_floor_scores_with_injected_search(tmp_path):
    mem = _corpus(tmp_path)
    gen = bg.generate(memory_dir=mem)

    def fake_search(query, top_k):
        # perfect retrieval for alpha, miss for everything else
        if "alpha" in query:
            return ["alpha.md", "beta.md"]
        return ["unrelated.md"]

    report = bg.run_floor(gen["floor"], search_fn=fake_search, top_k=3)
    assert report["n"] == len(gen["floor"])
    assert report["passed"] == 1
    assert 0 < report["mrr"] <= 1
    assert any(f["expected"] == "beta.md" for f in report["failures"])


def test_hallucination_audit_finds_phantoms_and_orphans(tmp_path):
    mem = _corpus(tmp_path)
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE embeddings (filename TEXT PRIMARY KEY, mtime REAL, model TEXT, dim INT, vector BLOB, content_hash TEXT)"
    )
    conn.execute("INSERT INTO embeddings VALUES ('alpha.md',0,'m',3,x'00','h')")
    conn.execute("INSERT INTO embeddings VALUES ('vanished.md',0,'m',3,x'00','h')")
    conn.execute(
        "CREATE TABLE co_recall (a TEXT, b TEXT, weight INT, decayed REAL, last_fired INT, PRIMARY KEY(a,b))"
    )
    conn.execute("INSERT INTO co_recall VALUES ('alpha.md','vanished.md',3,1.5,0)")
    conn.commit()
    conn.close()

    audit = bg.hallucination_audit(memory_dir=mem, db_path=db)
    assert "vanished.md" in audit["orphan_embeddings"]
    assert "alpha.md" not in audit["orphan_embeddings"]
    assert "ghost.md" in audit["phantom_index"]
    assert audit["orphan_co_recall"] == 1
    assert audit["clean"] is False


def test_hallucination_audit_clean_corpus(tmp_path):
    mem = _corpus(tmp_path)
    # remove the planted phantom from MEMORY.md
    idx = mem / "MEMORY.md"
    idx.write_text(
        "# index\n<!-- AUTO-INDEX-START -->\n- `alpha.md` — desc\n<!-- AUTO-INDEX-END -->\n",
        encoding="utf-8",
    )
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE embeddings (filename TEXT PRIMARY KEY, mtime REAL, model TEXT, dim INT, vector BLOB, content_hash TEXT)"
    )
    conn.execute("INSERT INTO embeddings VALUES ('alpha.md',0,'m',3,x'00','h')")
    conn.execute(
        "CREATE TABLE co_recall (a TEXT, b TEXT, weight INT, decayed REAL, last_fired INT, PRIMARY KEY(a,b))"
    )
    conn.commit()
    conn.close()
    audit = bg.hallucination_audit(memory_dir=mem, db_path=db)
    assert audit["clean"] is True
