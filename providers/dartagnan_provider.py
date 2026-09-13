"""d'Artagnan local provider — dart-e2b llama-server sidecar (T3.6 cutover).

Serves the memory stack's mechanical LLM lanes (importance scoring, HyDE if
re-enabled, consolidate rewrites) from dart's own QLoRA-tuned Gemma-4 E2B —
trained 2026-07-10 on dart traces/journal/memories, Q4_K_M, ~1.4GB VRAM
resident (llama.cpp keeps the E-series per-layer embeddings on CPU),
109 tok/s decode on the 4070.

Transport: OpenAI-compat /v1/chat/completions on 127.0.0.1:8766, supervised
by e2b_daemon.py (on-demand spawn, idle self-stop after E2B_DAEMON_IDLE_SEC).
Override endpoint via DARTAGNAN_URL env (the T3.5 eval shim used this).

History: previous incarnation spoke Ollama (/api/chat, dart-fast-4b) on
:11434. Ollama stays installed until the user retires it — this provider just
no longer calls it. Legacy model aliases (dart-fast-4b, local-qwen, qwen,
claude-*) all map to dart-e2b so old call sites keep working.

Thinking is disabled per-request via chat_template_kwargs: Gemma 4 opens a
reasoning block by default, which would eat small max_tokens budgets whole
(measured on the T3.4 smoke: 200 tokens of thinking, empty content).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import paths as _paths  # noqa: E402

from providers import register
from providers.base import Provider, Response

DEFAULT_URL = "http://127.0.0.1:8766"
DEFAULT_MODEL = "dart-e2b"
LEGACY_ALIASES = ("dart-fast-4b", "dart-brain", "local-qwen", "qwen")
HEALTH_TIMEOUT_SEC = 1.2
SPAWN_WAIT_SEC = 25  # cold llama-server load measured ~6s; leave slack
GEN_TIMEOUT_SEC = 120

MEMORY_DIR = _paths.MEMORY_DIR
HEARTBEAT = MEMORY_DIR / "_meta" / ".e2b_last_used"
DAEMON = _paths.SCRIPTS_DIR / "e2b_daemon.py"


class DartagnanProvider(Provider):
    name = "dartagnan"

    def __init__(self):
        self.base_url = os.environ.get("DARTAGNAN_URL", DEFAULT_URL).rstrip("/")
        self._spawn_attempted = False

    # -- lifecycle ---------------------------------------------------------

    def _server_up(self, timeout: float = HEALTH_TIMEOUT_SEC) -> bool:
        try:
            req = Request(f"{self.base_url}/health", method="GET")
            with urlopen(req, timeout=timeout) as r:
                if r.status != 200:
                    return False
                body = json.loads(r.read().decode("utf-8") or "{}")
                return body.get("status") == "ok"
        except (URLError, HTTPError, json.JSONDecodeError, TimeoutError, OSError):
            return False

    def _spawn_daemon(self) -> None:
        """Fire-and-forget the supervisor. At most once per process (a daemon
        that can't start must not turn every call into a spawn storm)."""
        if self._spawn_attempted:
            return
        self._spawn_attempted = True
        try:
            subprocess.Popen(
                [sys.executable, str(DAEMON)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
            )
        except Exception:
            pass

    def health_check(self) -> bool:
        """Quick contract (callers budget ~1.5s, e.g. hyde's timed thread):
        if the server is cold, kick the spawn and report False — THIS call
        falls back to anthropic, the next one finds the sidecar warm."""
        if self._server_up():
            return True
        if os.environ.get("DARTAGNAN_URL"):
            return False  # explicit endpoint (eval shim / test) — never spawn
        self._spawn_daemon()
        return self._server_up(timeout=0.5)

    def _ensure_up(self) -> bool:
        """Committed-path variant: background workers (consolidate, outbox)
        can afford the cold-load wait."""
        if self._server_up():
            return True
        if os.environ.get("DARTAGNAN_URL"):
            return False
        self._spawn_daemon()
        deadline = time.time() + SPAWN_WAIT_SEC
        while time.time() < deadline:
            time.sleep(1.0)
            if self._server_up():
                return True
        return False

    # -- generation --------------------------------------------------------

    def generate(
        self, prompt: str, model: str = DEFAULT_MODEL, max_tokens: int = 1024, **kw
    ) -> Response:
        if model.startswith("claude-") or model in LEGACY_ALIASES:
            model = DEFAULT_MODEL

        if not self._ensure_up():
            raise RuntimeError("dartagnan unreachable: e2b sidecar failed to start")

        messages = []
        system = kw.get("system")
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        body = json.dumps(
            {
                "model": model,
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": kw.get("temperature", 0.3),  # low for structured tasks
                "chat_template_kwargs": {"enable_thinking": False},
            }
        ).encode("utf-8")
        req = Request(
            f"{self.base_url}/v1/chat/completions",
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urlopen(req, timeout=GEN_TIMEOUT_SEC) as r:
                resp = json.loads(r.read().decode("utf-8") or "{}")
        except (URLError, HTTPError, TimeoutError) as e:
            raise RuntimeError(f"dartagnan unreachable: {e}") from e

        try:
            HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
            HEARTBEAT.touch()
        except OSError:
            pass  # heartbeat is best-effort; worst case the sidecar idles out early

        choices = resp.get("choices") or [{}]
        text = (choices[0].get("message") or {}).get("content") or ""
        usage = resp.get("usage") or {}
        return Response(
            text=text,
            tokens_in=usage.get("prompt_tokens", 0),
            tokens_out=usage.get("completion_tokens", 0),
            model=resp.get("model", model),
            raw=resp,
        )


register("dartagnan", DartagnanProvider)
