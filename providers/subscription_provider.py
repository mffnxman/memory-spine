"""
subscription_provider.py — uses the local `claude` CLI (subscription auth).

Shells out to `claude -p "<prompt>"` which uses the user's existing OAuth
credentials. Zero new API keys needed. Trade-off: CLI cold-start latency is
~10-15s per call, so this provider is suitable for BACKGROUND work only
(weekly digest, sleep-time consolidation) — NOT for hot-path hooks like
HyDE that need sub-second response.

The tier router knows this via the `slow_acceptable` flag — tasks that opt
in get routed here when no API key is present.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers import register
from providers.base import Provider, Response

CLI_TIMEOUT_SEC = 180  # generous — model + CLI startup. W20 (14 epilogues) hit 90s,
# so 180s gives headroom for very busy weeks.
DEFAULT_MODEL = "claude-haiku-4-5"  # cheap by default; caller can override


class SubscriptionProvider(Provider):
    """Wraps the `claude` CLI as a Provider. Uses your Claude Code subscription
    auth — no API key required."""

    name = "subscription"

    def __init__(self):
        self._claude_path = shutil.which("claude")

    def health_check(self) -> bool:
        if not self._claude_path:
            return False
        # Cheap check: just verify the binary runs --version
        try:
            r = subprocess.run(
                [self._claude_path, "--version"],
                capture_output=True,
                timeout=5,
                text=True,
            )
            return r.returncode == 0
        except Exception:
            return False

    def generate(
        self, prompt: str, model: str = DEFAULT_MODEL, max_tokens: int = 1024, **kw
    ) -> Response:
        if not self._claude_path:
            raise RuntimeError("claude CLI not on PATH")

        # System prompt support: prepend to user prompt with a clear marker
        system = kw.get("system", "")
        if system:
            full_prompt = f"[System instructions]\n{system}\n\n[User request]\n{prompt}"
        else:
            full_prompt = prompt

        # claude -p reads prompt from arg or stdin. Use stdin for safety with
        # long/complex prompts (no shell-injection surface).
        # --strict-mcp-config with no --mcp-config: nested CLI loads ZERO MCP
        # servers; --settings disables hooks/plugins/statusline in the child.
        # Without these, every background synthesis call booted the full MCP
        # farm and re-fired all hooks (recursion) — a multi-GB commit charge
        # per call that helped exhaust virtual memory on 2026-06-10.
        nested_settings = str(
            Path(__file__).resolve().parent / "nested_claude_settings.json"
        )
        cmd = [
            self._claude_path,
            "-p",
            "--model",
            model,
            "--strict-mcp-config",
            "--settings",
            nested_settings,
        ]
        # Mark the child (and therefore its hooks, which inherit env) as a
        # headless worker: --settings disableAllHooks does NOT actually
        # suppress user-settings hooks (verified 2026-07-06 — every digest/
        # distiller call was writing a draft epilogue via SessionEnd).
        # session_end.py checks this and skips draft/marker/consolidate.
        child_env = {**os.environ, "CLAUDE_HEADLESS_WORKER": "1"}
        try:
            r = subprocess.run(
                cmd,
                input=full_prompt,
                capture_output=True,
                text=True,
                timeout=CLI_TIMEOUT_SEC,
                encoding="utf-8",
                errors="replace",
                env=child_env,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"claude CLI timed out after {CLI_TIMEOUT_SEC}s") from e
        except Exception as e:
            raise RuntimeError(f"claude CLI failed: {e}") from e

        if r.returncode != 0:
            err_preview = (r.stderr or "")[:300].strip()
            raise RuntimeError(f"claude CLI exit {r.returncode}: {err_preview}")

        text = (r.stdout or "").strip()
        # CLI doesn't expose token counts directly — estimate
        return Response(
            text=text,
            tokens_in=len(full_prompt) // 4,
            tokens_out=len(text) // 4,
            model=model,
            raw={"return_code": r.returncode, "stderr_excerpt": (r.stderr or "")[:200]},
        )


register("subscription", SubscriptionProvider)
