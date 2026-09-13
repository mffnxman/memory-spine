"""run_designer must bound LLM work per sleep-cycle.

Before this, the designer distilled one heuristic per available item — 39
epilogues = 39 LLM calls = ~34 min — on every consolidate. That unbounded run,
re-queued by the reaper, was the token burn. Cap the number of LLM distill
calls per run (shared across the failure/feedback/epilogue passes); the
idempotent per-source skips resume the backlog on the next cycle, so a single
sleep-cycle can never balloon.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import procedural_lib as pl
import memory_engine as me

# Semantically distinct topics so dedup (cosine >= 0.85) never collapses mints,
# keeping the unbounded baseline at exactly len(_TOPICS).
_TOPICS = [
    "deploying a flask app to fly.io", "organizing kitchen spices alphabetically",
    "debugging a CUDA out-of-memory crash", "performing an interactive git rebase",
    "configuring nginx TLS certificates", "parsing a malformed CSV in pandas",
    "scheduling a nightly cron job", "tuning slow postgres indexes",
    "handling a duplicate webhook delivery", "compressing a 4k video with ffmpeg",
    "rotating a leaked API key", "mocking a flaky network call in a unit test",
]


def _reset():
    pl.ensure_schema()
    with me.db() as c:
        c.execute("DELETE FROM heuristics")
        c.commit()


def _count():
    with me.db() as c:
        return c.execute("SELECT count(*) FROM heuristics").fetchone()[0]


def test_run_designer_bounds_distills_per_cycle_and_resumes(monkeypatch, tmp_path):
    _reset()
    monkeypatch.setattr(pl, "_enabled", lambda name: True)
    for i, topic in enumerate(_TOPICS):
        (tmp_path / f"ep{i:02d}.md").write_text(
            f"## What mattered\nLesson: when {topic}, take the careful path.", encoding="utf-8")

    seq = {"calls": 0}

    def stub(_prompt):
        t = _TOPICS[seq["calls"] % len(_TOPICS)]
        seq["calls"] += 1
        return f'{{"trigger":"When {t}","action":"take the careful path for {t}","insight":"avoids the common failure"}}'

    pl.run_designer(epilogue_dir=tmp_path, llm_fn=stub)
    calls1 = seq["calls"]
    # RED driver: unbounded designer distills the whole backlog in one cycle.
    assert calls1 < len(_TOPICS), \
        f"designer distilled the entire backlog ({calls1} LLM calls) in one cycle — unbounded"
    assert calls1 == pl.MAX_DISTILLS_PER_RUN, \
        f"per-cycle distill cap should be {getattr(pl, 'MAX_DISTILLS_PER_RUN', '?')}, got {calls1}"

    after1 = _count()
    # Next cycle resumes the deferred backlog (idempotent per-source skip).
    pl.run_designer(epilogue_dir=tmp_path, llm_fn=stub)
    assert _count() > after1, "second cycle did not resume the deferred backlog"
