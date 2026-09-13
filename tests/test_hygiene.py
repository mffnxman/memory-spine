"""TDD: Wave C hygiene fixes.

C1 kg.db self-heals the v14 bitemporal columns on a fresh install (kg.db CREATE
   omits valid_*, but upsert_relationship INSERTs valid_from -> 'no such column',
   swallowed by the write hook = edgeless graph = dark PPR/boot hubs).
C5 observer privacy path extractor must also handle the plural 'paths' key
   (e.g. read_multiple_files) so an excluded path can't bypass redaction.
C2 provenance.prune must be schedulable (quiet, returns counts) so the unbounded
   snapshot store can be pruned from consolidation.
"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kg
import observation_capture as oc
import provenance


def test_kg_self_heals_bitemporal_columns():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        "CREATE TABLE relationships(id INTEGER PRIMARY KEY, created INTEGER);"
        "CREATE TABLE entity_mentions(entity_id INTEGER, ts INTEGER);"
    )
    kg._ensure_bitemporal_columns(conn)
    rel = [r[1] for r in conn.execute("PRAGMA table_info(relationships)")]
    em = [r[1] for r in conn.execute("PRAGMA table_info(entity_mentions)")]
    assert {"valid_from", "valid_to", "superseded_by"}.issubset(set(rel))
    assert {"valid_from", "valid_to"}.issubset(set(em))


def test_observer_extracts_plural_paths_key():
    got = oc._extract_file_paths("read_multiple_files", {"paths": ["/a.txt", "/b.txt"]})
    assert "/a.txt" in got and "/b.txt" in got


def test_provenance_prune_quiet_returns_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, "PROV_DIR", tmp_path)
    slug = tmp_path / "mem-x"
    slug.mkdir()
    (slug / "20200101-000000-init.md").write_text("oldest-kept", encoding="utf-8")
    (slug / "20200102-000000-v2.md").write_text("old-pruned", encoding="utf-8")
    res = provenance.prune(days=30, quiet=True)
    assert isinstance(res, dict)
    assert res["removed"] >= 1
    assert (slug / "20200101-000000-init.md").exists()  # oldest always kept
