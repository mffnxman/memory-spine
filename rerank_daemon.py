"""rerank_daemon.py — warm resident cross-encoder sidecar (Theme A, 2026-06-03).

The bge-reranker-v2-m3 model (~600MB-1.1GB) was cold-loading on EVERY
UserPromptSubmit because each Claude Code hook is a fresh OS process — 551
reloads in 6.3 days, p50=8.9s/load, blowing the <500ms prefetch budget by ~19x.
This daemon loads the model ONCE and serves rerank requests over localhost, so
prefetch pays the load cost once per resident lifetime instead of per prompt.

Client: reranker._rerank_via_daemon() POSTs here; if we're down it falls back to
an in-process load and respawns us. So this is a pure latency optimisation —
correctness never depends on the daemon being up.

Lifecycle:
  - Singleton via port bind: a second instance fails to bind 127.0.0.1:PORT and
    exits 0. The bind IS the mutex.
  - Idle self-shutdown after RERANK_DAEMON_IDLE_SEC (default 1800s) to free VRAM;
    the client auto-respawns on the next rerank (one cold load, then warm).
  - Stdlib only (http.server + json), matching dartagnan_provider's urllib idiom.

Endpoints:
  GET  /health  -> {ok, model, device, loaded_sec, idle_sec}
  POST /evict   -> {evicting: true}; the server then exits (2026-09-02:
                   co-tenant eviction — dart2's lifecycle asks for the
                   RAM back when a brain can't seat; the client respawns
                   us on the next rerank)
  POST /rerank  -> body {query, candidates, top_k} -> {candidates: [...]}
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _no_wmi  # noqa: F401, E402  — hung-WMI guard, must precede torch import
import reranker  # noqa: E402

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
IDLE_TIMEOUT_SEC = int(os.environ.get("RERANK_DAEMON_IDLE_SEC", "1800"))
# Cap so one runaway /rerank can't monopolize the resident model. The real
# caller (memory_engine) sends ~30; this is generous headroom.
MAX_CANDIDATES = 512

# Serialize inference: ThreadingHTTPServer dispatches each /rerank on its own
# thread, but they share ONE torch CrossEncoder, and PyTorch does not guarantee
# thread-safe concurrent forward passes (predict internally calls the model's
# eval-mode and device-placement methods, which mutate shared module state).
_infer_lock = threading.Lock()


def _port() -> int:
    """Port from RERANK_DAEMON_URL (http://127.0.0.1:PORT) else DEFAULT_PORT."""
    url = os.environ.get("RERANK_DAEMON_URL", "")
    if url and ":" in url:
        try:
            return int(url.rsplit(":", 1)[1].strip("/"))
        except Exception:
            pass
    return DEFAULT_PORT


def _daemon_health(timeout: float = 1.0):
    """GET /health on the configured port. Returns the health dict, or None."""
    import urllib.request

    try:
        with urllib.request.urlopen(
            reranker._daemon_base_url() + "/health", timeout=timeout
        ) as r:
            if r.status != 200:
                return None
            return json.loads(r.read().decode("utf-8") or "{}")
    except Exception:
        return None


def _already_running(timeout: float = 1.0) -> bool:
    """True if a sibling daemon is already serving /health.

    The app-level half of the singleton guard. Necessary because on Windows the
    socket bind alone is NOT a reliable mutex (see _Server)."""
    return _daemon_health(timeout=timeout) is not None


class _Server(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR permits binding a port that's already in use, so
    # the default HTTPServer.allow_reuse_address=1 would let a SECOND daemon bind
    # 127.0.0.1:PORT and load a duplicate ~600MB model (double VRAM, split brain).
    # False makes the second bind fail with WSAEADDRINUSE — the race-safe half of
    # the singleton mutex (paired with the _already_running() pre-check).
    allow_reuse_address = False


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested in tests/test_reranker_daemon.py)
# ---------------------------------------------------------------------------


def _should_evict(last_request_ts: float, now: float, idle_timeout: float) -> bool:
    """True when the daemon has been idle longer than the timeout (→ free VRAM).
    idle_timeout <= 0 means "never evict" (stay resident)."""
    if idle_timeout <= 0:
        return False
    return (now - last_request_ts) > idle_timeout


def process_rerank_request(payload, scorer):
    """Validate a /rerank payload and apply `scorer`. Returns (status, dict).

    scorer(query, candidates, top_k) -> reranked candidates. Kept pure (no model,
    no I/O) so it's unit-testable with a stub scorer.
    """
    if not isinstance(payload, dict):
        return 400, {"error": "payload must be a JSON object"}
    query = payload.get("query")
    candidates = payload.get("candidates")
    if not isinstance(query, str) or not isinstance(candidates, list):
        return 400, {"error": "require {query: str, candidates: list, top_k?: int}"}
    # Bound the work one request can demand of the shared resident model.
    if len(candidates) > MAX_CANDIDATES:
        candidates = candidates[:MAX_CANDIDATES]
    top_k = payload.get("top_k", reranker.DEFAULT_TOP_K)
    try:
        top_k = int(top_k)
    except Exception:
        top_k = reranker.DEFAULT_TOP_K
    try:
        out = scorer(query, candidates, top_k)
        return 200, {"candidates": out}
    except Exception as e:  # scorer blew up — surface, don't crash the daemon
        return 500, {"error": str(e)[:200]}


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

_state = {"last_request": 0.0, "loaded_sec": None, "device": "unknown"}
_state_lock = threading.Lock()


def _scorer(query, candidates, top_k):
    """Score using the resident model, reusing reranker's shared scoring core.

    Holds _infer_lock so concurrent /rerank requests never run forward passes on
    the shared model at the same time. Reranks are ~0.2s and rarely overlap on a
    single user, so the serialization cost is negligible."""
    model = reranker._get_reranker()
    if model is None:
        return candidates[:top_k]
    with _infer_lock:
        return reranker._rerank_with_model(model, query, candidates, top_k)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence default stderr logging
        pass

    def _send(self, status: int, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        if self.path.startswith("/health"):
            with _state_lock:
                idle = time.time() - _state["last_request"]
                self._send(
                    200,
                    {
                        "ok": True,
                        "model": reranker.LOADED_MODEL or reranker.MODEL,
                        "device": _state["device"],
                        "loaded_sec": _state["loaded_sec"],
                        "idle_sec": round(idle, 1),
                    },
                )
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.startswith("/evict"):
            # Co-tenant eviction (2026-09-02): answer, then exit off-thread.
            self._send(200, {"evicting": True})
            request_evict(self.server)
            return
        if not self.path.startswith("/rerank"):
            self._send(404, {"error": "not found"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n > 0 else b""
            payload = json.loads(raw or b"{}")
        except Exception as e:
            self._send(400, {"error": ("bad json: " + str(e))[:200]})
            return
        with _state_lock:
            _state["last_request"] = time.time()
        status, resp = process_rerank_request(payload, _scorer)
        self._send(status, resp)


def request_evict(server, thread_factory=threading.Thread) -> None:
    """Stop `server` from a side thread so the calling handler can finish
    its response first — shutdown() blocks until serve_forever() returns,
    and the handler runs on the server's own pool. Injectable thread
    factory keeps it unit-testable with a stub server."""
    thread_factory(target=server.shutdown, daemon=True).start()


def _idle_watchdog(server) -> None:
    """Shut the server down once it has been idle past the timeout."""
    poll = min(max(IDLE_TIMEOUT_SEC // 4, 30), IDLE_TIMEOUT_SEC) or 60
    while True:
        time.sleep(poll)
        with _state_lock:
            last = _state["last_request"]
        if _should_evict(last, time.time(), IDLE_TIMEOUT_SEC):
            try:
                server.shutdown()
            finally:
                return


def serve() -> int:
    port = _port()
    # Bind host from the same URL the client uses, so bind and connect can't
    # disagree (default 127.0.0.1).
    try:
        from urllib.parse import urlparse

        host = urlparse(reranker._daemon_base_url()).hostname or HOST
    except Exception:
        host = HOST

    # Singleton guard, part 1: if a sibling already answers /health, decline.
    if _already_running(timeout=1.0):
        return 0
    try:
        # Part 2: bind WITHOUT activating. The bind reserves the port (race-safe
        # mutex via allow_reuse_address=False); deferring listen() means a client
        # that connects during the ~9s model load gets a fast refusal and falls
        # back in-process, instead of hanging on a listening-but-not-ready socket.
        server = _Server((host, port), _Handler, bind_and_activate=False)
        server.server_bind()
    except OSError:
        # Port already bound → another daemon raced us here. We're the loser; exit.
        return 0

    # Load the model BEFORE we accept connections (the port is held, not listening).
    t0 = time.time()
    model = reranker._get_reranker()
    if model is None:
        try:
            server.server_close()
        except Exception:
            pass
        return 1  # nothing to serve; client will keep using in-process fallback
    with _state_lock:
        _state["loaded_sec"] = round(time.time() - t0, 2)
        _state["last_request"] = time.time()
        try:
            _state["device"] = str(
                getattr(getattr(model, "model", None), "device", "unknown")
            )
        except Exception:
            _state["device"] = "unknown"

    # Warm now — start listening.
    server.server_activate()

    if IDLE_TIMEOUT_SEC > 0:
        threading.Thread(target=_idle_watchdog, args=(server,), daemon=True).start()
    try:
        server.serve_forever(poll_interval=1.0)
    finally:
        try:
            server.server_close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(serve())
