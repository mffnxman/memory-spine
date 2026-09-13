"""Reranker warm-sidecar daemon — client routing + daemon pure logic.

Theme A of the 2026-06-03 brain recon: bge-reranker-v2-m3 cold-loads its
~600MB-1.1GB model on every UserPromptSubmit because each hook is a fresh OS
process (551 reloads in 6.3 days, p50=8.9s/load, 16-17s cold prefetch). The fix
is a resident localhost daemon that loads the model once; reranker.rerank()
POSTs to it and falls back to in-process load only when the daemon is down.

These tests cover the routing/fallback logic and the daemon's pure helpers
WITHOUT loading the real model (too heavy for unit tests) — the real load +
end-to-end is verified manually.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import reranker
import rerank_daemon


# ---- shared scoring core (used by BOTH in-process and the daemon) ----

class _StubModel:
    """Returns a fixed score per pair so ordering is deterministic."""
    def __init__(self, scores):
        self._scores = scores
    def predict(self, pairs, **kw):
        return self._scores


def test_rerank_with_model_orders_by_score_and_truncates():
    cands = [
        {"filename": "a.md", "body": "alpha"},
        {"filename": "b.md", "body": "bravo"},
        {"filename": "c.md", "body": "charlie"},
    ]
    model = _StubModel([0.1, 0.9, 0.5])  # b > c > a
    out = reranker._rerank_with_model(model, "q", cands, top_k=2)
    assert [c["filename"] for c in out] == ["b.md", "c.md"]
    assert out[0]["rerank_score"] == 0.9
    assert len(out) == 2


def test_rerank_with_model_falls_back_on_shape_mismatch():
    cands = [{"filename": "a.md", "body": "x"}, {"filename": "b.md", "body": "y"}]
    model = _StubModel([0.5])  # wrong length → must not silently mis-zip
    out = reranker._rerank_with_model(model, "q", cands, top_k=5)
    assert out == cands[:5]  # unchanged order, no rerank_score added


def test_rerank_with_model_handles_singular_scalar_output():
    """ST 5.x returns a scalar / 0-d (a bare float here) when it judges the input
    'singular'. For a single candidate that's the CORRECT length-1 case: score it,
    don't crash on len(scalar) — the bug behind 'numpy.float32 has no len()'."""
    cands = [{"filename": "a.md", "body": "only"}]
    model = _StubModel(0.7)  # bare scalar, not a list (simulates 0-d collapse)
    out = reranker._rerank_with_model(model, "q", cands, top_k=5)
    assert len(out) == 1
    assert out[0]["rerank_score"] == 0.7


def test_rerank_with_model_scalar_with_multi_candidates_soft_falls_back():
    """A scalar return with >1 candidate is a genuine batch collapse: must fail
    soft to RRF order WITHOUT raising 'object of type numpy.float32 has no len()'."""
    cands = [{"filename": "a.md", "body": "x"}, {"filename": "b.md", "body": "y"}]
    model = _StubModel(0.5)  # scalar, but 2 candidates
    out = reranker._rerank_with_model(model, "q", cands, top_k=5)
    assert out == cands[:5]              # unchanged order
    assert "rerank_score" not in out[0]  # nothing scored


# ---- rerank() routing: prefer the daemon, fall back in-process ----

def test_rerank_uses_daemon_result_when_available(monkeypatch):
    cands = [{"filename": "a.md", "body": "x"}]
    sentinel = [{"filename": "a.md", "body": "x", "rerank_score": 0.42}]
    monkeypatch.setattr(reranker, "_daemon_enabled", lambda: True)
    monkeypatch.setattr(reranker, "_rerank_via_daemon", lambda q, c, k: sentinel)
    # If the daemon answered, we must NOT cold-load in-process.
    monkeypatch.setattr(reranker, "_get_reranker",
                        lambda: (_ for _ in ()).throw(AssertionError("cold-loaded despite warm daemon")))
    out = reranker.rerank("q", cands, top_k=5)
    assert out is sentinel


