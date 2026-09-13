"""TDD P0: procedural (L2) memory layer — schema scaffold.

The heuristics table lives in memory.db beside embeddings/entities/relationships
so it reuses the embedding + decay + bitemporal machinery. ensure_schema()
self-heals it on connect (fresh-install safety; mirrors the kg.db pattern).
The three procedural feature flags are registered and default OFF (dark ship).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import paths as _paths  # noqa: E402
import procedural_lib as pl
import memory_engine as me

EXPECTED_COLUMNS = {
    "id", "trigger", "action", "insight", "origin", "polarity",
    "importance", "corroboration", "use_count", "source_session",
    "source_obs_ids", "source_refs", "created_ts", "last_used_ts",
    "last_validated_ts", "valid_to", "superseded_by", "status",
}

FLAGS_PATH = _paths.META_DIR / "feature_flags.json"
PROCEDURAL_FLAGS = (
    "procedural_extraction_enabled",
    "procedural_injection_enabled",
    "procedural_promotion_enabled",
)


def test_ensure_schema_creates_heuristics_table():
    pl.ensure_schema()
    with me.db() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(heuristics)")}
    assert EXPECTED_COLUMNS <= cols, f"missing: {EXPECTED_COLUMNS - cols}"


def test_ensure_schema_is_idempotent():
    pl.ensure_schema()
    pl.ensure_schema()  # second call must not raise
    with me.db() as c:
        n = c.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='heuristics'"
        ).fetchone()[0]
    assert n == 1


def test_procedural_flags_registered():
    # All three procedural flags must stay registered + boolean. (extraction +
    # injection went live after the eval harness passed; promotion is held OFF
    # for the Phase-7 L2->L3 spine-write diagonal — the one flag that mutates the
    # durable spine, so it stays gated until that work lands and is reviewed.)
    flags = json.loads(FLAGS_PATH.read_text(encoding="utf-8"))
    for flag in PROCEDURAL_FLAGS:
        assert flag in flags, f"{flag} not registered in feature_flags.json"
        assert isinstance(flags[flag], bool), f"{flag} must be boolean"
    assert flags["procedural_promotion_enabled"] is False, \
        "promotion (L2->L3 spine writes) stays gated until Phase 7"
