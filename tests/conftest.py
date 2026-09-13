"""pytest session isolation — never mutate the real memory.

Several tests DELETE rows, mint embeddings, append `related:` links to memory
files, regenerate MEMORY.md and write epilogues. Without isolation they would
do that to whatever MEMORY_HOME points at (the starter corpus in
examples/memory, or your live memory). This conftest therefore:

  1. resolves the source memory dir (MEMORY_HOME, else the parent of _scripts),
  2. copies the whole tree (markdown + _meta, minus logs) to a temp dir,
  3. points MEMORY_HOME and MEMORY_DB_PATH at the copy BEFORE `paths` or
     `memory_engine` are imported anywhere in the suite.

Data-dependent tests still see a full copy of real content; the source tree
stays byte-identical. Run `python examples/seed_examples.py` first so the copy
carries embeddings and a heuristic pool.
"""

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent
_SRC_HOME = (
    Path(os.environ.get("MEMORY_HOME") or _SCRIPTS.parent).expanduser().resolve()
)

_tmp_dir = Path(tempfile.mkdtemp(prefix="memtest-"))
_test_home = _tmp_dir / "memory"
_pristine_dir = _tmp_dir / "pristine"
_pristine_dir.mkdir()

_SKIP_SUFFIX = {".log", ".jsonl"}


def _copy_tree(src: Path, dst: Path) -> None:
    for p in src.rglob("*"):
        rel = p.relative_to(src)
        if "_scripts" in rel.parts or "__pycache__" in rel.parts:
            continue
        if p.is_dir():
            (dst / rel).mkdir(parents=True, exist_ok=True)
            continue
        if p.suffix in _SKIP_SUFFIX:
            continue
        (dst / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst / rel)


if _SRC_HOME.exists():
    _copy_tree(_SRC_HOME, _test_home)
else:
    _test_home.mkdir(parents=True)
(_test_home / "_meta").mkdir(exist_ok=True)

_test_db = _test_home / "_meta" / "memory.db"
_PROD_DB = _SRC_HOME / "_meta" / "memory.db"
if _PROD_DB.exists():
    for suffix in ("", "-wal", "-shm"):
        src = _PROD_DB.with_name(_PROD_DB.name + suffix)
        if src.exists():
            shutil.copy2(src, _test_db.with_name(_test_db.name + suffix))
            shutil.copy2(src, _pristine_dir / src.name)

# Redirect the engine to the copy before it is imported by any test module.
os.environ["MEMORY_HOME_SOURCE"] = str(_SRC_HOME)  # what tests must NOT touch
os.environ["MEMORY_HOME"] = str(_test_home)
os.environ["MEMORY_DB_PATH"] = str(_test_db)

sys.path.insert(0, str(_SCRIPTS))

import gc  # noqa: E402
import sqlite3  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True, scope="module")
def _fresh_db_per_module():
    """Restore the DB copy from pristine before every test module.

    monkeypatch restores stubbed functions, but DATA written through a stub
    (e.g. test_genesis reindexing with fake [1,0,...] vectors) persisted in
    the shared copy and poisoned every later data-dependent module. The engine
    bakes DB_PATH at import, so we refresh the file's contents in place rather
    than repointing it, via the sqlite backup API (lingering connections from
    earlier modules hold Windows file locks that make shutil.copy2 raise).
    """
    gc.collect()  # release un-closed sqlite connections' locks where possible
    pristine = _pristine_dir / _PROD_DB.name
    if pristine.exists():
        src = sqlite3.connect(pristine)
        dst = sqlite3.connect(_test_db)
        try:
            dst.execute("PRAGMA busy_timeout=5000")
            src.backup(dst)
        finally:
            src.close()
            dst.close()
    # The DB restore alone is not enough: memory_engine keeps a module-level
    # filename->vector cache, so fake vectors minted through a stubbed
    # embed_text outlive both the monkeypatch and the DB refresh.
    try:
        import memory_engine as me

        me._embedding_cache.clear()
    except Exception:
        pass
    yield


@atexit.register
def _cleanup_test_dir():
    shutil.rmtree(_tmp_dir, ignore_errors=True)
