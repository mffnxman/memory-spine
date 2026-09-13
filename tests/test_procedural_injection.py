"""TDD P4: injection — recall lane + boot 'Learned habits' section.

Heuristics surface two ways, both behind procedural_injection_enabled (default
OFF -> zero runtime impact until we flip it after eval):
  - recall lane: top-k trigger-matched heuristics for the CURRENT prompt
    (prefetch.py / UserPromptSubmit path)
  - boot section: top global heuristics by standing (boot_ritual.py)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import procedural_lib as pl
import memory_engine as me

PY = {"trigger": "running python3 on Windows hits a UnicodeEncodeError on output",
      "action": "set PYTHONIOENCODING=utf-8 before the command", "insight": ""}


def _reset():
    pl.ensure_schema()
    with me.db() as c:
        c.execute("DELETE FROM heuristics")
        c.commit()


def test_format_for_injection_renders_trigger_action():
    block = pl.format_for_injection(
        [{"trigger": "When X happens", "action": "do Y", "score": 1.0}], header="learned habits")
    assert "learned habits" in block
    assert "When X happens" in block and "do Y" in block


def test_recall_lane_empty_when_flag_off(monkeypatch):
    _reset()
    pl.upsert_heuristic(PY, origin="test", polarity="failure_derived")
    monkeypatch.setattr(pl, "_enabled", lambda name: False)
    assert pl.recall_lane("UnicodeEncodeError running python") == ""


def test_recall_lane_surfaces_relevant_heuristic_when_on(monkeypatch):
    _reset()
    pl.upsert_heuristic(PY, origin="test", polarity="failure_derived")
    monkeypatch.setattr(pl, "_enabled", lambda name: True)
    block = pl.recall_lane("I keep getting a UnicodeEncodeError when running a python script")
    assert "PYTHONIOENCODING" in block


def test_recall_lane_silent_on_irrelevant_prompt(monkeypatch):
    _reset()
    pl.upsert_heuristic(PY, origin="test", polarity="failure_derived")
    monkeypatch.setattr(pl, "_enabled", lambda name: True)
    assert pl.recall_lane("what is the capital of France") == ""


def test_boot_section_off_then_on(monkeypatch):
    _reset()
    pl.upsert_heuristic(PY, origin="test", polarity="failure_derived")
    monkeypatch.setattr(pl, "_enabled", lambda name: False)
    assert pl.boot_section() == ""
    monkeypatch.setattr(pl, "_enabled", lambda name: True)
    assert "PYTHONIOENCODING" in pl.boot_section()