def test_rerank_falls_back_in_process_and_spawns_daemon_when_down(monkeypatch):
    cands = [{"filename": "a.md", "body": "x"}, {"filename": "b.md", "body": "y"}]
    spawned = []
    monkeypatch.setattr(reranker, "_daemon_enabled", lambda: True)
    monkeypatch.setattr(reranker, "_rerank_via_daemon", lambda q, c, k: None)  # daemon down
    monkeypatch.setattr(reranker, "_ensure_daemon_spawned", lambda: spawned.append(True))
    monkeypatch.setattr(reranker, "_get_reranker", lambda: _StubModel([0.2, 0.8]))
    out = reranker.rerank("q", cands, top_k=5)
    assert [c["filename"] for c in out] == ["b.md", "a.md"], "in-process scoring should run"
    assert spawned == [True], "a down daemon should be (re)spawned for next time"


def test_rerank_empty_candidates_short_circuits(monkeypatch):
    # Must not touch the daemon or the model for an empty pool.
    monkeypatch.setattr(reranker, "_rerank_via_daemon",
                        lambda q, c, k: (_ for _ in ()).throw(AssertionError("called for empty")))
    assert reranker.rerank("q", [], top_k=5) == []


def test_rerank_via_daemon_returns_none_when_unreachable(monkeypatch):
    # Point at a closed port; the client must fail soft to None (→ fallback),
    # never raise.
    monkeypatch.setattr(reranker, "_daemon_base_url", lambda: "http://127.0.0.1:9")  # discard port
    out = reranker._rerank_via_daemon("q", [{"filename": "a.md", "body": "x"}], 5)
    assert out is None


# ---- daemon pure helpers ----

def test_process_rerank_request_uses_scorer():
    payload = {"query": "q", "candidates": [{"filename": "a.md", "body": "x"}], "top_k": 3}
    called = {}
    def scorer(query, candidates, top_k):
        called.update(query=query, top_k=top_k, n=len(candidates))
        return [{"filename": "a.md", "body": "x", "rerank_score": 1.0}]
    status, resp = rerank_daemon.process_rerank_request(payload, scorer)
    assert status == 200
    assert resp["candidates"][0]["rerank_score"] == 1.0
    assert called == {"query": "q", "top_k": 3, "n": 1}


def test_process_rerank_request_rejects_malformed_payload():
    status, resp = rerank_daemon.process_rerank_request({"not": "valid"}, scorer=lambda *a: [])
    assert status == 400
    assert "error" in resp


def test_should_evict_after_idle_timeout():
    # No request for longer than the idle window → evict (free VRAM).
    assert rerank_daemon._should_evict(last_request_ts=100.0, now=100.0 + 1801, idle_timeout=1800) is True
    # Recent request → stay resident.
    assert rerank_daemon._should_evict(last_request_ts=100.0, now=100.0 + 5, idle_timeout=1800) is False


# ---- singleton guard (Windows SO_REUSEADDR makes port-bind alone unreliable) ----

def test_already_running_false_on_dead_port(monkeypatch):
    # No daemon listening → must report not-running (so this process becomes it).
    monkeypatch.setattr(reranker, "_daemon_base_url", lambda: "http://127.0.0.1:9")
    assert rerank_daemon._already_running(timeout=0.5) is False


def test_already_running_true_when_health_responds(monkeypatch):
    # A sibling daemon answers /health → this process must decline (avoid a
    # second resident model; on Windows the port bind would otherwise succeed).
    monkeypatch.setattr(rerank_daemon, "_daemon_health", lambda timeout=1.0: {"ok": True})
    assert rerank_daemon._already_running() is True


def test_server_does_not_allow_addr_reuse():
    # allow_reuse_address must be False so a 2nd bind to the same port FAILS on
    # Windows (SO_REUSEADDR there permits same-port rebinds) — the bind is the
    # race-safe half of the singleton mutex.
    assert rerank_daemon._Server.allow_reuse_address is False


# ---- adversarial-review fixes (2026-06-03) ----

