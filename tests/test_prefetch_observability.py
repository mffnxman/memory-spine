"""TDD: prefetch leaves a breadcrumb when the retrieval path fails (recon H6).

prefetch.main() swallows any search error and returns silently so it never blocks
the prompt — correct, but it left a degraded retrieval path indistinguishable from
a healthy idle one. It must now emit a telemetry breadcrumb while staying silent
to the user.
"""
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import prefetch
import memory_engine


def test_prefetch_search_error_emits_breadcrumb(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": "a genuine prompt to retrieve on"})))
    monkeypatch.setattr(prefetch, "list_memories", lambda: [])

    def _boom(*a, **k):
        raise RuntimeError("search blew up")
    monkeypatch.setattr(prefetch, "search_hybrid", _boom)

    caught = []
    monkeypatch.setattr(memory_engine, "_log_telemetry", lambda rec: caught.append(rec))
    prefetch.main()  # must not raise (fail-soft preserved)
    assert any(r.get("event") == "prefetch_search_error" for r in caught)
