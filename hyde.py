"""
hyde.py — HyDE (Hypothetical Document Embeddings) query expansion.

Before searching the memory index, generate a hypothetical memory entry that
would answer the user's query. Embed THAT instead of the raw query. Closes
the distribution gap between question phrasing and memory phrasing.

Pipeline:
  user_prompt -> [provider via tier_router] -> hypothetical_memory_text
  hypothetical_memory_text -> embed -> search index

Routed via tier_router("hyde_expansion") which prefers local d'Artagnan
(Qwen) and falls back to Haiku.

Hard timeout: 1.5s (prefetch is a hot path).
Cache: 24h SQLite by sha256(prompt + detail_level) in _meta/hyde_cache.sqlite.

To disable globally: MEMORY_HYDE_DISABLED=1 in env.
Gated on prompt length > 30 chars — short prompts aren't worth the expansion.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
import paths as _paths  # noqa: E402

META_DIR = _paths.META_DIR
CACHE_DB = META_DIR / "hyde_cache.sqlite"

DISABLED = bool(os.environ.get("MEMORY_HYDE_DISABLED"))
MIN_PROMPT_LEN = 30          # below this — skip expansion (cost not worth it)
TIMEOUT_SEC = 1.5            # hard ceiling on provider call
CACHE_TTL_SEC = 24 * 3600    # 24h cache

HYDE_SYSTEM = (
    "You're generating a hypothetical memory entry from the user's personal knowledge "
    "base that would answer this user query. The entry should look like one of his "
    "existing markdown memory files: 2-4 declarative sentences focused on facts, "
    "preferences, decisions, or patterns. NOT speculation, NOT a question. Write as "
    "if recalling something already known."
)

HYDE_USER_TEMPLATE = (
    "User query: {prompt}\n\n"
    "Hypothetical memory entry (2-4 sentences):"
)


def _hyde_flag_enabled() -> bool:
    """HyDE defaults OFF. Measured 2026-06-03 on the 27 continuity cases: HyDE
    expansion REDUCED retrieval — 25/27 hits & MRR 0.864 (raw) fell to 19/27 &
    0.617 (expanded), breaking 6 passing cases and improving none. On a brain
    whose direct retrieval is already strong, the local model's hypothetical doc
    (often confidently wrong) dilutes the query embedding. Toggle on via
    feature_flags hyde_enabled=true to re-evaluate (e.g. with a better model or
    an embed-only-hypothetical variant)."""
    try:
        from feature_flags import all_flags
        flags = all_flags()
        if "hyde_enabled" in flags:
            return bool(flags["hyde_enabled"])
    except Exception:
        pass
    return False


def _hash_prompt(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:32]


# Conversational / refusal tells of a model answering as a chat companion instead
# of emitting a hypothetical memory document.
_REJECT_SIGNALS = (
    "i don't actually remember", "i don't remember", "fill me in",
    "no shared past", "no shared session", "no shared history",
    "as an ai", "i'm running on local", "i am running on local",
    "i don't have any memory", "i can't recall", "let me know what",
    "what's next", "how we looking", "how are we looking",
)


def _looks_like_document(text: str) -> bool:
    """True iff `text` reads like a hypothetical memory entry (prose), not a
    chat reply, refusal, question, or JSON blob.

    Defense-in-depth: even with the system prompt fixed, we never cache or embed
    something that isn't a document. On rejection HyDE just skips expansion and
    searches the raw query — strictly fail-soft."""
    if not text:
        return False
    t = text.strip()
    if len(t) < 15:
        return False
    # JSON / structured output rather than prose.
    if t[0] in "{[":
        return False
    low = t.lower()
    if '"trigger"' in low or '"action"' in low or '"insight"' in low:
        return False
    # A bare question is a query restatement, not a hypothetical answer doc.
    if t.endswith("?") and "." not in t:
        return False
    # Conversational persona / refusal chatter.
    if any(sig in low for sig in _REJECT_SIGNALS):
        return False
    return True


def _ensure_cache_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS hyde_cache (
            prompt_hash TEXT PRIMARY KEY,
            hypothetical TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            hit_count INTEGER DEFAULT 0
        );
    """)


