"""TDD guard: reranker pair construction must always emit (str, str) tuples.

Regression (14 occurrences in _meta/v3_2_telemetry.jsonl, last 2026-06-01):
a candidate whose 'body' (or name/description) was a non-str value (int, list,
or a stray None survivor) reached sentence_transformers.CrossEncoder.predict()
and raised "TextInputSequence must be str". The except-clause in rerank()
swallowed it and returned candidates[:top_k] unchanged — silently dropping the
ENTIRE query back to RRF order with zero rerank lift and no user signal.

The fix extracts pair-building into _build_pairs() and coerces every field to
str() so predict() can never receive a non-string. These tests pin that.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import reranker


def test_build_pairs_coerces_nonstring_body_to_str():
    pairs = reranker._build_pairs("q", [{"filename": "a.md", "body": 12345}])
    assert len(pairs) == 1
    query, text = pairs[0]
    assert isinstance(query, str)
    assert isinstance(text, str)
    assert "12345" in text


def test_build_pairs_handles_none_list_and_missing_fields():
    candidates = [
        {"filename": "none.md", "body": None},
        {"filename": "list.md", "body": ["x", "y"]},
        {"filename": "missing.md"},  # no body key at all
        {"filename": "n.md", "body": "ok", "name": 7, "description": None},
    ]
    pairs = reranker._build_pairs("q", candidates)
    assert len(pairs) == len(candidates)
    for query, text in pairs:
        assert isinstance(query, str), f"query not str: {query!r}"
        assert isinstance(text, str), f"text not str: {text!r}"


def test_build_pairs_truncates_long_body():
    long_body = "a" * (reranker.BODY_MAX_CHARS + 500)
    pairs = reranker._build_pairs("q", [{"filename": "big.md", "body": long_body}])
    _, text = pairs[0]
    # body portion capped at BODY_MAX_CHARS (small slack for any prefix/newline)
    assert len(text) <= reranker.BODY_MAX_CHARS + 64


def test_build_pairs_frontloads_name_and_description_before_body():
    pairs = reranker._build_pairs(
        "q", [{"filename": "x.md", "name": "TITLE", "description": "DESC", "body": "BODY"}]
    )
    _, text = pairs[0]
    assert text.index("TITLE") < text.index("BODY")
    assert "DESC" in text


def test_build_pairs_guarantees_nonempty_strings():
    """Empty/blank query and an all-empty candidate must still yield non-empty
    str pair elements — tokenizers is happier and ST's is_singular_input() is
    stable. (Closes the residual TextInputSequence / TextEncodeInput path that
    str()-coercion alone left open: an empty '' is what destabilises the batch.)
    """
    pairs = reranker._build_pairs("", [{"filename": "e.md", "body": ""}])
    assert len(pairs) == 1
    q, text = pairs[0]
    assert isinstance(q, str) and isinstance(text, str)
    assert q != "", "blank query must be normalised away from empty"
    assert text != "", "empty doc must be normalised away from empty"


def test_build_pairs_blank_whitespace_fields_normalised():
    # Whitespace-only query/body coerces to a single space, never the empty string.
    pairs = reranker._build_pairs("   ", [{"filename": "w.md", "body": "   "}])
    q, text = pairs[0]
    assert q.strip() == "" and q != ""
    assert text.strip() == "" and text != ""


# --- rerank_model flag override (2026-07-31 MiniLM-CPU migration) -----------
# The model name must be swappable via feature_flags ("rerank_model") without
# editing code, and the actually-loaded name must be stamped for honest
# /health + CLI-status reporting.

def test_resolve_model_name_defaults_to_constant():
    assert reranker._resolve_model_name({}) == reranker.MODEL


def test_resolve_model_name_flag_override_stripped():
    assert reranker._resolve_model_name({"rerank_model": "  org/model-x  "}) == "org/model-x"


def test_resolve_model_name_rejects_nonstring_and_blank():
    # A mistyped flags file must never brick rerank with an unloadable name.
    assert reranker._resolve_model_name({"rerank_model": 42}) == reranker.MODEL
    assert reranker._resolve_model_name({"rerank_model": ""}) == reranker.MODEL
    assert reranker._resolve_model_name({"rerank_model": "   "}) == reranker.MODEL


def test_get_reranker_constructs_flag_model_and_stamps_loaded(monkeypatch):
    constructed = {}

    class FakeCE:
        def __init__(self, name, **kwargs):
            constructed["name"] = name

    import feature_flags
    monkeypatch.setattr(feature_flags, "all_flags", lambda: {"rerank_model": "fake/model-x"})
    # rerank_on_cpu on → device path never touches torch/CUDA in this test
    monkeypatch.setattr(feature_flags, "is_enabled", lambda name: name == "rerank_on_cpu")
    monkeypatch.setattr(reranker, "_import_cross_encoder", lambda: FakeCE)
    monkeypatch.setattr(reranker, "_ST_AVAILABLE", True)
    monkeypatch.setattr(reranker, "_reranker", None)
    monkeypatch.setattr(reranker, "_load_failed", False)
    monkeypatch.setattr(reranker, "LOADED_MODEL", None)

    model = reranker._get_reranker()
    assert model is not None
    assert constructed["name"] == "fake/model-x"
    assert reranker.LOADED_MODEL == "fake/model-x"


# -- silent-exit telemetry (hotpath-floor-bypass investigation 2026-08-13) ----
# The sentinel pairs vec_empty_fused with the next rerank* event; 13 of 21
# flagged "bypasses" in the trailing week had NO rerank event at all because
# three rerank() exits returned without logging. A bypass you can't see is a
# bypass you can't fix — every exit must say what happened.


def _cap_telemetry(monkeypatch):
    events = []
    monkeypatch.setattr(reranker, "_log_telemetry", lambda e: events.append(e))
    return events


def test_rerank_empty_candidates_emits_ok_with_empty_pool(monkeypatch):
    """Empty pool = nothing injected = nothing for the floor to protect.
    Emitting rerank_ok (not _failed) stops the sentinel counting these as
    bypasses — they were false positives inflating the rate."""
    events = _cap_telemetry(monkeypatch)
    assert reranker.rerank("q", [], top_k=3) == []
    assert any(
        e.get("event") == "rerank_ok" and e.get("empty_pool") for e in events
    )


def test_rerank_circuit_breaker_emits_failed(monkeypatch):
    """A circuit-breaker skip IS a real floor bypass (candidates injected
    unfloored) — it must be visible, with its reason."""
    events = _cap_telemetry(monkeypatch)
    monkeypatch.setattr(reranker, "_daemon_enabled", lambda: False)
    monkeypatch.setattr(reranker, "_load_failed_recently", lambda: True)
    out = reranker.rerank("q", [{"filename": "a", "body": "x"}], top_k=3)
    assert len(out) == 1  # candidates still returned unchanged (fail-soft)
    assert any(
        e.get("event") == "rerank_failed" and e.get("reason") == "circuit_breaker"
        for e in events
    )


def test_rerank_model_unavailable_emits_failed(monkeypatch):
    events = _cap_telemetry(monkeypatch)
    monkeypatch.setattr(reranker, "_daemon_enabled", lambda: False)
    monkeypatch.setattr(reranker, "_load_failed_recently", lambda: False)
    monkeypatch.setattr(reranker, "_get_reranker", lambda: None)
    monkeypatch.setattr(reranker, "_stamp_load_failed", lambda: None)
    out = reranker.rerank("q", [{"filename": "a", "body": "x"}], top_k=3)
    assert len(out) == 1
    assert any(
        e.get("event") == "rerank_failed" and e.get("reason") == "model_unavailable"
        for e in events
    )
