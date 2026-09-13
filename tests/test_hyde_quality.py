"""HyDE query-expansion quality — the local provider must honor the system prompt.

2026-06-03 recon finding: 49/58 hyde_cache entries were NOT hypothetical memory
documents — they were dart-fast-4b answering CONVERSATIONALLY in its d'Artagnan
companion persona ("Hey, I don't actually remember that — fill me in?").
Root cause: dartagnan_provider.generate() dropped the `system` kwarg, so
HYDE_SYSTEM ("write a hypothetical entry, NOT a question") never reached the
local model — it used its baked-in Modelfile persona instead. Those chatty
strings were then embedded as the query expansion, degrading retrieval.

Fix: (1) dartagnan provider passes `system` as an Ollama system-role message;
(2) hyde validates output looks like a document before caching it.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hyde
from providers.dartagnan_provider import DartagnanProvider


# ---- HyDE output validation ----

def test_rejects_conversational_refusal():
    assert hyde._looks_like_document(
        "Hey, I don't actually remember that — fill me in? No shared past here.") is False


def test_rejects_json_blob():
    assert hyde._looks_like_document('{"trigger": "x", "action": "y", "insight": "z"}') is False


def test_rejects_a_question():
    assert hyde._looks_like_document("What did the user decide about the reranker daemon?") is False


def test_accepts_prose_memory_entry():
    assert hyde._looks_like_document(
        "the user prefers building tools over buying them. He runs a homemade memory "
        "system with an event bus and a reranker sidecar daemon on localhost."
    ) is True


def test_expand_rejects_and_does_not_cache_garbage(monkeypatch):
    monkeypatch.setattr(hyde, "DISABLED", False)
    monkeypatch.setattr(hyde, "_hyde_flag_enabled", lambda: True)
    monkeypatch.setattr(hyde, "cache_get", lambda p: None)
    put = []
    monkeypatch.setattr(hyde, "cache_put", lambda p, t: put.append((p, t)))
    monkeypatch.setattr(hyde, "_call_provider_timed", lambda p: "Hey, no shared past here, what's next?")
    out = hyde.expand("what did we decide about the reranker daemon architecture today")
    assert out is None
    assert put == [], "conversational garbage must never be cached"


def test_expand_caches_a_valid_document(monkeypatch):
    monkeypatch.setattr(hyde, "DISABLED", False)
    monkeypatch.setattr(hyde, "_hyde_flag_enabled", lambda: True)
    monkeypatch.setattr(hyde, "cache_get", lambda p: None)
    put = []
    monkeypatch.setattr(hyde, "cache_put", lambda p, t: put.append((p, t)))
    doc = ("the user runs a homemade memory system with a content-addressed event bus "
           "and a warm reranker daemon bound to localhost.")
    monkeypatch.setattr(hyde, "_call_provider_timed", lambda p: doc)
    out = hyde.expand("what is the user's memory architecture")
    assert out == doc
    assert put and put[0][1] == doc


# ---- dartagnan provider must forward `system` to the e2b sidecar ----

class _FakeResp:
    status = 200
    def __init__(self, payload):
        self._p = payload
    def read(self):
        return json.dumps(self._p).encode("utf-8")
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def _fake_sidecar(captured):
    """urlopen fake for the e2b sidecar: healthy /health, OpenAI-shaped chat."""
    def fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "/health" in url:
            return _FakeResp({"status": "ok"})
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp({
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "model": "dart-e2b", "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })
    return fake_urlopen


def test_dartagnan_forwards_system_as_message(monkeypatch):
    captured = {}
    monkeypatch.setattr("providers.dartagnan_provider.urlopen", _fake_sidecar(captured))
    DartagnanProvider().generate("the query", model="local-qwen", system="WRITE A DOCUMENT, NOT A CHAT")
    msgs = captured["body"]["messages"]
    assert msgs[0] == {"role": "system", "content": "WRITE A DOCUMENT, NOT A CHAT"}
    assert msgs[-1] == {"role": "user", "content": "the query"}
    # Gemma 4 opens a reasoning block by default; a small max_tokens budget
    # returns empty content unless thinking is disabled per-request (T3.4).
    assert captured["body"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_expand_disabled_by_flag_skips_provider_and_cache(monkeypatch):
    # Measured 2026-06-03: HyDE REDUCES retrieval (25->19/27 hits, MRR
    # 0.864->0.617) — default OFF. expand() must short-circuit before any
    # provider call or cache read so it adds zero hot-path cost.
    monkeypatch.setattr(hyde, "DISABLED", False)
    monkeypatch.setattr(hyde, "_hyde_flag_enabled", lambda: False)
    monkeypatch.setattr(hyde, "_call_provider_timed",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("provider called while HyDE disabled")))
    monkeypatch.setattr(hyde, "cache_get",
                        lambda p: (_ for _ in ()).throw(AssertionError("cache read while HyDE disabled")))
    assert hyde.expand("a sufficiently long query about the memory architecture") is None


def test_expand_enabled_flag_allows_cache_hit(monkeypatch):
    monkeypatch.setattr(hyde, "DISABLED", False)
    monkeypatch.setattr(hyde, "_hyde_flag_enabled", lambda: True)
    doc = "the user's brain uses a content-addressed event bus and a warm reranker daemon."
    monkeypatch.setattr(hyde, "cache_get", lambda p: doc)
    assert hyde.expand("what is the memory architecture in this brain") == doc


def test_dartagnan_without_system_is_backward_compatible(monkeypatch):
    captured = {}
    monkeypatch.setattr("providers.dartagnan_provider.urlopen", _fake_sidecar(captured))
    resp = DartagnanProvider().generate("the query", model="local-qwen")
    msgs = captured["body"]["messages"]
    assert len(msgs) == 1 and msgs[0]["role"] == "user"
    # legacy aliases (local-qwen, dart-fast-4b) must map to the e2b model
    assert captured["body"]["model"] == "dart-e2b"
    assert resp.text == "ok"