def cache_get(prompt: str) -> Optional[str]:
    if not CACHE_DB.exists():
        return None
    try:
        with sqlite3.connect(CACHE_DB) as conn:
            _ensure_cache_schema(conn)
            row = conn.execute(
                "SELECT hypothetical, created_at FROM hyde_cache WHERE prompt_hash = ?",
                (_hash_prompt(prompt),),
            ).fetchone()
            if not row:
                return None
            text, created = row
            if time.time() - created > CACHE_TTL_SEC:
                return None
            conn.execute(
                "UPDATE hyde_cache SET hit_count = hit_count + 1 WHERE prompt_hash = ?",
                (_hash_prompt(prompt),),
            )
            return text
    except Exception:
        return None


def cache_put(prompt: str, hypothetical: str) -> None:
    try:
        META_DIR.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(CACHE_DB) as conn:
            _ensure_cache_schema(conn)
            conn.execute(
                "INSERT INTO hyde_cache(prompt_hash, hypothetical, created_at, hit_count) "
                "VALUES (?, ?, ?, 0) "
                "ON CONFLICT(prompt_hash) DO UPDATE SET hypothetical=excluded.hypothetical, "
                "created_at=excluded.created_at",
                (_hash_prompt(prompt), hypothetical, int(time.time())),
            )
    except Exception:
        pass


class _TimeoutResult:
    """Thread-runner with a hard timeout — works on Windows (no signal.alarm)."""
    def __init__(self):
        self.text = None
        self.error = None

    def _run(self, fn):
        try:
            self.text = fn()
        except Exception as e:
            self.error = e


def _call_provider_timed(user_prompt: str) -> Optional[str]:
    """Route through tier_router, hard timeout TIMEOUT_SEC."""
    def _do_call():
        from tier_router import route, log_use
        from providers import get_provider
        provider_name, model, params = route("hyde_expansion")
        prov = get_provider(provider_name)
        if not prov.health_check():
            if provider_name != "anthropic":
                prov = get_provider("anthropic")
                if not prov.health_check():
                    return None
        resp = prov.generate(
            HYDE_USER_TEMPLATE.format(prompt=user_prompt),
            model=model if provider_name == "anthropic" else "local-qwen",
            max_tokens=params.get("max_tokens", 300),
            system=HYDE_SYSTEM,
        )
        log_use("hyde_expansion", resp.model or model, resp.tokens_in, resp.tokens_out, provider=provider_name)
        return resp.text.strip() if resp.text else None

    result = _TimeoutResult()
    th = threading.Thread(target=result._run, args=(_do_call,), daemon=True)
    th.start()
    th.join(TIMEOUT_SEC)
    if th.is_alive():
        # Provider hung — abandon (the daemon thread will be GC'd)
        return None
    if result.error:
        return None
    return result.text


def expand(user_prompt: str) -> Optional[str]:
    """Return a hypothetical memory entry for the query, or None if unavailable.

    Order of operations:
      1. Disabled? return None
      2. Too short? return None
      3. Cache hit? return cached
      4. Timed provider call (1.5s hard ceiling)
      5. Cache + return result

    Never raises.
    """
    if DISABLED or not _hyde_flag_enabled():
        return None
    if not user_prompt or len(user_prompt.strip()) < MIN_PROMPT_LEN:
        return None
    cached = cache_get(user_prompt)
    if cached:
        return cached
    try:
        text = _call_provider_timed(user_prompt)
    except Exception:
        return None
    if not text or not _looks_like_document(text):
        # Provider returned nothing usable (or a chat/refusal/JSON). Skip
        # expansion rather than caching+embedding garbage; search the raw query.
        return None
    cache_put(user_prompt, text)
    return text


def stats() -> dict:
    if not CACHE_DB.exists():
        return {"entries": 0, "hits": 0}
    try:
        with sqlite3.connect(CACHE_DB) as conn:
            _ensure_cache_schema(conn)
            row = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(hit_count), 0) FROM hyde_cache"
            ).fetchone()
            return {"entries": row[0], "hits": row[1]}
    except Exception:
        return {"entries": 0, "hits": 0}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="hyde module CLI")
    sub = ap.add_subparsers(dest="cmd")
    p_expand = sub.add_parser("expand", help="Expand a query")
    p_expand.add_argument("prompt")
    sub.add_parser("stats", help="Show cache stats")
    sub.add_parser("purge", help="Delete cache")
    args = ap.parse_args()

    if args.cmd == "expand":
        result = expand(args.prompt)
        print(result if result else "(no expansion — provider unavailable or disabled)")
    elif args.cmd == "stats":
        print(json.dumps(stats(), indent=2))
    elif args.cmd == "purge":
        if CACHE_DB.exists():
            CACHE_DB.unlink()
        print("purged")
    else:
        ap.print_help()