def test_rerank_via_daemon_fails_fast_when_down(monkeypatch):
    """Regression guard for the headline review finding: a down/warming daemon
    must NOT make the prefetch hook wait the read timeout. Closed port → None
    in well under a second (the bug this whole session exists to kill)."""
    monkeypatch.setattr(reranker, "_daemon_base_url", lambda: "http://127.0.0.1:9")
    t = time.time()
    out = reranker._rerank_via_daemon("q", [{"filename": "a.md", "body": "x"}], 5)
    dt = time.time() - t
    assert out is None
    assert dt < 1.0, f"down-daemon probe took {dt:.2f}s — would block the prefetch hook"


def test_read_timeout_parses_safely(monkeypatch):
    monkeypatch.setenv("RERANK_DAEMON_TIMEOUT", "garbage")
    assert reranker._read_timeout() == 1.5  # bad value can't crash `import reranker`
    monkeypatch.setenv("RERANK_DAEMON_TIMEOUT", "2.5")
    assert reranker._read_timeout() == 2.5


def test_daemon_serializes_concurrent_inference(monkeypatch):
    """ThreadingHTTPServer runs predict() per-thread against ONE shared torch
    model — _scorer must serialize so concurrent forward passes never overlap
    (PyTorch doesn't guarantee thread-safe concurrent forward())."""
    import threading
    state = {"cur": 0, "max": 0}
    lk = threading.Lock()

    class _Slow:
        def predict(self, pairs, **k):
            with lk:
                state["cur"] += 1
                state["max"] = max(state["max"], state["cur"])
            time.sleep(0.05)
            with lk:
                state["cur"] -= 1
            return [0.5] * len(pairs)

    monkeypatch.setattr(reranker, "_get_reranker", lambda: _Slow())
    cands = [{"filename": "a.md", "body": "x"}]
    threads = [threading.Thread(target=lambda: rerank_daemon._scorer("q", cands, 1)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert state["max"] == 1, f"concurrent predict() detected (max overlap={state['max']})"


def test_serve_loads_model_before_it_listens(monkeypatch):
    """The daemon must finish loading the model BEFORE it accepts connections,
    so warm-up clients get a fast refusal (→ in-process fallback) instead of
    hanging on a listening-but-not-ready socket."""
    order = []
    monkeypatch.setattr(rerank_daemon, "_already_running", lambda timeout=1.0: False)

    class _FakeServer:
        def __init__(self, addr, handler, bind_and_activate=True):
            pass
        def server_bind(self):
            order.append("bind")
        def server_activate(self):
            order.append("activate")
        def serve_forever(self, poll_interval=1.0):
            order.append("serve")
        def server_close(self):
            order.append("close")
        def shutdown(self):
            pass

    monkeypatch.setattr(rerank_daemon, "_Server", _FakeServer)
    monkeypatch.setattr(reranker, "_get_reranker", lambda: (order.append("load") or object()))
    # Neutralize the watchdog thread.
    monkeypatch.setattr(rerank_daemon.threading, "Thread",
                        lambda *a, **k: type("_T", (), {"start": lambda s: None})())
    rc = rerank_daemon.serve()
    assert rc == 0
    assert order.index("bind") < order.index("load") < order.index("activate") < order.index("serve"), order


def test_process_rerank_request_caps_candidate_count():
    big = [{"filename": f"{i}.md", "body": "x"} for i in range(rerank_daemon.MAX_CANDIDATES + 25)]
    seen = {}
    def scorer(q, cands, k):
        seen["n"] = len(cands)
        return cands[:k]
    status, resp = rerank_daemon.process_rerank_request(
        {"query": "q", "candidates": big, "top_k": 5}, scorer)
    assert seen.get("n", 10**9) <= rerank_daemon.MAX_CANDIDATES


def test_idle_timeout_zero_disables_eviction():
    # IDLE_SEC <= 0 must mean "never evict", not "evict after the first poll".
    assert rerank_daemon._should_evict(last_request_ts=0.0, now=10 ** 9, idle_timeout=0) is False
    assert rerank_daemon._should_evict(last_request_ts=0.0, now=10 ** 9, idle_timeout=-5) is False


def test_rerank_circuit_breaker_skips_in_process_load(monkeypatch):
    # If a model load failed recently (cross-process marker), don't re-pay the
    # ~9s cold load on every hook — return candidates unchanged (strictly additive).
    cands = [{"filename": "a.md", "body": "x"}]
    monkeypatch.setattr(reranker, "_daemon_enabled", lambda: False)
    monkeypatch.setattr(reranker, "_load_failed_recently", lambda: True)
    monkeypatch.setattr(reranker, "_get_reranker",
                        lambda: (_ for _ in ()).throw(AssertionError("loaded model while breaker tripped")))
    assert reranker.rerank("q", cands, top_k=5) == cands[:5]


def test_rerank_stamps_breaker_on_load_failure(monkeypatch):
    cands = [{"filename": "a.md", "body": "x"}]
    monkeypatch.setattr(reranker, "_daemon_enabled", lambda: False)
    monkeypatch.setattr(reranker, "_load_failed_recently", lambda: False)
    monkeypatch.setattr(reranker, "_get_reranker", lambda: None)  # load fails
    stamped = []
    monkeypatch.setattr(reranker, "_stamp_load_failed", lambda: stamped.append(True))
    out = reranker.rerank("q", cands, top_k=5)
    assert out == cands[:5]
    assert stamped == [True], "a failed load must trip the breaker for next time"


def test_rerank_clears_breaker_on_successful_load(monkeypatch):
    cands = [{"filename": "a.md", "body": "x"}]
    monkeypatch.setattr(reranker, "_daemon_enabled", lambda: False)
    monkeypatch.setattr(reranker, "_load_failed_recently", lambda: False)

    class _M:
        def predict(self, pairs, **k):
            return [0.5] * len(pairs)
    monkeypatch.setattr(reranker, "_get_reranker", lambda: _M())
    cleared = []
    monkeypatch.setattr(reranker, "_clear_load_failed", lambda: cleared.append(True))
    reranker.rerank("q", cands, top_k=5)
    assert cleared == [True], "a healthy load must clear a stale breaker"


def test_within_cooldown_handles_future_mtime():
    # Recent past mtime → still cooling down (suppress spawn).
    assert reranker._within_cooldown(mtime=1000.0, now=1005.0, cooldown=30) is True
    # Old mtime → cooled down (allow spawn).
    assert reranker._within_cooldown(mtime=1000.0, now=1100.0, cooldown=30) is False
    # FUTURE mtime (clock skew / backup restore) → treat as stale, not "forever cooling".
    assert reranker._within_cooldown(mtime=2000.0, now=1000.0, cooldown=30) is False


def test_request_evict_shuts_server_down_off_thread():
    calls = []

    class _S:
        def shutdown(self):
            calls.append("shutdown")

    class _T:
        def __init__(self, target=None, daemon=None):
            self.target, self.daemon = target, daemon

        def start(self):
            assert self.daemon is True
            self.target()

    rerank_daemon.request_evict(_S(), thread_factory=_T)
    assert calls == ["shutdown"]


def test_evict_endpoint_answers_then_requests_shutdown(monkeypatch):
    """POST /evict (dart2 co-tenant eviction, 2026-09-02): 200 first so the
    caller isn't left waiting on a dying socket, then the off-thread stop."""
    sent, evicted = [], []
    h = rerank_daemon._Handler.__new__(rerank_daemon._Handler)
    h.path, h.headers, h.server = "/evict", {}, object()
    h._send = lambda status, obj: sent.append((status, obj))
    monkeypatch.setattr(rerank_daemon, "request_evict", lambda server: evicted.append(server))
    h.do_POST()
    assert sent == [(200, {"evicting": True})]
    assert evicted == [h.server]


def test_evict_does_not_shadow_rerank_route(monkeypatch):
    sent, evicted = [], []
    h = rerank_daemon._Handler.__new__(rerank_daemon._Handler)
    h.path, h.headers, h.server = "/nope", {}, object()
    h._send = lambda status, obj: sent.append((status, obj))
    monkeypatch.setattr(rerank_daemon, "request_evict", lambda server: evicted.append(server))
    h.do_POST()
    assert sent == [(404, {"error": "not found"})] and evicted == []
