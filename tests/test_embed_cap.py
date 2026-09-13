"""TDD: the recall query embed path must share the write path's input cap.

Write path caps body at a constant (was the literal 1500 at :460); the query
path (vector_search -> embed_text) was uncapped, so a pathological prompt+HyDE
concatenation got silently truncated by the model. One shared constant guards
both sides so they cannot drift.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import memory_engine as me


def test_shared_cap_constant_exists():
    assert hasattr(me, "EMBED_INPUT_CHARS")
    assert me.EMBED_INPUT_CHARS == 1500  # preserve existing write behavior


def test_write_path_uses_shared_cap():
    class M:
        name, description = "N", "D"
        body = "b" * 99999
    txt = me._memory_text_for_embedding(M())
    assert txt.count("b") == me.EMBED_INPUT_CHARS  # body capped at the constant


def test_query_path_caps_input(monkeypatch):
    captured = {}

    def fake_embed(text):
        captured["len"] = len(text)
        return [0.0] * 384

    monkeypatch.setattr(me, "embed_text", fake_embed)
    me.vector_search("x" * 10000, mems=[])  # empty corpus: returns after embed
    assert captured["len"] <= me.EMBED_INPUT_CHARS
