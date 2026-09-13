"""TDD: don't silently lose a distilled insight on an LLM JSON parse failure.

_reflection_synthesis_pass returns {error} on parse failure and the importance
accumulator was already reset for that window, so the insight is gone with no
record. Persist the raw LLM text to a _failed/ dir for human salvage.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import consolidate_worker as cw


def test_persist_failed_synthesis_writes_raw(tmp_path, monkeypatch):
    monkeypatch.setattr(cw, "REFLECTION_CANDIDATES_DIR", tmp_path)
    raw = '{"insights": [ truncated nonsense that will not parse'
    p = cw._persist_failed_synthesis(raw, kind="reflection")
    assert p.exists()
    assert raw in p.read_text(encoding="utf-8")
    assert p.parent.name == "_failed"
