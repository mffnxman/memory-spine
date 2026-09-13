"""TDD: a memory_write should make the changed memory vector-recallable.

The hot write path emits a 'reindex' event; the outbox must route it to
reindex_embeddings for just that memory. Before this change, kind='reindex'
hit the unknown-kind `else: pass` and nothing got embedded.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import memory_engine as me
from outbox_worker import process_event


def _hash_in_db(filename):
    with me.db() as c:
        row = c.execute(
            "SELECT content_hash FROM embeddings WHERE filename=?", (filename,)
        ).fetchone()
    return row[0] if row else None


def test_reindex_event_embeds_changed_memory():
    mems = me.list_memories()
    assert mems, "no memories on disk to test against"
    target = mems[0]

    # setup: known-missing embedding state (reindex will restore it)
    with me.db() as c:
        c.execute("DELETE FROM embeddings WHERE filename=?", (target.filename,))
    assert _hash_in_db(target.filename) is None

    # act: the new outbox 'reindex' kind should embed just this one memory.
    # platform_source mirrors what emit_event always stamps (Triware gate now treats
    # a missing source as untrusted; a real reindex event is always claude_code).
    process_event({"kind": "reindex", "platform_source": "claude_code",
                   "payload": {"file_path": str(target.path)}})

    # assert: embedding now exists and matches current content
    got = _hash_in_db(target.filename)
    assert got is not None, "reindex event did not create an embedding"
    assert got == me._content_hash(target), "embedding is stale after reindex"


def test_memory_write_hook_emits_reindex_event(monkeypatch):
    """Writing a memory must enqueue a reindex job (not just memory_write)."""
    import memory_write_postprocess as mwp
    import event_bus

    emitted = []
    monkeypatch.setattr(mwp, "_read_stdin_json",
                        lambda: {"tool_input": {"file_path": "fake_memory.md"}})
    monkeypatch.setattr(mwp, "_is_memory_path", lambda fp: True)
    # capture emits to avoid polluting the durable prod event log
    monkeypatch.setattr(event_bus, "emit_event", lambda kind, payload: emitted.append(kind))

    mwp.main()

    assert "memory_write" in emitted, "regression: memory_write no longer emitted"
    assert "reindex" in emitted, "write hook did not enqueue a reindex job"
