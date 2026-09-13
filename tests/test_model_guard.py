"""TDD: vector_search must ignore embeddings written by a different model.

The embeddings table records the `model` used, but vector_search selected only
(filename, vector) and cosine-compared everything. Swapping EMBEDDING_MODEL
without `reindex --force` would then silently compare incompatible vectors.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import memory_engine as me


def test_vector_search_skips_model_mismatch():
    mems = me.list_memories()
    assert mems
    target = mems[0]
    q = f"{target.name} {target.description}"

    # sanity: with the correct model, the target is retrievable
    pre = [m.filename for m, _ in me.vector_search(q, mems=mems, top_k=len(mems))]
    assert target.filename in pre, "precondition failed: target not retrievable"

    # setup: corrupt the model tag on the target's stored embedding
    with me.db() as c:
        c.execute("UPDATE embeddings SET model=? WHERE filename=?",
                  ("STALE_MODEL_X", target.filename))
    try:
        post = [m.filename for m, _ in me.vector_search(q, mems=mems, top_k=len(mems))]
        assert target.filename not in post, "returned a model-mismatched embedding"
    finally:
        me.reindex_embeddings([target], force=True)  # restore correct row
