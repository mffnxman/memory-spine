"""
reranker.py — cross-encoder rerank pass for hybrid retrieval (v3.2 Phase 1).

Wraps BAAI/bge-reranker-v2-m3 via sentence-transformers.CrossEncoder. Takes
top-N candidates from RRF fusion, jointly scores (query, memory) pairs, and
returns a precision-tuned top-K.

Empirical lift: +5-7 nDCG@10 on standard RAG retrieval (verified across
MTEB/BEIR). The highest-leverage retrieval upgrade by ratio of effort to gain.

Design:
  - Lazy load — model loads on first use, not import time
  - Failover — if model unavailable, returns candidates unchanged (no exception)
  - Pure post-rank — never disturbs RRF, never writes state
  - Body truncation at 2000 chars — bge-reranker has 8192-token context but
    most signal lives in the opening anyway, and short truncation keeps
    latency bounded

Telemetry: appends per-call rerank events to _meta/v3_2_telemetry.jsonl.

Hook contract:
  rerank(query, candidates, top_k) -> list[candidate dict]
  where candidate dict has at minimum {filename, body} and ranking metadata
  is preserved as-is.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
import paths as _paths  # noqa: E402

# Sentence-transformers pulls in torch: ~600MB RSS and 10s+ of import time
# (40-60s under memory pressure). Importing it at module level made every
# CLIENT process (prefetch, hooks — anything that only pings the daemon) pay
# that cost, which is what blocked UserPromptSubmit for 40-60s per prompt on
# 2026-06-10. The import is now deferred into _get_reranker(), which only the
# daemon (or an explicit local-model fallback) ever reaches.
import importlib.util

CrossEncoder = None  # type: ignore  # populated lazily by _import_cross_encoder()
_ST_AVAILABLE = importlib.util.find_spec("sentence_transformers") is not None


def _import_cross_encoder():
    """Deferred heavyweight import (sentence_transformers -> torch).
    Returns the CrossEncoder class, or None if unavailable. Never raises."""
    global CrossEncoder
    if CrossEncoder is not None:
        return CrossEncoder
    try:
        from sentence_transformers import CrossEncoder as _CE  # type: ignore

        CrossEncoder = _CE
    except Exception:
        return None
    return CrossEncoder


if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


MODEL = "BAAI/bge-reranker-v2-m3"  # default; override via feature_flags "rerank_model"
LOADED_MODEL: Optional[str] = (
    None  # name actually constructed — /health and CLI report this
)
BODY_MAX_CHARS = 2000  # truncate memory body before pairing with query
DEFAULT_TOP_K = 10
# Alarm threshold; just logs, doesn't block. Recalibrated 500 -> 2000
# (BIGBUFF 2.0 D6-08): the 7/31 surgery deliberately put a ~700-900ms warm
# rerank in the hook path, so the old 500ms flag fired on 79% of healthy
# post-surgery events (p50 363ms, p99 1.28s) — an alarm that always fires is
# no alarm. The real pathology is the in-process cold load, tracked
# separately via cold_load=true on rerank_ok.
LATENCY_BUDGET_MS = 2000

# Warm-sidecar daemon (rerank_daemon.py): loads the model once and serves over
# localhost so prefetch stops cold-loading 600MB+ every prompt (Theme A).
RERANK_DAEMON_DEFAULT_PORT = 8765
_DAEMON_CONNECT_TIMEOUT = 0.3  # fast liveness pre-check before the POST
_SPAWN_COOLDOWN_SEC = 30  # bound spawn attempts across fresh hook processes
_daemon_spawn_attempted = False  # at most one spawn attempt per process


def _read_timeout() -> float:
    """Read timeout for the /rerank POST. Default 1.5s — a warm daemon answers in
    ~0.2s, so we stay well under prefetch's 500ms-ish budget while tolerating a
    brief GPU hiccup. A malformed RERANK_DAEMON_TIMEOUT must never crash import."""
    try:
        return float(os.environ.get("RERANK_DAEMON_TIMEOUT", "1.5"))
    except (TypeError, ValueError):
        return 1.5


def _within_cooldown(mtime: float, now: float, cooldown: float) -> bool:
    """True iff the marker was touched recently in the PAST. A future mtime
    (clock skew / backup restore) is treated as stale so it can't permanently
    suppress the gated action."""
    delta = now - mtime
    return 0 <= delta < cooldown


# Circuit breaker for a permanently-broken model (corrupt cache / CUDA failure).
# Without it, every fresh hook process re-attempts the ~9s in-process cold load
# and fails again. The cross-process marker makes one process's failure suppress
# the reload for a backoff window; rerank stays strictly-additive (returns
# candidates unchanged when it can't score).
RERANK_LOAD_FAIL_BACKOFF_SEC = 900  # 15 min


def _load_failed_recently() -> bool:
    try:
        if _LOAD_FAIL_MARKER.exists():
            return _within_cooldown(
                _LOAD_FAIL_MARKER.stat().st_mtime,
                time.time(),
                RERANK_LOAD_FAIL_BACKOFF_SEC,
            )
    except Exception:
        pass
    return False


def _stamp_load_failed() -> None:
    try:
        _LOAD_FAIL_MARKER.parent.mkdir(parents=True, exist_ok=True)
        _LOAD_FAIL_MARKER.write_text(str(time.time()))
    except Exception:
        pass


def _clear_load_failed() -> None:
    try:
        if _LOAD_FAIL_MARKER.exists():
            _LOAD_FAIL_MARKER.unlink()
    except Exception:
        pass


def _resolve_model_name(flags: dict) -> str:
    """The 'rerank_model' flag (a string) overrides MODEL. Non-string or blank
    values fall back to the default — a mistyped flags file must never brick
    rerank with an unloadable name."""
    val = flags.get("rerank_model")
    if isinstance(val, str) and val.strip():
        return val.strip()
    return MODEL


_reranker: Optional["CrossEncoder"] = None  # lazy singleton
_load_failed = False  # avoid retrying after a failed load
_load_lock = threading.Lock()  # serialize first-load across threads

MEMORY_DIR = _paths.MEMORY_DIR
TELEMETRY_PATH = MEMORY_DIR / "_meta" / "v3_2_telemetry.jsonl"
_LOAD_FAIL_MARKER = (
    MEMORY_DIR / "_meta" / ".rerank_load_failed"
)  # reranker circuit breaker


def _log_telemetry(record: dict) -> None:
    """Append a telemetry record. Failures here must never bubble up."""
    try:
        TELEMETRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        record.setdefault("ts", int(time.time()))
        record.setdefault("component", "reranker")
        with TELEMETRY_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception:
        pass


def _get_reranker() -> Optional["CrossEncoder"]:
    """Lazy-load and cache. Returns None if unavailable.

    Loads in FP16 on CUDA when available — halves VRAM (~1.1GB → ~600MB) and
    is faster on Ampere+ GPUs (the 4070 Laptop is Ada Lovelace, even better).
    Falls back to FP32 on CPU because CPU FP16 isn't a real speedup.

    If the `rerank_on_cpu` feature flag is on, forces device='cpu' regardless
    of CUDA availability — used when GPU VRAM is contested by Ollama's
    larger models.

    Thread-safe via _load_lock — guarantees the model is loaded exactly once
    even under concurrent hook invocations. Fast path (already loaded) skips
    the lock entirely. Slow path serializes on first load."""
    global _reranker, _load_failed, LOADED_MODEL
    # Fast path — no lock needed once loaded.
    if _reranker is not None:
        return _reranker
    if _load_failed or not _ST_AVAILABLE:
        return None
    with _load_lock:
        # Double-check inside lock — another thread may have loaded while we waited.
        if _reranker is not None:
            return _reranker
        if _load_failed:
            return None
        try:
            t0 = time.time()
            cls = _import_cross_encoder()
            if cls is None:
                _load_failed = True
                return None
            # Resolve model name + device + dtype before construction.
            force_cpu = False
            flags: dict = {}
            try:
                from feature_flags import all_flags, is_enabled

                force_cpu = is_enabled("rerank_on_cpu")
                flags = all_flags()
            except Exception:
                pass
            model_name = _resolve_model_name(flags)
            device = "cpu"
            dtype = None
            if not force_cpu:
                try:
                    import torch

                    if torch.cuda.is_available():
                        device = "cuda"
                        dtype = torch.float16
                except Exception:
                    pass
            kwargs: dict = {"max_length": 512, "device": device}
            if dtype is not None:
                # sentence-transformers >=3.x accepts model_kwargs for HF AutoModel
                kwargs["model_kwargs"] = {"torch_dtype": dtype}
            _reranker = cls(model_name, **kwargs)
            LOADED_MODEL = model_name
            _log_telemetry(
                {
                    "event": "model_loaded",
                    "model": model_name,
                    "device": device,
                    "dtype": "float16" if dtype is not None else "float32",
                    "load_sec": round(time.time() - t0, 2),
                }
            )
            return _reranker
        except Exception as e:
            _load_failed = True
            _log_telemetry(
                {"event": "model_load_failed", "model": MODEL, "error": str(e)[:200]}
            )
            return None


def is_available() -> bool:
    """Cheap check — does the model load? Used by feature flag gating to
    decide whether to bother calling rerank() at all on cold paths."""
    return _get_reranker() is not None


def _build_pairs(query: str, candidates: list[dict]) -> list[tuple[str, str]]:
    """Build (query, text) pairs for the cross-encoder.

    Every element is coerced with str(): a non-string body/name/description
    (int, list, or a stray non-None survivor) would otherwise reach
    CrossEncoder.predict() and raise "TextInputSequence must be str", which
    rerank()'s except-clause swallows — silently dropping the WHOLE query back
    to RRF order with no rerank lift and no user signal. str() makes the
    malformed-input path unreachable. (Regression: 14 hits in v3_2_telemetry,
    last 2026-06-01.)
    """

    # Coerce every element to a guaranteed non-empty str. _s() always returns a
    # str (None->"", everything else->str()); we then normalise an empty/blank
    # query or doc to a single space. tokenizers accepts "" but ST 5.x's
    # is_singular_input() is least predictable on empty/degenerate shapes, and an
    # empty doc carries no rerank signal anyway — " " tokenises cleanly and never
    # reshapes the batch. (Closes the residual "TextInputSequence must be str" /
    # "TextEncodeInput must be Union[...]" path that str() alone left open.)
    def _s(v) -> str:
        return v if isinstance(v, str) else ("" if v is None else str(v))

    q = _s(query).strip() or " "
    pairs: list[tuple[str, str]] = []
    for c in candidates:
        body = _s(c.get("body"))
        if len(body) > BODY_MAX_CHARS:
            body = body[:BODY_MAX_CHARS]
        # Front-load name + description if present (these carry signal density).
        prefix_parts = []
        for k in ("name", "description"):
            v = _s(c.get(k))
            if v:
                prefix_parts.append(v)
        prefix = "\n".join(prefix_parts)
        text = (prefix + "\n" + body) if prefix else body
        text = text if text.strip() else " "
        pairs.append((q, text))
    return pairs


def _rerank_with_model(
    model, query: str, candidates: list[dict], top_k: int, cold_load: bool = False
) -> list[dict]:
    """Score `candidates` with an already-loaded cross-encoder model.

    The shared scoring core used by BOTH the in-process path and the resident
    daemon — so the two can never drift. Fail-soft: returns candidates[:top_k]
    unchanged on predict failure or output-shape mismatch (never raises).
    """
    t0 = time.time()
    pairs = _build_pairs(query, candidates)

    try:
        scores = model.predict(pairs, show_progress_bar=False)
    except Exception as e:
        _log_telemetry(
            {
                "event": "rerank_failed",
                "error": str(e)[:200],
                "n_candidates": len(candidates),
            }
        )
        return candidates[:top_k]

    # Normalise predict()'s output to a flat list of floats BEFORE any length
    # check. sentence-transformers 5.x returns a numpy scalar / 0-d array when
    # is_singular_input() fires (a lone pair, or a flat shape it mistakes for
    # one) — len() on that raises "object of type numpy.float32 has no len()",
    # the uncaught TypeError that used to escape to search_hybrid as a fail_soft
    # and to the daemon as a 500. Coercing to a list makes the scalar case and
    # the genuine length-N case comparable instead of crashing.
    try:
        score_list = [float(s) for s in scores]
    except TypeError:
        score_list = [float(scores)]

    # Output-shape guard. A mismatch means predict() collapsed or expanded the
    # batch (ST singular-input ambiguity, corrupt model, library quirk). This is
    # a benign, recoverable shape event — NOT an error — so we fail soft to RRF
    # order at debug level instead of spamming error telemetry on every few-
    # candidate query. (Renamed from rerank_shape_mismatch; nothing in _scripts
    # consumes the old name — verified by grep.)
    if len(score_list) != len(pairs):
        _log_telemetry(
            {
                "event": "rerank_shape_softfallback",
                "level": "debug",
                "expected": len(pairs),
                "got": len(score_list),
            }
        )
        return candidates[:top_k]

    for c, s in zip(candidates, score_list):
        c["rerank_score"] = float(s)

    candidates.sort(key=lambda c: c.get("rerank_score", float("-inf")), reverse=True)

    elapsed_ms = round((time.time() - t0) * 1000, 1)
    _log_telemetry(
        {
            "event": "rerank_ok",
            "n_candidates": len(pairs),
            "top_k": top_k,
            "elapsed_ms": elapsed_ms,
            "over_budget": elapsed_ms > LATENCY_BUDGET_MS,
            # The genuine latency pathology (D6-08): this call cold-loaded the
            # model in-process (~9s+, the 120s-outlier class). Monitors should
            # key on this, not on over_budget.
            "cold_load": cold_load,
        }
    )

    return candidates[:top_k]


def _daemon_enabled() -> bool:
    """Daemon path on by default; off only if explicitly disabled.

    RERANK_DAEMON_DISABLE env or rerank_daemon_enabled=false in feature_flags
    turns it off. A missing flag means ON (the daemon is a pure latency win and
    fails soft to in-process)."""
    if os.environ.get("RERANK_DAEMON_DISABLE"):
        return False
    try:
        from feature_flags import all_flags

        flags = all_flags()
        if "rerank_daemon_enabled" in flags:
            return bool(flags["rerank_daemon_enabled"])
    except Exception:
        pass
    return True


def _daemon_base_url() -> str:
    return os.environ.get(
        "RERANK_DAEMON_URL", f"http://127.0.0.1:{RERANK_DAEMON_DEFAULT_PORT}"
    ).rstrip("/")


def _rerank_via_daemon(query: str, candidates: list[dict], top_k: int):
    """POST to the warm daemon. Returns the reranked list, or None to signal the
    caller to fall back in-process (daemon down/slow/unreachable). Never raises.

    A fast socket pre-check (0.3s) comes first so a DOWN or still-WARMING daemon
    fails over immediately instead of making the prefetch hook wait the full read
    timeout — the whole point of this session was to stop hooks blocking."""
    import socket
    import urllib.request
    from urllib.parse import urlparse

    base = _daemon_base_url()
    u = urlparse(base)
    host = u.hostname or "127.0.0.1"
    port = u.port or RERANK_DAEMON_DEFAULT_PORT

    # Liveness pre-check: a closed or bound-but-not-listening (warming) port
    # refuses or times out fast here, capped at _DAEMON_CONNECT_TIMEOUT.
    try:
        with socket.create_connection((host, port), timeout=_DAEMON_CONNECT_TIMEOUT):
            pass
    except Exception:
        return None

    body = json.dumps(
        {"query": query, "candidates": candidates, "top_k": top_k}
    ).encode("utf-8")
    req = urllib.request.Request(
        base + "/rerank",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_read_timeout()) as r:
            if r.status != 200:
                return None
            resp = json.loads(r.read().decode("utf-8") or "{}")
        out = resp.get("candidates")
        return out if isinstance(out, list) else None
    except Exception:
        return None


def _ensure_daemon_spawned() -> None:
    """Spawn the resident daemon (detached) if it isn't already up.

    At most once per process, and rate-limited across fresh hook processes via a
    cooldown marker so a daemon that can't start doesn't cause a spawn storm
    (the port-bind singleton would also make extras exit, but this avoids the
    churn). Best-effort — never raises."""
    global _daemon_spawn_attempted
    if _daemon_spawn_attempted:
        return
    _daemon_spawn_attempted = True
    try:
        marker = MEMORY_DIR / "_meta" / ".rerank_daemon_spawn"
        now = time.time()
        if marker.exists() and _within_cooldown(
            marker.stat().st_mtime, now, _SPAWN_COOLDOWN_SEC
        ):
            return
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(str(now))
        import subprocess

        daemon_path = str(Path(__file__).resolve().parent / "rerank_daemon.py")
        kwargs: dict = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "close_fds": True,
        }
        if sys.platform == "win32":
            # DETACHED_PROCESS | CREATE_NO_WINDOW — survive the hook's exit, no console.
            kwargs["creationflags"] = 0x00000008 | 0x08000000
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, daemon_path], **kwargs)
    except Exception:
        pass


def rerank(
    query: str, candidates: list[dict], top_k: int = DEFAULT_TOP_K
) -> list[dict]:
    """Cross-encoder rerank.

    Args:
      query: the search query string.
      candidates: list of dicts. Each MUST have keys 'filename' and 'body'.
                  Other keys are preserved untouched.
      top_k: how many to return after reranking. If candidates has fewer
             than top_k entries, returns all of them reranked.

    Returns:
      The same list of dicts, reordered by cross-encoder score, with an
      added 'rerank_score' key on each. Truncated to top_k.

    Path:
      Prefers the warm sidecar daemon (model already resident → no cold load).
      If the daemon is down it spawns it for next time and serves THIS call
      in-process (cold-loading the model in this process).

    Failover:
      If model unavailable, returns candidates[:top_k] unchanged. Never
      raises. The caller can treat rerank as a strictly-additive lift —
      worst case it's a no-op.
    """
    if not candidates:
        # Empty pool = nothing injected = nothing for the floor to protect.
        # Logged as rerank_ok so the sentinel's bypass pairing doesn't count
        # it (2026-08-13: unlogged empties were most of the false 26%).
        _log_telemetry({"event": "rerank_ok", "empty_pool": True, "n": 0})
        return []

    # Fast path: the resident daemon already holds the model.
    if _daemon_enabled():
        via = _rerank_via_daemon(query, candidates, top_k)
        if via is not None:
            return via
        # Daemon unreachable: bring it up for next time, serve this call below.
        _ensure_daemon_spawned()
        # Hot-path processes (prefetch et al., MEMORY_HOT_PATH=1) must NEVER
        # cold-load the model in-process — that's a ~10s / multi-GB hit on the
        # prompt path (2026-06-10 freeze). Skip the lift; daemon serves next call.
        if os.environ.get("MEMORY_HOT_PATH"):
            _log_telemetry({"event": "rerank_skipped_daemon_cold", "hot_path": True})
            return candidates[:top_k]

    # Circuit breaker: if an in-process load failed recently, the model is likely
    # broken — skip the ~9s cold-load attempt and return unchanged. A real
    # floor bypass (candidates go out unfloored) — must be visible.
    if _load_failed_recently():
        _log_telemetry({"event": "rerank_failed", "reason": "circuit_breaker"})
        return candidates[:top_k]

    was_cold = _reranker is None  # resident state BEFORE the load attempt
    model = _get_reranker()
    if model is None:
        _stamp_load_failed()
        _log_telemetry({"event": "rerank_failed", "reason": "model_unavailable"})
        return candidates[:top_k]
    _clear_load_failed()

    return _rerank_with_model(model, query, candidates, top_k, cold_load=was_cold)


def main():
    """CLI for quick sanity check.

    Usage:
      python reranker.py status                  # is model available?
      python reranker.py test "your query"       # rerank current memories
    """
    if len(sys.argv) < 2:
        print("Usage: python reranker.py status | test <query>")
        return

    cmd = sys.argv[1]

    if cmd == "status":
        ok = is_available()
        print(
            json.dumps(
                {
                    "available": ok,
                    "model": LOADED_MODEL or MODEL,
                    "sentence_transformers_installed": _ST_AVAILABLE,
                },
                indent=2,
            )
        )
        return

    if cmd == "test":
        if len(sys.argv) < 3:
            print("Usage: python reranker.py test <query>")
            return
        query = sys.argv[2]
        # Pull current memories and rerank a sample
        try:
            from memory_engine import list_memories, search_hybrid
        except Exception as e:
            print(f"ERR: couldn't import memory_engine: {e}")
            return

        mems = list_memories()
        # Get top-15 via existing hybrid search, then rerank top-15 → top-5
        results = search_hybrid(query, mems=mems, top_k=15)
        candidates = [
            {
                "filename": m.filename,
                "name": m.name,
                "description": m.description,
                "body": m.body,
                "rrf_score": score,
            }
            for m, score, _ in results
        ]

        print(f"\nBefore rerank (top 15 by RRF):")
        for i, c in enumerate(candidates[:15]):
            print(f"  {i+1:2d}. {c['filename']:50s}  rrf={c['rrf_score']:.4f}")

        reranked = rerank(query, candidates, top_k=5)

        print(f"\nAfter rerank (top 5 by cross-encoder):")
        for i, c in enumerate(reranked):
            print(
                f"  {i+1:2d}. {c['filename']:50s}  rerank={c.get('rerank_score', 0):.4f}  (was rrf={c['rrf_score']:.4f})"
            )
        return

    print(f"Unknown command: {cmd}")


if __name__ == "__main__":
    main()
