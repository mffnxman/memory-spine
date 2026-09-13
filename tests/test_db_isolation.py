"""Guard: the test suite must NEVER run against the production memory.db.

Several tests do `DELETE FROM heuristics` / `DELETE FROM embeddings` and mint
rows; without isolation those run against the real _meta/memory.db and corrupt
live data on every `pytest` invocation. A conftest copies the DB to a temp file
and points MEMORY_DB_PATH at it. This test asserts that isolation is in effect.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import paths as _paths  # noqa: E402
import memory_engine as me

import os

PROD_DB = (Path(os.environ["MEMORY_HOME_SOURCE"]) / "_meta" / "memory.db").resolve()


def test_db_path_is_isolated_from_production():
    assert Path(me.DB_PATH).resolve() != PROD_DB, (
        "tests are running against PRODUCTION memory.db — the suite's "
        "DELETE FROM heuristics/embeddings would corrupt real data. "
        "conftest memory-home isolation is not active."
    )


def test_isolated_db_is_a_copy_with_real_data():
    # Isolation must be a COPY of prod (not an empty DB), because data-dependent
    # tests (index_health, reindex_on_write) assert real memories exist.
    assert me.list_memories(), "isolated memory home should be a non-empty copy of the source corpus"
