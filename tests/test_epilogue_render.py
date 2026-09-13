"""TDD: epilogue list fields must render as markdown bullets, not Python reprs.

A /epilogue payload passes what_we_did / what_surprised / open_threads as JSON
arrays; write_epilogue used to str() them straight into the template, producing
an ugly ["a", "b"] literal in the durable continuity artifact.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import epilogue as ep


def test_list_field_renders_as_bullets():
    out = ep._as_text(["first thing", "second thing"])
    assert out == "- first thing\n- second thing"
    assert "[" not in out  # no Python list repr leaking into the epilogue


def test_string_field_passes_through():
    assert ep._as_text("a prose paragraph") == "a prose paragraph"


def test_empty_list_is_not_captured():
    assert ep._as_text([]) == "(not captured)"
