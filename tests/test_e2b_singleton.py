"""e2b_daemon singleton lock — the 2026-07-13 triple-spawn regression.

Three provider calls 20s apart at session boot each passed the /health
pre-check (llama-server binds :8766 only after 30s+ of model load) and
spawned a full copy of the E2B weights — 3x in VRAM, dart2's daily tier
starved out ("nothing fits: free VRAM 1035MB"). The fix is a two-part guard
mirroring rerank_daemon: /health pre-check plus a delete-on-close lock file
held for the supervisor's lifetime. These tests cover the lock semantics
WITHOUT spawning llama-server (too heavy for unit tests).
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import e2b_daemon


@pytest.fixture
def lock_path(tmp_path, monkeypatch):
    path = tmp_path / ".e2b_daemon.lock"
    monkeypatch.setattr(e2b_daemon, "LOCK", path)
    return path


def test_lock_is_exclusive_while_held(lock_path):
    fd = e2b_daemon._acquire_lock()
    assert fd is not None
    try:
        assert e2b_daemon._acquire_lock() is None  # second supervisor loses
    finally:
        os.close(fd)


def test_lock_self_cleans_on_release(lock_path):
    fd = e2b_daemon._acquire_lock()
    assert lock_path.exists()
    os.close(fd)  # stands in for process exit/death — O_TEMPORARY delete-on-close
    assert not lock_path.exists()
    fd2 = e2b_daemon._acquire_lock()  # respawn after holder death must succeed
    assert fd2 is not None
    os.close(fd2)


def test_main_declines_to_spawn_when_lock_held(lock_path, monkeypatch):
    """The exact triple-spawn shape: health says nothing is serving (mid-load),
    but another supervisor already owns the lock — main must exit 0, no Popen."""
    monkeypatch.setattr(e2b_daemon, "_health", lambda *a, **k: False)

    def _no_spawn(*a, **k):
        raise AssertionError("spawned llama-server despite held lock")

    monkeypatch.setattr(e2b_daemon.subprocess, "Popen", _no_spawn)
    fd = e2b_daemon._acquire_lock()
    try:
        assert e2b_daemon.main() == 0
    finally:
        os.close(fd)
