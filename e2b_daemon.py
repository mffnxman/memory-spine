"""e2b_daemon.py — llama-server supervisor for dart-e2b (T3.6, 2026-07-10).

Runs the memory stack's local LLM sidecar: dart's own QLoRA-tuned Gemma-4 E2B
(trained on dart traces/journal/memories, exported Q4_K_M). Serves importance
scoring, HyDE (if ever re-enabled), consolidate rewrites, and other mechanical
lanes that used to hit Ollama/dart-fast-4b.

Design (mirrors rerank_daemon's contract):
  - Spawns llama-server as a child on 127.0.0.1:8766 (memory-stack port
    family: 8765 rerank, 8766 e2b). Singleton is a two-part guard mirroring
    rerank_daemon: if a healthy server already answers /health this supervisor
    exits without spawning, and a delete-on-close lock file
    (_meta/.e2b_daemon.lock) covers the model-load window before llama-server
    binds the port.
  - Idle self-shutdown after E2B_DAEMON_IDLE_SEC (default 900s) to free the
    ~1.4GB VRAM for dart2's big tiers (the governor refuses spawns into thrash;
    a stale resident e2b must not be the reason dart's daily tier can't load).
    Idle = age of the heartbeat file memory/_meta/.e2b_last_used, touched by
    dartagnan_provider on every generate.
  - If the llama-server child dies, the supervisor exits too (next provider
    call respawns both).

Server VRAM note: llama.cpp keeps Gemma E-series per-layer embeddings on CPU,
so the resident footprint is ~1.4GB despite the 3.4GB GGUF.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
import sys as _sys  # noqa: E402
_sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths as _paths  # noqa: E402
from urllib.request import Request, urlopen

MEMORY_DIR = _paths.MEMORY_DIR
HEARTBEAT = MEMORY_DIR / "_meta" / ".e2b_last_used"
LOCK = MEMORY_DIR / "_meta" / ".e2b_daemon.lock"

# Both come from the environment: the daemon is an optional local-LLM sidecar
# and the binary/model live wherever the operator keeps them.
LLAMA_SERVER = os.environ.get("E2B_LLAMA_SERVER", "llama-server")
MODEL = os.environ.get("E2B_MODEL", "")
PORT = int(os.environ.get("E2B_DAEMON_PORT", "8766"))
IDLE_TIMEOUT_SEC = int(os.environ.get("E2B_DAEMON_IDLE_SEC", "900"))
POLL_SEC = 30


def _health(timeout: float = 1.5) -> bool:
    try:
        req = Request(f"http://127.0.0.1:{PORT}/health", method="GET")
        with urlopen(req, timeout=timeout) as r:
            return (
                r.status == 200 and json.loads(r.read() or b"{}").get("status") == "ok"
            )
    except Exception:
        return False


def _acquire_lock() -> int | None:
    """Singleton mutex, part 2 (pairs with the _health() pre-check).

    The health check alone is not enough: llama-server binds :8766 only AFTER
    the model finishes loading (30s+), and provider calls landing in that
    window each spawned a full extra copy of the weights — 3x in VRAM on the
    2026-07-13 boot, starving dart2's daily tier. O_TEMPORARY = delete-on-
    close: Windows removes the lock when this process exits or dies, so a
    stale lock can never wedge respawns.
    """
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    try:
        return os.open(str(LOCK), os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_TEMPORARY)
    except (FileExistsError, PermissionError):
        # exists → a live supervisor holds it; PermissionError → the holder is
        # mid-exit (delete pending). Either way it's not ours — don't spawn.
        return None


def _idle_sec(now: float) -> float:
    try:
        return now - HEARTBEAT.stat().st_mtime
    except OSError:
        return float("inf")  # no heartbeat ever written — treat as idle since boot


def main() -> int:
    if _health():
        print("[e2b_daemon] healthy server already on port, exiting", flush=True)
        return 0
    lock_fd = _acquire_lock()  # held (never closed) until process exit
    if lock_fd is None:
        print(
            "[e2b_daemon] another supervisor holds the lock (server mid-load), exiting",
            flush=True,
        )
        return 0
    if not Path(MODEL).exists():
        print(f"[e2b_daemon] model missing: {MODEL}", flush=True)
        return 1

    child = subprocess.Popen(
        [
            LLAMA_SERVER,
            "-m",
            MODEL,
            "--port",
            str(PORT),
            "--host",
            "127.0.0.1",
            "-ngl",
            "99",
            "--jinja",
            "-c",
            "4096",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    print(f"[e2b_daemon] spawned llama-server pid={child.pid} port={PORT}", flush=True)
    HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
    HEARTBEAT.touch()  # spawn counts as use: idle clock starts now, not at epoch

    try:
        while True:
            time.sleep(POLL_SEC)
            if child.poll() is not None:
                print(
                    f"[e2b_daemon] llama-server exited rc={child.returncode}",
                    flush=True,
                )
                return child.returncode or 0
            idle = _idle_sec(time.time())
            if IDLE_TIMEOUT_SEC > 0 and idle > IDLE_TIMEOUT_SEC:
                print(
                    f"[e2b_daemon] idle {idle:.0f}s > {IDLE_TIMEOUT_SEC}s — stopping to free VRAM",
                    flush=True,
                )
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                return 0
    except KeyboardInterrupt:
        child.terminate()
        return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
