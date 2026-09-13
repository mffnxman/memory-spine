"""TDD P5: lifecycle — ExpeL importance counter + bitemporal supersession.

Closes MemSkill's designer loop with our blended signal: heuristics that helped
(session went well after injection) get upvoted; ones that didn't get downvoted;
importance reaching 0 archives them. Refined heuristics supersede their parents
bitemporally (same mechanism the KG uses for facts).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import procedural_lib as pl
import memory_engine as me

H = {"trigger": "running python3 on Windows hits a UnicodeEncodeError",
     "action": "set PYTHONIOENCODING=utf-8 first", "insight": ""}


def _reset():
    pl.ensure_schema()
    with me.db() as c:
        c.execute("DELETE FROM heuristics")
        c.commit()


def test_upvote_increments_importance_and_corroboration():
    _reset()
    hid = pl.upsert_heuristic(H, origin="test", polarity="failure_derived")["id"]
    pl.upvote(hid)
    with me.db() as c:
        imp, cor = c.execute("SELECT importance, corroboration FROM heuristics WHERE id=?", (hid,)).fetchone()
    assert imp == 3 and cor == 2


def test_downvote_to_zero_archives():
    _reset()
    hid = pl.upsert_heuristic(H, origin="test", polarity="failure_derived")["id"]  # importance 2
    pl.downvote(hid)  # 1
    pl.downvote(hid)  # 0 -> archived
    with me.db() as c:
        imp, st = c.execute("SELECT importance, status FROM heuristics WHERE id=?", (hid,)).fetchone()
    assert imp <= 0 and st == "archived"


def test_prune_archives_nonpositive_importance():
    _reset()
    keep = pl.upsert_heuristic(H, origin="test", polarity="failure_derived")["id"]
    with me.db() as c:
        c.execute("UPDATE heuristics SET importance=0 WHERE id=?", (keep,))
        c.execute("INSERT INTO heuristics (trigger,action,origin,polarity,importance,created_ts,status) "
                  "VALUES ('t2','a2','test','failure_derived',2,1,'active')")
        c.commit()
    n = pl.prune()
    assert n == 1
    with me.db() as c:
        active = c.execute("SELECT count(*) FROM heuristics WHERE status='active'").fetchone()[0]
    assert active == 1


def test_supersede_retires_old_and_excludes_from_retrieval():
    _reset()
    old = pl.upsert_heuristic(H, origin="test", polarity="failure_derived")["id"]
    new = pl.upsert_heuristic(
        {"trigger": "deploying to the new cloud region", "action": "check quota first", "insight": ""},
        origin="test", polarity="failure_derived")["id"]
    pl.supersede(old, new)
    with me.db() as c:
        st, vt, sb = c.execute("SELECT status, valid_to, superseded_by FROM heuristics WHERE id=?", (old,)).fetchone()
    assert st == "superseded" and vt is not None and sb == new
    # superseded heuristic must not surface in retrieval
    assert all(h["id"] != old for h in pl.retrieve("UnicodeEncodeError python", k=5))


def test_record_outcome_upvotes_on_success_downvotes_on_failure():
    _reset()
    hid = pl.upsert_heuristic(H, origin="test", polarity="failure_derived")["id"]
    pl.record_outcome([hid], success=True)
    with me.db() as c:
        imp = c.execute("SELECT importance FROM heuristics WHERE id=?", (hid,)).fetchone()[0]
    assert imp == 3
    pl.record_outcome([hid], success=False)
    with me.db() as c:
        imp = c.execute("SELECT importance FROM heuristics WHERE id=?", (hid,)).fetchone()[0]
    assert imp == 2


def test_mark_used_bumps_use_count():
    _reset()
    hid = pl.upsert_heuristic(H, origin="test", polarity="failure_derived")["id"]
    pl.mark_used([hid])
    pl.mark_used([hid])
    with me.db() as c:
        uc = c.execute("SELECT use_count FROM heuristics WHERE id=?", (hid,)).fetchone()[0]
    assert uc == 2
