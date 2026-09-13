"""Characterization test for search_hybrid's no-embeddings fallback (recon Q4).

When vector_search yields nothing (no embeddings available / not yet indexed),
search_hybrid must fall back to the TF-IDF ranking — returning those hits ordered
and truncated, NOT an empty list. The existing telemetry test only checks that a
breadcrumb fires; this pins the actual degraded OUTPUT so a regression that drops
results stays caught even when telemetry still emits.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import memory_engine as me


def test_search_hybrid_returns_tfidf_when_no_embeddings(monkeypatch):
    # The fallback branch returns tf[:top_k] without inspecting the tuples, so
    # opaque sentinels suffice and keep the test free of DB/embedding dependencies.
    # Scoped to rerank OFF since 2026-07-31: with rerank ON, an empty vector lane
    # now proceeds into fusion so the warm daemon + floor still apply (the
    # seven-week TF-IDF-only rot) — that path is pinned in test_prefetch_floor.
    import feature_flags

    monkeypatch.setattr(feature_flags, "is_enabled", lambda name: False)
    tf_hits = [("m1", 0.9, ["x"]), ("m2", 0.5, ["y"]), ("m3", 0.1, [])]
    monkeypatch.setattr(me, "search", lambda *a, **k: tf_hits)
    monkeypatch.setattr(me, "vector_search", lambda *a, **k: [])  # no embeddings
    out = me.search_hybrid("q", mems=[], top_k=2)
    assert out == tf_hits[:2]  # TF-IDF fallback, ordered + truncated — NOT []


def test_search_hybrid_empty_when_no_tfidf_and_no_vectors(monkeypatch):
    monkeypatch.setattr(me, "search", lambda *a, **k: [])
    monkeypatch.setattr(me, "vector_search", lambda *a, **k: [])
    assert me.search_hybrid("q", mems=[], top_k=5) == []  # genuine no-results case
