"""Characterization tests for auto_promote._confidence_tier (recon Q1).

auto_promote.py is the one path whose flag flip (auto_promote_high_confidence_enabled)
lets the system write to the PERMANENT corpus without a human, and it had zero test
coverage. These pin the HIGH/MEDIUM/LOW tier boundaries so a future off-by-one
can't silently shift what becomes eligible for auto-promotion. Pure function
(dict + float in, tier string out) — no DB, no LLM.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import auto_promote as ap


def test_high_tier_at_exact_boundaries():
    # HIGH := size>=HC_MIN_CLUSTER(5) AND span>=HC_MIN_SPAN_DAYS(14) AND sim<=HC_MAX_EXISTING_SIM(0.50)
    assert ap._confidence_tier({"size": 5, "span_days": 14}, 0.50) == "HIGH"
    assert ap._confidence_tier({"size": 9, "span_days": 30}, 0.10) == "HIGH"


def test_just_below_high_falls_to_medium():
    assert ap._confidence_tier({"size": 4, "span_days": 14}, 0.50) == "MEDIUM"  # size < 5
    assert ap._confidence_tier({"size": 5, "span_days": 13}, 0.50) == "MEDIUM"  # span < 14
    assert ap._confidence_tier({"size": 5, "span_days": 14}, 0.51) == "MEDIUM"  # sim > 0.50


def test_medium_tier_at_exact_boundaries():
    # MEDIUM := size>=MIN_CLUSTER_SIZE(3) AND span>=MIN_TIME_SPAN_DAYS(7)
    assert ap._confidence_tier({"size": 3, "span_days": 7}, 0.99) == "MEDIUM"


def test_low_tier_below_medium():
    assert ap._confidence_tier({"size": 2, "span_days": 7}, 0.0) == "LOW"   # size < 3
    assert ap._confidence_tier({"size": 3, "span_days": 6}, 0.0) == "LOW"   # span < 7


def test_tier_thresholds_have_not_drifted():
    # Lock the live constants the tiers depend on — a silent change here moves the
    # permanent-corpus eligibility bar.
    assert (ap.HC_MIN_CLUSTER, ap.HC_MIN_SPAN_DAYS, ap.HC_MAX_EXISTING_SIM) == (5, 14, 0.50)
    assert (ap.MIN_CLUSTER_SIZE, ap.MIN_TIME_SPAN_DAYS) == (3, 7)
    assert (ap.CLUSTER_SIM_THRESHOLD, ap.SIMILAR_EXISTING_THRESHOLD) == (0.85, 0.88)
