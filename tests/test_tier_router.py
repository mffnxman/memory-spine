"""Characterization tests for tier_router.route() (recon Q3).

route() is the single dispatcher deciding provider/model/params for every LLM
workload across 7 modules, yet had zero coverage — a wrong routing-table entry
(mechanical job -> Opus, or synthesis -> Haiku) would fan out silently. These pin
the table mapping, the unknown->default fallback, and the Haiku escalation. Local
and subscription routing are bypassed (allow_local=False + disable env) so the
base table behavior is deterministic and offline.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tier_router as tr


@pytest.mark.parametrize("task_type,expected_model", [
    ("entity_extraction",     "claude-haiku-4-5-20251001"),
    ("dedup_similarity",      "claude-haiku-4-5-20251001"),
    ("hyde_expansion",        "claude-haiku-4-5-20251001"),
    ("importance_scoring",    "claude-haiku-4-5-20251001"),
    ("memory_classification", "claude-sonnet-5"),
    ("probe_scoring",         "claude-sonnet-5"),
    ("multi_doc_synthesis",   "claude-opus-4-8"),
    ("epilogue_generation",   "claude-opus-4-8"),
    ("weekly_digest_chapter", "claude-opus-4-8"),
])
def test_route_maps_known_task_types_to_expected_tier(task_type, expected_model, monkeypatch):
    monkeypatch.setenv("MEMORY_DISABLE_SUBSCRIPTION_PROVIDER", "1")  # bypass subscription fallback
    _prov, model, _params = tr.route(task_type, allow_local=False)
    assert model == expected_model


def test_route_unknown_task_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("MEMORY_DISABLE_SUBSCRIPTION_PROVIDER", "1")
    _prov, model, _params = tr.route("totally_unknown_task_xyz", allow_local=False)
    assert model == tr.TIER_TABLE["default"]["model"] == "claude-sonnet-5"


def test_route_haiku_escalates_to_sonnet_on_huge_input(monkeypatch):
    monkeypatch.setenv("MEMORY_DISABLE_SUBSCRIPTION_PROVIDER", "1")
    big = int(tr.HAIKU_CONTEXT_LIMIT * 0.95)
    _prov, model, _params = tr.route("entity_extraction", input_tokens=big, allow_local=False)
    assert model == "claude-sonnet-5"  # escalated off Haiku for oversize context
