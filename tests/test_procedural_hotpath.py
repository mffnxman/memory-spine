"""TDD: retrieve() must not inline-embed unembedded rows on the prompt hot path
(recon H4).

prefetch.recall_lane -> retrieve runs on every UserPromptSubmit (<500ms budget).
retrieve embedded any NULL-embedding heuristic inline (one model call per row), so
right after a sleep cycle adds a batch of not-yet-reindexed heuristics, the very
next prompt fired N inline embeds with no cap — the same unbounded-hot-path-load
class the freeze fix cured. Skip unembedded rows instead; the reindex/designer
path embeds them shortly, and precision-first prefers a missing inject over a
hot-path stall.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import procedural_lib as pl


def _seed(trigger, action, importance=2):
    pl.ensure_schema()
    with pl.me.db() as c:
        cur = c.execute(
            "INSERT INTO heuristics (trigger, action, insight, origin, polarity, "
            "importance, created_ts, last_used_ts, status) VALUES (?,?,?,?,?,?,?,?,?)",
            (trigger, action, "", "test", "failure_derived", importance,
             int(time.time()), int(time.time()), "active"))
        c.commit()
        return cur.lastrowid


def test_retrieve_skips_unembedded_rows_without_inline_embedding(monkeypatch):
    embedded = _seed("When embedded", "do e")
    null_row = _seed("When unembedded", "do u")
    with pl.me.db() as c:
        c.execute("UPDATE heuristics SET embedding=? WHERE id=?", (b"blob", embedded))
        c.commit()

    calls = {"n": 0}
    def fake_embed(text):
        calls["n"] += 1
        return [1.0, 0.0, 0.0]
    monkeypatch.setattr(pl, "_embed", fake_embed)
    monkeypatch.setattr(pl.me, "_blob_to_vec", lambda b: [1.0, 0.0, 0.0])
    monkeypatch.setattr(pl, "_cosine", lambda a, b: 0.9)

    results = pl.retrieve("a query", k=10000)
    ids = [d["id"] for d in results]
    assert embedded in ids            # the embedded row is still retrievable
    assert null_row not in ids        # the unembedded row is skipped, not inline-embedded
    assert calls["n"] == 1            # only the query was embedded — NO per-row inline embed
