"""TDD: observer->spine promotion lane needs a trigger + boot visibility.

80 observations are referenced yet 0 of 4000+ have ever graduated to the spine
(promoted_to_memory_id NULL for all). The CLI lane exists; we add programmatic
generation (idempotent, so a consolidate trigger can't flood), a status summary,
and boot surfacing. Candidates-only — never auto-promotes to the corpus.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import promotion_candidate_generator as pcg


def test_run_generation_returns_summary_and_is_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(pcg, "CANDIDATES_DIR", tmp_path)
    r1 = pcg.run_generation()
    assert "clusters" in r1 and "written" in r1
    assert isinstance(r1["written"], list)
    # second run must not re-dump candidates for sessions already pending
    r2 = pcg.run_generation()
    assert r2["written"] == [], "run_generation re-generated existing candidates"


def test_eligible_summary_shape():
    s = pcg.eligible_summary()
    for k in ("total", "eligible", "promoted", "pending_files"):
        assert k in s and isinstance(s[k], int)


def test_boot_surfaces_eligible_observations(monkeypatch):
    import boot_ritual
    monkeypatch.setattr(pcg, "eligible_summary",
                        lambda: {"total": 100, "eligible": 12, "promoted": 0, "pending_files": 3})
    lines = boot_ritual.observer_promotion_lines()
    assert any("12" in l for l in lines), "eligible observation count not surfaced"


def test_boot_observer_silent_when_nothing_pending(monkeypatch):
    import boot_ritual
    monkeypatch.setattr(pcg, "eligible_summary",
                        lambda: {"total": 100, "eligible": 0, "promoted": 5, "pending_files": 0})
    assert boot_ritual.observer_promotion_lines() == []
