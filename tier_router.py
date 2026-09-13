"""
tier_router.py — route LLM workloads to the cheapest sufficient model.

Returns (provider_name, model_id, params) for a given task_type so callers
don't hard-code a model. Cost-aware: Haiku for mechanical extraction, Opus
for synthesis. Phase 6 layers a Provider abstraction on top of this.

Routing table is intentionally explicit — when in doubt, edit this file.
Telemetry to `_meta/router_log.jsonl` enables weekly cost rollups.

Usage:
  from tier_router import route, log_use
  provider, model, params = route("entity_extraction")
  ...call provider with model+params...
  log_use("entity_extraction", model, tokens_in=350, tokens_out=120)
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple

sys.path.insert(0, str(Path(__file__).parent))
import paths as _paths  # noqa: E402

META_DIR = _paths.META_DIR
ROUTER_LOG = META_DIR / "router_log.jsonl"


# Routing table — single source of truth for "which model for which job"
TIER_TABLE: dict[str, dict] = {
    # Mechanical, single-fact, deterministic — Haiku
    "entity_extraction": {
        "provider": "anthropic",
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 1024,
        "prefer_local": True,
    },
    "dedup_similarity": {
        "provider": "anthropic",
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 256,
        "prefer_local": True,
    },
    "provenance_chain": {
        "provider": "anthropic",
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 512,
        "prefer_local": True,
    },
    "summary_short": {
        "provider": "anthropic",
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 512,
        "prefer_local": True,
    },
    "hyde_expansion": {
        "provider": "anthropic",
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 300,
        "prefer_local": True,
    },
    "importance_scoring": {
        "provider": "anthropic",
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 150,
        "prefer_local": True,
    },
    # Middle ground — Sonnet
    "probe_scoring": {
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "max_tokens": 2048,
        "prefer_local": False,
    },
    # v15: sleep-time semantic compression of session observations. LOCAL-FIRST
    # by design — d'Artagnan does this for $0.00 and zero subscription quota;
    # haiku/subscription only when the lil guy is asleep. Summary quality bar
    # is "dense paragraph", well within a 4B model's reach.
    "observation_compression": {
        "provider": "anthropic",
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 768,
        "prefer_local": True,
        "slow_acceptable": True,
    },
    # memory_classification is sleep-time / background work (auto-promote draft
    # generation). slow_acceptable=True so the subscription fallback engages
    # when ANTHROPIC_API_KEY isn't set — avoids needing the dart-brain spill.
    # local_fallback: dart2 deep tier catches the "cloud unhealthy → skip
    # cycle" losses (2026-07-30 subconscious wiring).
    "memory_classification": {
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "max_tokens": 1024,
        "prefer_local": False,
        "slow_acceptable": True,
        "local_fallback": "dart2",
    },
    # Synthesis, judgment, narrative — Opus (background, slow_acceptable)
    "multi_doc_synthesis": {
        "provider": "anthropic",
        "model": "claude-opus-4-8",
        "max_tokens": 4096,
        "prefer_local": False,
        "slow_acceptable": True,
        "local_fallback": "dart2",
    },
    "epilogue_generation": {
        "provider": "anthropic",
        "model": "claude-opus-4-8",
        "max_tokens": 4096,
        "prefer_local": False,
        "slow_acceptable": True,
    },
    "weekly_digest_chapter": {
        "provider": "anthropic",
        "model": "claude-opus-4-8",
        "max_tokens": 4096,
        "prefer_local": False,
        "slow_acceptable": True,
    },
    "conflict_resolution": {
        "provider": "anthropic",
        "model": "claude-opus-4-8",
        "max_tokens": 2048,
        "prefer_local": False,
        "slow_acceptable": True,
    },
    # Default
    "default": {
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "max_tokens": 2048,
        "prefer_local": False,
    },
}


# Per-tier rough price-per-MTok (July 2026 list; update as Anthropic changes)
# Format: (input_per_mtok, output_per_mtok) in USD
# Sonnet 5 has intro pricing ($2/$10) through 2026-08-31 — sticker listed here.
PRICE_TABLE: dict[str, Tuple[float, float]] = {
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),  # legacy, still routable via force_model
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),  # legacy, still routable via force_model
    "local-qwen": (0.00, 0.00),  # d'Artagnan (e2b sidecar)
    "dart2-deep": (0.00, 0.00),  # dart2 subconscious — Qwen3.6-35B thinking
    "dart2-daily": (0.00, 0.00),
    "dart2-fast": (0.00, 0.00),
}

HAIKU_CONTEXT_LIMIT = 200_000  # generous; escalate if approaching


def route(
    task_type: str,
    input_tokens: int = 0,
    force_model: str | None = None,
    allow_local: bool = True,
) -> Tuple[str, str, dict]:
    """Returns (provider, model_id, params) tuple. Never raises.

    If the task spec sets `prefer_local: true` and d'Artagnan is reachable,
    routes to the local Qwen model. Falls back to Anthropic on health-check fail.
    Set `allow_local=False` to force cloud routing.
    """
    if force_model:
        for model, prices in PRICE_TABLE.items():
            if model == force_model:
                return ("anthropic", model, {"max_tokens": 2048})
        return ("anthropic", force_model, {"max_tokens": 2048})

    spec = TIER_TABLE.get(task_type) or TIER_TABLE["default"]
    provider = spec["provider"]
    model = spec["model"]
    params = {k: v for k, v in spec.items() if k not in ("provider", "model")}

    # Escalation rules
    if model.startswith("claude-haiku") and input_tokens > HAIKU_CONTEXT_LIMIT * 0.9:
        model = "claude-sonnet-5"
        params.setdefault("max_tokens", 2048)

    # Local-first opt-in (d'Artagnan)
    if allow_local and spec.get("prefer_local"):
        try:
            from providers import get_provider

            d = get_provider("dartagnan")
            if d.health_check():
                return ("dartagnan", "local-qwen", params)
        except Exception:
            pass  # fall back to cloud

    # v14.1: Subscription fallback for slow-acceptable tasks when no API key.
    # The Anthropic API key isn't set, but the user has a Claude Code
    # subscription — route background work through the `claude` CLI.
    if (
        spec.get("slow_acceptable")
        and not os.environ.get("ANTHROPIC_API_KEY")
        and not os.environ.get("MEMORY_DISABLE_SUBSCRIPTION_PROVIDER")
    ):
        try:
            from providers import get_provider

            sub = get_provider("subscription")
            if sub.health_check():
                # Use the same model name — subscription provider passes through
                return ("subscription", model, params)
        except Exception:
            pass

    # v16: dart2 subconscious fallback. Only reached when every cloud path is
    # out (API key missing/SDK broken AND subscription unhealthy). Callers
    # health-check whatever we return and skip the cycle on failure — with
    # dart2 in the chain, "cloud is down" no longer means "insight is lost".
    # dart2's governor still gets the final word on VRAM (refusal ->
    # generate() raises -> caller skips, exactly the old behavior).
    if spec.get("local_fallback") and allow_local:
        try:
            from providers import get_provider

            primary = get_provider(provider)
            if primary.health_check():
                return (provider, model, params)
        except Exception:
            pass
        try:
            from providers import get_provider

            d2 = get_provider(spec["local_fallback"])
            if d2.health_check():
                return (spec["local_fallback"], "dart2-deep", params)
        except Exception:
            pass

    return (provider, model, params)


def cost_estimate(model: str, tokens_in: int, tokens_out: int) -> float:
    price_in, price_out = PRICE_TABLE.get(model, (0.0, 0.0))
    return (tokens_in / 1_000_000) * price_in + (tokens_out / 1_000_000) * price_out


def log_use(
    task_type: str,
    model: str,
    tokens_in: int = 0,
    tokens_out: int = 0,
    provider: str = "anthropic",
    extra: dict | None = None,
) -> None:
    """Append a telemetry row. Best-effort, never raises."""
    try:
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "task_type": task_type,
            "provider": provider,
            "model": model,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_usd": round(cost_estimate(model, tokens_in, tokens_out), 6),
        }
        if extra:
            rec.update(extra)
        ROUTER_LOG.parent.mkdir(parents=True, exist_ok=True)
        with ROUTER_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except Exception:
        pass


def digest(days: int = 7) -> dict:
    """Sum cost + token usage by model across last `days`."""
    if not ROUTER_LOG.exists():
        return {"models": {}, "total_cost_usd": 0.0, "calls": 0}
    cutoff_ts = time.time() - days * 86400
    by_model: dict[str, dict] = {}
    total_cost = 0.0
    n = 0
    try:
        with ROUTER_LOG.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                ts = rec.get("ts", "")
                try:
                    rec_ts = datetime.fromisoformat(
                        ts.replace("Z", "+00:00")
                    ).timestamp()
                except Exception:
                    rec_ts = time.time()
                if rec_ts < cutoff_ts:
                    continue
                m = rec.get("model", "?")
                slot = by_model.setdefault(
                    m, {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}
                )
                slot["calls"] += 1
                slot["tokens_in"] += rec.get("tokens_in", 0)
                slot["tokens_out"] += rec.get("tokens_out", 0)
                slot["cost_usd"] = round(slot["cost_usd"] + rec.get("cost_usd", 0.0), 6)
                total_cost += rec.get("cost_usd", 0.0)
                n += 1
    except Exception:
        pass
    return {
        "models": by_model,
        "total_cost_usd": round(total_cost, 6),
        "calls": n,
        "days": days,
    }


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="tier_router CLI")
    sub = ap.add_subparsers(dest="cmd")
    p_route = sub.add_parser("route", help="Show routing for a task_type")
    p_route.add_argument("task_type")
    p_route.add_argument("--tokens", type=int, default=0)
    p_digest = sub.add_parser("digest", help="Cost rollup")
    p_digest.add_argument("--days", type=int, default=7)
    sub.add_parser("table", help="Print routing table")
    args = ap.parse_args()

    if args.cmd == "route":
        prov, model, params = route(args.task_type, args.tokens)
        print(
            json.dumps({"provider": prov, "model": model, "params": params}, indent=2)
        )
    elif args.cmd == "digest":
        print(json.dumps(digest(args.days), indent=2))
    elif args.cmd == "table":
        print(json.dumps(TIER_TABLE, indent=2))
    else:
        ap.print_help()
