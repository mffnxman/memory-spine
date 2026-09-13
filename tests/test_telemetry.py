"""TDD: silent fail-soft branches must leave a telemetry breadcrumb.

search_hybrid wraps KG-PPR / decay / rerank in bare `except: pass`, and the
query-embed cap truncates silently. A broken neighbor (esp. decay, which self-
logs nothing) reverts recall to plain RRF with zero signal. We keep the fail-
soft contract but record that it fired.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import memory_engine as me


def test_query_truncation_is_logged(monkeypatch, tmp_path):
    monkeypatch.setattr(me, "TELEMETRY_PATH", tmp_path / "t.jsonl")
    monkeypatch.setattr(me, "embed_text", lambda t: [0.0] * 384)
    me.vector_search("x" * 10000, mems=[])
    data = (tmp_path / "t.jsonl").read_text(encoding="utf-8")
    assert "query_embed" in data and "truncated" in data


def test_decay_failure_is_logged(monkeypatch, tmp_path):
    import decay
    monkeypatch.setattr(me, "TELEMETRY_PATH", tmp_path / "t.jsonl")

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(decay, "apply_decay", boom)
    mems = me.list_memories()
    # must NOT raise — fail-soft contract preserved
    me.search_hybrid("anything", mems=mems, top_k=3)
    path = tmp_path / "t.jsonl"
    data = path.read_text(encoding="utf-8") if path.exists() else ""
    assert "decay" in data and "fail_soft" in data


def test_rerank_fail_soft_event_name_visible_to_sentinel(tmp_path, monkeypatch):
    """search_hybrid's rerank except-path used to log event='fail_soft' —
    invisible to the sentinel's startswith('rerank') pairing, so every such
    prompt counted as a mystery NO_RERANK_EVENT bypass. The event must be
    'rerank_fail_soft': paired AND counted as a bypass (it is one), with
    the error preserved."""
    import feature_flags
    import reranker as rr

    monkeypatch.setattr(me, "TELEMETRY_PATH", tmp_path / "t.jsonl")
    monkeypatch.setattr(
        feature_flags, "is_enabled", lambda name: name == "rerank_enabled"
    )

    def boom(*a, **k):
        raise RuntimeError("rerank exploded")

    monkeypatch.setattr(rr, "rerank", boom)
    mems = me.list_memories()
    me.search_hybrid("anything", mems=mems, top_k=3)  # must NOT raise
    data = (tmp_path / "t.jsonl").read_text(encoding="utf-8")
    assert "rerank_fail_soft" in data
    assert "rerank exploded" in data
