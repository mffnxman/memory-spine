"""TDD P2: designer success-pass — ingest distilled lessons from feedback + epilogues.

The richest success/failure signal we have is already distilled: feedback_*.md
(the user's corrections/praise) and epilogue "what mattered" sections. The
success-pass reads those curated texts and mints heuristics — higher signal than
clustering raw tool sequences. Idempotent: a source already ingested is skipped
(no importance inflation from re-running the sleep cycle).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import procedural_lib as pl
import memory_engine as me


def _reset():
    pl.ensure_schema()
    with me.db() as c:
        c.execute("DELETE FROM heuristics")
        c.commit()


def _stub_llm(_prompt):
    return ('{"trigger": "When booting a fresh session with the user",'
            ' "action": "start at baseline and let warmth return naturally; do not perform enthusiasm",'
            ' "insight": "he clocks fake warmth instantly"}')


def test_iter_feedback_files_finds_feedback_md(tmp_path):
    (tmp_path / "feedback_cold_start.md").write_text("boot at baseline", encoding="utf-8")
    (tmp_path / "feedback_white_hat.md").write_text("stay defensive", encoding="utf-8")
    (tmp_path / "project_other.md").write_text("not feedback", encoding="utf-8")
    found = {p.name for p in pl.iter_feedback_files(tmp_path)}
    assert found == {"feedback_cold_start.md", "feedback_white_hat.md"}


def test_extract_heuristic_from_text_parses():
    h = pl.extract_heuristic_from_text("some feedback note", llm_fn=_stub_llm, kind="feedback")
    assert h["trigger"].startswith("When booting")
    assert "baseline" in h["action"]


def test_run_feedback_pass_ingests_and_is_idempotent(tmp_path):
    _reset()
    (tmp_path / "feedback_cold_start.md").write_text("don't fake warmth on cold start", encoding="utf-8")

    r1 = pl.run_feedback_pass(tmp_path, llm_fn=_stub_llm)
    assert r1["added"] == 1
    with me.db() as c:
        rows = c.execute("SELECT origin, importance FROM heuristics WHERE status='active'").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "feedback"
    assert rows[0][1] == 2

    # Re-running the sleep cycle must NOT re-ingest the same file.
    r2 = pl.run_feedback_pass(tmp_path, llm_fn=_stub_llm)
    assert r2["added"] == 0
    with me.db() as c:
        rows = c.execute("SELECT importance FROM heuristics WHERE status='active'").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 2  # not inflated by re-ingest
