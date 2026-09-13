"""Tests for the rerank-score floor (2026-07-31 MiniLM migration gate, Tier 2).

The cross-encoder's rerank_score is the one signal in the retrieval path that
actually knows a (query, memory) pair is junk — on noise queries ("better or
worse friend") the decayed-RRF scores pass MIN_SCORE trivially and the reranker
was reordering garbage, so garbage got injected. The floor drops candidates
whose rerank_score falls below a threshold BEFORE they leave search_hybrid.

Semantics under test:
  - rerank_floor=None (default)  -> behavior unchanged (backward compatible)
  - scored candidate below floor -> dropped
  - candidate WITHOUT rerank_score (model dead / fail-soft path) -> kept
  - everything below floor       -> [] (prefetch then injects nothing)
  - prefetch.main passes the flag value through to search_hybrid
"""

import io
import json
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import memory_engine as me
import prefetch
import reranker


class _FakeMem:
    def __init__(self, filename):
        self.filename = filename
        self.name = filename.replace(".md", "")
        self.description = f"description of {filename}"
        self.body = f"body of {filename}"
        self.weight = "medium"
        self.type = "feedback"
        self.path = Path("Z:/nonexistent") / filename  # decay stage fails soft


def _wire_retrieval(monkeypatch, filenames, scores_by_file, floor_gate=True):
    """Point search_hybrid's inputs at fakes and make the reranker attach
    controlled rerank_scores. Only rerank_enabled is on; PPR/hebbian stay off."""
    mems = [_FakeMem(fn) for fn in filenames]
    tf = [(m, 0.9 - i * 0.1, ["t"]) for i, m in enumerate(mems)]
    vec = [(m, 0.8 - i * 0.1) for i, m in enumerate(mems)]
    monkeypatch.setattr(me, "search", lambda *a, **k: tf)
    monkeypatch.setattr(me, "vector_search", lambda *a, **k: vec)

    import feature_flags

    monkeypatch.setattr(
        feature_flags, "is_enabled", lambda name: name == "rerank_enabled"
    )

    def fake_rerank(query, candidates, top_k=10):
        for c in candidates:
            if scores_by_file.get(c["filename"]) is not None:
                c["rerank_score"] = scores_by_file[c["filename"]]
        candidates.sort(
            key=lambda c: c.get("rerank_score", float("-inf")), reverse=True
        )
        return candidates[:top_k]

    monkeypatch.setattr(reranker, "rerank", fake_rerank)
    return mems


def test_hot_path_empty_vec_still_reaches_rerank_and_floor(monkeypatch):
    """The seven-week rot (2026-06-10 -> 2026-07-31): under MEMORY_HOT_PATH the
    embedder is skipped, vector_search returns [], and search_hybrid used to
    early-return raw TF-IDF — never reaching KG, the warm rerank daemon, or the
    floor. The daemon exists precisely to serve this path. Pin the fix: with
    rerank enabled, an empty vector lane must still fuse + rerank + floor."""
    mems = [_FakeMem(fn) for fn in ["good.md", "junk.md"]]
    tf = [(m, 0.9 - i * 0.1, ["t"]) for i, m in enumerate(mems)]
    monkeypatch.setattr(me, "search", lambda *a, **k: tf)
    monkeypatch.setattr(me, "vector_search", lambda *a, **k: [])  # hot path

    import feature_flags

    monkeypatch.setattr(
        feature_flags, "is_enabled", lambda name: name == "rerank_enabled"
    )

    def fake_rerank(query, candidates, top_k=10):
        for c in candidates:
            c["rerank_score"] = 3.0 if c["filename"] == "good.md" else -9.0
        candidates.sort(key=lambda c: -c["rerank_score"])
        return candidates[:top_k]

    monkeypatch.setattr(reranker, "rerank", fake_rerank)
    out = me.search_hybrid("q", mems=[], top_k=2, rerank_floor=-6.0)
    assert [m.filename for m, _, _ in out] == ["good.md"]


def test_no_floor_is_backward_compatible(monkeypatch):
    _wire_retrieval(
        monkeypatch,
        ["a.md", "b.md", "c.md"],
        {"a.md": 5.0, "b.md": -8.0, "c.md": -9.5},
    )
    out = me.search_hybrid("q", mems=[], top_k=3)
    assert [m.filename for m, _, _ in out] == ["a.md", "b.md", "c.md"]


def test_floor_drops_scored_candidates_below_it(monkeypatch):
    _wire_retrieval(
        monkeypatch,
        ["a.md", "b.md", "c.md"],
        {"a.md": 5.0, "b.md": -8.0, "c.md": -9.5},
    )
    out = me.search_hybrid("q", mems=[], top_k=3, rerank_floor=-2.0)
    assert [m.filename for m, _, _ in out] == ["a.md"]


def test_floor_keeps_unscored_candidates(monkeypatch):
    # Model-dead fail-soft: rerank returns candidates with NO rerank_score.
    # The floor must not turn "reranker unavailable" into "no results".
    _wire_retrieval(
        monkeypatch,
        ["a.md", "b.md"],
        {"a.md": None, "b.md": None},
    )
    out = me.search_hybrid("q", mems=[], top_k=2, rerank_floor=0.0)
    assert len(out) == 2


def test_floor_can_empty_the_result(monkeypatch):
    _wire_retrieval(
        monkeypatch,
        ["a.md", "b.md"],
        {"a.md": -7.0, "b.md": -9.0},
    )
    out = me.search_hybrid("noise query", mems=[], top_k=2, rerank_floor=-2.0)
    assert out == []


def test_prefetch_passes_flag_floor_through(monkeypatch):
    captured = {}

    def fake_search_hybrid(query, mems=None, top_k=8, **kwargs):
        captured.update(kwargs, query=query)
        return []

    monkeypatch.setattr(prefetch, "search_hybrid", fake_search_hybrid)
    monkeypatch.setattr(prefetch, "list_memories", lambda: [])
    monkeypatch.setattr(
        prefetch,
        "_observer_flag",
        lambda name, default: (-2.5 if name == "prefetch_rerank_floor" else default),
    )
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"prompt": "a genuine user prompt"}))
    )
    prefetch.main()
    assert captured.get("rerank_floor") == -2.5


def test_prefetch_floor_flag_absent_means_none(monkeypatch):
    captured = {}

    def fake_search_hybrid(query, mems=None, top_k=8, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(prefetch, "search_hybrid", fake_search_hybrid)
    monkeypatch.setattr(prefetch, "list_memories", lambda: [])
    monkeypatch.setattr(prefetch, "_observer_flag", lambda name, default: default)
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"prompt": "a genuine user prompt"}))
    )
    prefetch.main()
    assert captured.get("rerank_floor") is None
