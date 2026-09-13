"""TDD P3: controller — embedding-kNN retrieval over heuristic triggers.

The v1 controller is plain embedding kNN (the only retrieval method that
survived adversarial verification; LLM-rerank is deferred to a P6 A/B). Embed the
task context, cosine against active heuristic trigger embeddings, rank by
similarity x recency x corroboration, return top-k. Archived heuristics are excluded.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import procedural_lib as pl
import memory_engine as me

PY = {"trigger": "running python3 on Windows hits a UnicodeEncodeError on output",
      "action": "set PYTHONIOENCODING=utf-8 before the command", "insight": ""}
GIT = {"trigger": "about to git push --force to a shared branch",
       "action": "stop and confirm with the human first", "insight": ""}
BOOT = {"trigger": "booting a fresh session with the user",
        "action": "start at baseline, do not perform enthusiasm", "insight": ""}


def _reset():
    pl.ensure_schema()
    with me.db() as c:
        c.execute("DELETE FROM heuristics")
        c.commit()


def _seed_all():
    for h in (PY, GIT, BOOT):
        pl.upsert_heuristic(h, origin="test", polarity="failure_derived")


def test_retrieve_ranks_relevant_heuristic_first():
    _reset(); _seed_all()
    res = pl.retrieve("I keep getting a UnicodeEncodeError when running a python script", k=2)
    assert 0 < len(res) <= 2
    assert "PYTHONIOENCODING" in res[0]["action"]


def test_retrieve_returns_empty_on_empty_pool():
    _reset()
    assert pl.retrieve("anything at all", k=5) == []


def test_retrieve_excludes_archived():
    _reset()
    r = pl.upsert_heuristic(PY, origin="test", polarity="failure_derived")
    with me.db() as c:
        c.execute("UPDATE heuristics SET status='archived' WHERE id=?", (r["id"],))
        c.commit()
    assert pl.retrieve("UnicodeEncodeError python", k=5) == []


def test_retrieve_results_carry_score_and_fields():
    _reset(); _seed_all()
    res = pl.retrieve("git force push to shared branch", k=1)
    assert res[0]["action"].startswith("stop and confirm")
    assert "score" in res[0] and res[0]["score"] > 0


def test_high_corroboration_does_not_override_better_cosine():
    # Relevance must dominate: a strongly-matching heuristic (corrob 1) outranks
    # an unrelated one even if the latter is heavily corroborated.
    _reset()
    rel = pl.upsert_heuristic(
        {"trigger": "deploying a flask app to fly.io", "action": "set the PORT env var", "insight": ""},
        origin="test", polarity="failure_derived")["id"]
    junk = pl.upsert_heuristic(
        {"trigger": "organizing kitchen spices alphabetically", "action": "use a lazy susan", "insight": ""},
        origin="test", polarity="failure_derived")["id"]
    with me.db() as c:
        c.execute("UPDATE heuristics SET corroboration=25 WHERE id=?", (junk,))
        c.commit()
    res = pl.retrieve("how do I deploy my flask app to fly.io", k=1)
    assert res[0]["id"] == rel


def test_upsert_persists_embedding_blob():
    _reset()
    pl.upsert_heuristic(PY, origin="test", polarity="failure_derived")
    with me.db() as c:
        blob = c.execute("SELECT embedding FROM heuristics WHERE status='active'").fetchone()[0]
    assert blob is not None
    assert len(me._blob_to_vec(blob)) == 384  # bge-small-en-v1.5
