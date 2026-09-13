"""TDD: read-gate fidelity.

The gate DENIES a Read and injects a tiny preview in place of the file. Live:
545 denials, top files (orchestrator.py, tools.py, AGENT_BUILDING_LESSONS.md)
served as ~462-char header previews. Fix (conservative, keep deny):
  - size-guard: files larger than MAX_GATED_BYTES are never gated (a short
    preview can't represent them) -> always a real read.
  - richer preview for the small files still gated.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import read_gate
import read_gate_record

NOW = 1_700_000_000


def _obs(captured=NOW, mtime=1000.0):
    return {"summary": "x", "captured_at": captured, "file_mtime_at_capture": mtime}


def test_gate_denies_small_fresh_cached_file():
    assert read_gate.gate_decision(size=4000, obs=_obs(), current_mtime=1000.0, now=NOW) is True


def test_gate_allows_large_file_even_when_cached():
    big = read_gate.MAX_GATED_BYTES + 1
    assert read_gate.gate_decision(size=big, obs=_obs(), current_mtime=1000.0, now=NOW) is False


def test_gate_allows_when_file_changed():
    assert read_gate.gate_decision(size=4000, obs=_obs(mtime=1000.0), current_mtime=2000.0, now=NOW) is False


def test_gate_allows_when_no_cache():
    assert read_gate.gate_decision(size=4000, obs=None, current_mtime=1000.0, now=NOW) is False


def test_gate_allows_when_ttl_expired():
    old = NOW - (read_gate.DEFAULT_TTL_HOURS * 3600) - 10
    assert read_gate.gate_decision(size=4000, obs=_obs(captured=old), current_mtime=1000.0, now=NOW) is False


def test_preview_is_richer_than_old_400_cap():
    text = "some content line\n" * 2000  # ~34KB
    prev = read_gate_record.build_preview(text, size=len(text))
    assert 400 < len(prev) <= read_gate_record.MAX_PREVIEW_CHARS + 220
