"""dart2 provider — the subconscious lane (2026-07-30, beef-up day).

Routes the memory stack's synthesis-grade background work to the dart2
daemon (127.0.0.1:8180) when cloud providers are unavailable. Before this,
reflection synthesis and candidate drafting SKIPPED the whole cycle on
provider-unhealthy — sleep-time insight was lost to cloud outages/quota.
dart2's deep tier (Qwen3.6-35B-A3B thinking, ~34 tok/s) is a credible
synthesis fallback, and unlike the old dart-brain 30B spill problem, dart2's
governor owns VRAM floors — if the machine can't afford the model, dart2
refuses and we skip the cycle exactly like before. Strictly better.

Distinct from DartagnanProvider (the e2b sidecar on :8766): that one serves
MECHANICAL lanes (scoring, compression) with a 1.4GB always-cheap model.
This one serves THINKING lanes, at dart2's discretion.

Transport: dart2's own API (not OpenAI-compat). Bearer token read fresh from
~/.dart2/token per call — the daemon regenerates it on restart. /chat is SSE;
we collect deltas and the final frame. /chat accepts no max_tokens or
temperature — dart2 governs its own sampling per tier; those kwargs are
accepted here and ignored by design.

Model names map to dart2 tiers:
  dart2-deep (default) -> deep · dart2-daily -> daily · dart2-fast -> failover
Anything else (claude-* etc.) -> deep, so tier_router fallbacks Just Work.

Wake: if the daemon is down and DART2_URL isn't overridden, kick the
`dart2-daemon` scheduled task (respawn loop) — at most once per process.
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

from providers import register
from providers.base import Provider, Response

DEFAULT_URL = "http://127.0.0.1:8180"
TOKEN_PATH = Path.home() / ".dart2" / "token"
SCHTASK_NAME = "dart2-daemon"

HEALTH_TIMEOUT_SEC = 1.5
BOOT_WAIT_SEC = 30  # daemon boot is quick; model loads happen per-request
# Deep tier thinks before it speaks — the socket can be silent for minutes
# before the first delta. Per-read timeout must cover the longest silent gap.
READ_TIMEOUT_SEC = 300
TOTAL_DEADLINE_SEC = 600

TIER_MAP = {
    "dart2-deep": "deep",
    "dart2-daily": "daily",
    "dart2-fast": "failover",
    "dart2-failover": "failover",
}


class Dart2Provider(Provider):
    name = "dart2"

    def __init__(self):
        self.base_url = os.environ.get("DART2_URL", DEFAULT_URL).rstrip("/")
        self._wake_attempted = False

    # -- auth --------------------------------------------------------------

    def _headers(self) -> dict:
        """Token read per-call, never cached: the daemon rewrites
        ~/.dart2/token on every restart and a stale cached value would 401
        forever. Missing file -> empty header -> clean 401, not a crash."""
        try:
            tok = TOKEN_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            tok = ""
        return {
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json",
        }

    # -- lifecycle ---------------------------------------------------------

    def _server_up(self, timeout: float = HEALTH_TIMEOUT_SEC) -> bool:
        try:
            req = Request(
                f"{self.base_url}/status", method="GET", headers=self._headers()
            )
            with urlopen(req, timeout=timeout) as r:
                return r.status == 200
        except (URLError, HTTPError, TimeoutError, OSError):
            return False

    def _wake_daemon(self) -> None:
        """Kick the dart2-daemon respawn loop via Task Scheduler. Once per
        process — a daemon that can't start must not become a kick storm."""
        if self._wake_attempted:
            return
        self._wake_attempted = True
        try:
            subprocess.run(
                ["schtasks", "/run", "/tn", SCHTASK_NAME],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except Exception:
            pass

    def health_check(self) -> bool:
        """Fast contract like the other providers: cold daemon -> kick the
        wake and report False; THIS call falls through to the next provider,
        a later call finds dart2 warm."""
        if self._server_up():
            return True
        if os.environ.get("DART2_URL"):
            return False  # explicit endpoint (test shim) — never wake
        self._wake_daemon()
        return self._server_up(timeout=0.5)

    def _ensure_up(self) -> bool:
        """Committed path — sleep-time callers can afford the boot wait."""
        if self._server_up():
            return True
        if os.environ.get("DART2_URL"):
            return False
        self._wake_daemon()
        deadline = time.time() + BOOT_WAIT_SEC
        while time.time() < deadline:
            time.sleep(1.0)
            if self._server_up():
                return True
        return False

    # -- generation --------------------------------------------------------

    def generate(
        self, prompt: str, model: str = "dart2-deep", max_tokens: int = 1024, **kw
    ) -> Response:
        # max_tokens/temperature intentionally unused — dart2 governs its own
        # sampling and budgets per tier (see module docstring).
        tier = TIER_MAP.get(model, "deep")

        if not self._ensure_up():
            raise RuntimeError("dart2 unreachable: daemon did not come up")

        system = kw.get("system")
        text = f"[system instructions]\n{system}\n\n{prompt}" if system else prompt

        body: dict = {"text": text, "tier": tier}
        if isinstance(kw.get("think"), bool):
            body["think"] = kw["think"]

        req = Request(
            f"{self.base_url}/chat",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers=self._headers(),
        )

        deltas: list[str] = []
        final: dict = {}
        error_reason = None
        deadline = time.time() + TOTAL_DEADLINE_SEC
        try:
            with urlopen(req, timeout=READ_TIMEOUT_SEC) as r:
                for raw_line in r:
                    if time.time() > deadline:
                        raise RuntimeError("dart2 stream exceeded total deadline")
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data: "):
                        continue
                    try:
                        item = json.loads(line[6:])
                    except json.JSONDecodeError:
                        continue
                    kind = item.get("type")
                    if kind == "delta":
                        deltas.append(item.get("text", ""))
                    elif kind == "final":
                        final = item
                    elif kind == "error":
                        error_reason = item.get("reason", "unknown")
        except (URLError, HTTPError, TimeoutError, OSError) as e:
            raise RuntimeError(f"dart2 unreachable: {e}") from e

        if error_reason:
            # GovernorRefusal lands here too — machine can't afford the model
            # right now. Caller treats it like any provider failure and the
            # cycle re-fires later, same as the old skip behavior.
            raise RuntimeError(f"dart2 refused: {error_reason}")

        out_text = final.get("reply") or "".join(deltas)
        # dart2 reports no token usage — rough estimate so router telemetry
        # isn't blind (cost is $0 either way).
        return Response(
            text=out_text,
            tokens_in=len(prompt) // 4,
            tokens_out=len(out_text) // 4,
            model=f"dart2-{final.get('tier', tier)}",
            raw=final or None,
        )


register("dart2", Dart2Provider)
