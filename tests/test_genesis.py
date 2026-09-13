"""TDD: Spine Genesis — n=1 novelty -> promotion candidate (candidates-only, OFF)."""
import json, os, sys, time
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import consolidate_worker as cw


def test_legacy_pass_raise_still_stamps_coalesce_marker(tmp_path, monkeypatch):
    m = tmp_path / ".lw"
    monkeypatch.setattr(cw, "_LAST_CONSOLIDATE_MARKER", m)
    monkeypatch.setattr(cw, "_log", lambda rec: None)
    def _boom():
        raise RuntimeError("pass blew up")
    monkeypatch.setattr(cw, "_near_duplicate_pass", _boom)
    with pytest.raises(RuntimeError, match="pass blew up"):
        cw.process_consolidate({"force": True})
    assert m.exists()  # finally stamped despite the raise -> no re-queue freeze


def test_genesis_flag_registered():
    import feature_flags
    flags = feature_flags.all_flags()
    assert "genesis_enabled" in flags
    assert isinstance(flags["genesis_enabled"], bool)
    # The live value is operator-controlled (trialed ON 2026-06-04), so we don't pin
    # it here. The default-OFF CONTRACT is that an UNSET flag resolves to False:
    assert feature_flags.is_enabled("definitely_not_a_real_flag_xyz") is False


def test_run_skips_when_flag_off(monkeypatch):
    import genesis
    monkeypatch.setattr(genesis, "_enabled", lambda name: False)
    assert genesis.run() == {"skipped": "flag_off"}


def test_log_telemetry_uses_genesis_component(tmp_path, monkeypatch):
    import genesis
    monkeypatch.setattr(genesis, "TELEMETRY_PATH", tmp_path / "t.jsonl")
    genesis._log_telemetry({"event": "x"})
    line = json.loads((tmp_path / "t.jsonl").read_text().strip())
    assert line["component"] == "genesis" and "ts" in line


def test_finalized_epilogues_excludes_drafts_and_marked(tmp_path, monkeypatch):
    import genesis
    monkeypatch.setattr(genesis, "EPILOGUE_DIR", tmp_path)
    (tmp_path / "draft-2026-06-01-1000.md").write_text("draft", encoding="utf-8")
    (tmp_path / "2026-06-02-1000.md").write_text("final A", encoding="utf-8")
    b = tmp_path / "2026-06-03-1000.md"
    b.write_text("final B", encoding="utf-8")
    (tmp_path / ("2026-06-03-1000.md" + genesis.MARKER_SUFFIX)).write_text("1", encoding="utf-8")
    names = [p.name for p in genesis._finalized_epilogues()]
    assert names == ["2026-06-02-1000.md"]  # draft excluded, B skipped (marked)


EPI_SAMPLE = """---
date: 2026-06-03 15:53:01
session: demo
---

## What we built / did
- built a thing

## What mattered
The loop is now alive and self-improving in a way it was not before today.

## What surprised / hit different
Two recursive moments hit different: the system caught itself being trained.

## Open threads (for next-me)
- something

## A note to next-me
hey

## What the observer caught (auto-ingested)
- Bash | secret-postscript-token | should never be embedded
"""


def test_extract_combines_mattered_and_surprised_excludes_postscript():
    import genesis
    txt = genesis._extract_significant_text(EPI_SAMPLE)
    assert "self-improving" in txt and "recursive moments" in txt
    assert "secret-postscript-token" not in txt  # postscript excluded
    assert "built a thing" not in txt             # "What we built" not in centroid


def test_is_significant_rejects_thin_and_not_captured():
    import genesis
    assert genesis._is_significant("x" * (genesis.GENESIS_MIN_SECTION_CHARS + 5)) is True
    assert genesis._is_significant("(not captured)") is False
    assert genesis._is_significant("short") is False
    assert genesis._is_significant("") is False


def test_index_clean_true_only_when_zero_missing_and_stale(monkeypatch):
    import genesis, memory_engine as me
    monkeypatch.setattr(me, "index_health", lambda: {"memories": 5, "embedded": 5, "missing": 0, "stale": 0})
    assert genesis._index_clean() is True
    monkeypatch.setattr(me, "index_health", lambda: {"memories": 5, "embedded": 4, "missing": 1, "stale": 0})
    assert genesis._index_clean() is False


def test_index_clean_false_when_health_raises(monkeypatch):
    import genesis, memory_engine as me
    def _boom():
        raise RuntimeError("db locked")
    monkeypatch.setattr(me, "index_health", _boom)
    assert genesis._index_clean() is False


def test_novelty_fail_closed_on_embed_none(monkeypatch):
    import genesis, memory_engine as me
    monkeypatch.setattr(me, "embed_text", lambda t: None)
    assessable, sim, closest = genesis._novelty("anything")
    assert assessable is False and sim == 1.0


def test_novelty_fail_closed_on_empty_corpus(monkeypatch):
    import genesis, memory_engine as me
    monkeypatch.setattr(me, "embed_text", lambda t: [1.0] + [0.0] * 383)
    monkeypatch.setattr(genesis, "_spine_vectors", lambda: [])
    assessable, _, _ = genesis._novelty("anything")
    assert assessable is False  # empty -> cannot call anything novel


def test_novelty_fail_closed_on_spine_query_raise(monkeypatch):
    import genesis, memory_engine as me
    monkeypatch.setattr(me, "embed_text", lambda t: [1.0] + [0.0] * 383)
    def _boom():
        raise RuntimeError("db error")
    monkeypatch.setattr(genesis, "_spine_vectors", _boom)
    assessable, _, _ = genesis._novelty("anything")
    assert assessable is False


def test_novelty_computes_max_cosine(monkeypatch):
    import genesis, memory_engine as me
    monkeypatch.setattr(me, "embed_text", lambda t: [1.0] + [0.0] * 383)
    monkeypatch.setattr(genesis, "_spine_vectors", lambda: [
        ("far.md", [0.0, 1.0] + [0.0] * 382),   # cosine 0
        ("near.md", [1.0] + [0.0] * 383),        # cosine 1
    ])
    assessable, sim, closest = genesis._novelty("anything")
    assert assessable is True and round(sim, 3) == 1.0 and closest == "near.md"


def test_novelty_embeds_at_input_cap(monkeypatch):
    import genesis, memory_engine as me
    captured = {}
    def fake_embed(text):
        captured["len"] = len(text)
        return [1.0] + [0.0] * 383
    monkeypatch.setattr(me, "embed_text", fake_embed)
    monkeypatch.setattr(genesis, "_spine_vectors", lambda: [("a.md", [1.0] + [0.0] * 383)])
    genesis._novelty("x" * 9999)
    assert captured["len"] <= me.EMBED_INPUT_CHARS


def test_genesis_marker_distinct_from_reviewed(tmp_path):
    import genesis
    epi = tmp_path / "2026-06-02-1000.md"
    epi.write_text("x", encoding="utf-8")
    genesis._mark(epi)
    assert (tmp_path / "2026-06-02-1000.md.genesis").exists()
    # must NOT satisfy procedural_lib's '.reviewed' existence check
    assert not (tmp_path / "2026-06-02-1000.md.reviewed").exists()
    assert genesis.MARKER_SUFFIX == ".genesis"


class _Resp:
    def __init__(self, text):
        self.text = text; self.tokens_in = 1; self.tokens_out = 2; self.model = "m"; self.raw = None


def _draft_env(monkeypatch, provider, route_val=("anthropic", "claude-sonnet-4-6", {})):
    import genesis
    monkeypatch.setattr("tier_router.route", lambda t: route_val)
    monkeypatch.setattr("tier_router.log_use", lambda *a, **k: None)
    monkeypatch.setattr("providers.get_provider", lambda n: provider)
    monkeypatch.setattr(genesis, "_log_telemetry", lambda r: None)


def test_genesis_prompt_is_single_session_not_recurrence():
    import genesis
    p = genesis.GENESIS_PROMPT.lower()
    assert "single session" in p
    assert "recurring pattern across" not in p
    assert "episodes" not in p  # never frames n=1 as a multi-episode cluster


def test_draft_declines_on_non_json(monkeypatch):
    import genesis
    class _Echo:
        def health_check(self): return True
        def generate(self, prompt, **kw): return _Resp(prompt)  # echo -> decline gate
    _draft_env(monkeypatch, _Echo())
    assert genesis._draft_single_session("rich session text", "2026-06-02-1000.md") is None


def test_draft_declines_on_null_name(monkeypatch):
    import genesis
    class _Decline:
        def health_check(self): return True
        def generate(self, prompt, **kw): return _Resp('{"name": null, "reason": "no novel insight"}')
    _draft_env(monkeypatch, _Decline())
    assert genesis._draft_single_session("rich text", "e.md") is None


def test_draft_skips_when_provider_unhealthy(monkeypatch):
    import genesis
    class _Unhealthy:
        def health_check(self): return False
        def generate(self, prompt, **kw): raise AssertionError("must not generate when unhealthy")
    _draft_env(monkeypatch, _Unhealthy())
    assert genesis._draft_single_session("rich text", "e.md") is None


def test_draft_returns_parsed_with_model(monkeypatch):
    import genesis
    class _Good:
        def health_check(self): return True
        def generate(self, prompt, **kw):
            return _Resp('{"name": "A new groove", "description": "d", "type": "self", "body": "yo, this mattered."}')
    _draft_env(monkeypatch, _Good())
    out = genesis._draft_single_session("rich text", "e.md")
    assert out["name"] == "A new groove" and out["model"] == "anthropic:claude-sonnet-4-6"


def test_tier_from_novelty_sim():
    import genesis
    assert genesis._tier(0.20) == "HIGH"
    assert genesis._tier(0.35) == "MEDIUM"
    assert genesis._tier(0.45) == "LOW"


def test_write_candidate_flat_schema_in_genesis_dir(tmp_path, monkeypatch):
    import genesis
    monkeypatch.setattr(genesis, "CANDIDATES_DIR", tmp_path / "genesis")
    epi = Path("/x/2026-06-03-1553.md")
    drafted = {"name": "Closing the loop", "description": "d", "type": "self",
               "body": "yo, this mattered.", "model": "anthropic:claude-sonnet-4-6"}
    out = genesis._write_candidate(drafted, epi, 0.42, "near.md")
    assert out.parent == (tmp_path / "genesis")
    assert "2026-06-03-1553" in out.name and out.name.endswith(".md")
    body = out.read_text(encoding="utf-8")
    assert "source: genesis" in body
    assert "novelty_sim: 0.420" in body
    assert "cited_epilogue: 2026-06-03-1553.md" in body
    assert "status: draft" in body and "awaiting_approval: true" in body
    assert "auto_promoted: true" not in body
    assert body.rstrip().endswith("yo, this mattered.")


def test_write_candidate_deterministic_overwrites(tmp_path, monkeypatch):
    import genesis
    monkeypatch.setattr(genesis, "CANDIDATES_DIR", tmp_path / "genesis")
    epi = Path("/x/2026-06-03-1553.md")
    drafted = {"name": "N", "description": "d", "type": "self", "body": "b", "model": "m:m"}
    a = genesis._write_candidate(drafted, epi, 0.42, "n.md")  # LOW tier
    b = genesis._write_candidate(drafted, epi, 0.20, "n.md")  # HIGH tier, same epilogue
    assert a == b  # filename keyed ONLY on epilogue stem -> overwrite across tiers/days
    assert len(list((tmp_path / "genesis").glob("*.md"))) == 1


def _epi(tmp_path, name):
    p = tmp_path / name
    p.write_text("body", encoding="utf-8")
    return p


def _stub_run(monkeypatch, genesis, epis, novelty, draft, significant=True, index=True):
    monkeypatch.setattr(genesis, "_enabled", lambda n: True)
    monkeypatch.setattr(genesis, "_index_clean", lambda: index)
    monkeypatch.setattr(genesis, "_finalized_epilogues", lambda: epis)
    monkeypatch.setattr(genesis, "_extract_significant_text", lambda t: "rich")
    monkeypatch.setattr(genesis, "_is_significant", lambda t: significant)
    monkeypatch.setattr(genesis, "_novelty", novelty)
    monkeypatch.setattr(genesis, "_draft_single_session", draft)
    monkeypatch.setattr(genesis, "_log_telemetry", lambda r: None)


def test_run_skips_index_unhealthy(monkeypatch):
    import genesis
    monkeypatch.setattr(genesis, "_enabled", lambda n: True)
    monkeypatch.setattr(genesis, "_index_clean", lambda: False)
    assert genesis.run() == {"skipped": "index_unhealthy"}


def test_run_novel_drafts_marks_and_writes(tmp_path, monkeypatch):
    import genesis
    e = _epi(tmp_path, "2026-06-02-1000.md")
    written = []
    _stub_run(monkeypatch, genesis, [e],
              novelty=lambda t: (True, 0.40, "n.md"),
              draft=lambda t, n: {"name": "N", "type": "self", "body": "b", "model": "m:m"})
    monkeypatch.setattr(genesis, "_write_candidate",
                        lambda d, p, s, c: written.append(p) or (tmp_path / "x.md"))
    out = genesis.run()
    assert out["drafted"] == 1
    assert e.with_name(e.name + ".genesis").exists()   # marked
    assert written == [e]


def test_run_transient_unassessable_does_not_mark(tmp_path, monkeypatch):
    import genesis
    e = _epi(tmp_path, "2026-06-02-1000.md")
    _stub_run(monkeypatch, genesis, [e],
              novelty=lambda t: (False, 1.0, ""),
              draft=lambda t, n: pytest.fail("must not draft when unassessable"))
    out = genesis.run()
    assert out["skipped_unassessable"] == 1 and out["drafted"] == 0
    assert not e.with_name(e.name + ".genesis").exists()  # retry next pass


def test_run_covered_marks_no_draft(tmp_path, monkeypatch):
    import genesis
    e = _epi(tmp_path, "2026-06-02-1000.md")
    _stub_run(monkeypatch, genesis, [e],
              novelty=lambda t: (True, 0.77, "n.md"),
              draft=lambda t, n: pytest.fail("must not draft when covered"))
    out = genesis.run()
    assert out["skipped_covered"] == 1 and out["drafted"] == 0
    assert e.with_name(e.name + ".genesis").exists()  # stable verdict -> marked


def test_run_trivial_marks_no_novelty_call(tmp_path, monkeypatch):
    import genesis
    e = _epi(tmp_path, "2026-06-02-1000.md")
    _stub_run(monkeypatch, genesis, [e],
              novelty=lambda t: pytest.fail("must not assess novelty when trivial"),
              draft=lambda t, n: None, significant=False)
    out = genesis.run()
    assert out["skipped_trivial"] == 1
    assert e.with_name(e.name + ".genesis").exists()


def test_run_decline_marks_no_candidate(tmp_path, monkeypatch):
    import genesis
    e = _epi(tmp_path, "2026-06-02-1000.md")
    _stub_run(monkeypatch, genesis, [e],
              novelty=lambda t: (True, 0.40, "n.md"),
              draft=lambda t, n: None)  # LLM declined
    out = genesis.run()
    assert out["declined"] == 1 and out["drafted"] == 0
    assert e.with_name(e.name + ".genesis").exists()  # declined IS a verdict


def test_run_honors_draft_cap(tmp_path, monkeypatch):
    import genesis
    epis = [_epi(tmp_path, f"2026-06-0{i}-1000.md") for i in range(1, 6)]  # 5 novel
    calls = {"n": 0}
    def _draft(t, n):
        calls["n"] += 1
        return {"name": "N", "type": "self", "body": "b", "model": "m:m"}
    _stub_run(monkeypatch, genesis, epis, novelty=lambda t: (True, 0.40, "n.md"), draft=_draft)
    monkeypatch.setattr(genesis, "_write_candidate", lambda d, p, s, c: tmp_path / "x.md")
    monkeypatch.setattr(genesis, "GENESIS_MAX_DRAFTS_PER_PASS", 3)
    out = genesis.run()
    assert calls["n"] == 3 and out["drafted"] == 3
    marked = sum(1 for e in epis if e.with_name(e.name + ".genesis").exists())
    assert marked == 3  # uncapped 2 remain UNMARKED for next pass


def _neutralize_consolidate(monkeypatch, cw):
    """Stub every other (real, mutating) consolidate pass so worker tests
    isolate the genesis wiring without touching real state."""
    import contextlib
    monkeypatch.setattr(cw, "_log", lambda rec: None)
    monkeypatch.setattr(cw, "_near_duplicate_pass", lambda: {})
    monkeypatch.setattr(cw, "_weight_rerank_pass", lambda: {})
    monkeypatch.setattr(cw, "_stale_functional_pass", lambda: {})
    monkeypatch.setattr(cw, "_predictive_prefetch_pass", lambda: {}, raising=False)
    monkeypatch.setenv("MEMORY_FLAG_SYNTHESIS_PASSES_ENABLED", "0")
    monkeypatch.setattr("promotion_candidate_generator.run_generation", lambda: {})
    monkeypatch.setattr("provenance.prune", lambda **k: {})
    monkeypatch.setattr("procedural_lib.run_designer", lambda **k: {})
    monkeypatch.setattr("observer_lib.get_connection", lambda: contextlib.nullcontext())


def test_worker_genesis_failsoft_and_marks(tmp_path, monkeypatch):
    import consolidate_worker as cw, genesis
    m = tmp_path / ".lw"
    monkeypatch.setattr(cw, "_LAST_CONSOLIDATE_MARKER", m)
    _neutralize_consolidate(monkeypatch, cw)
    def _boom():
        raise RuntimeError("genesis blew up")
    monkeypatch.setattr(genesis, "run", _boom)
    out = cw.process_consolidate({"force": True})
    assert out["genesis"]["error"].startswith("genesis blew up")
    assert m.exists()  # marker still stamped (finally)


def test_worker_genesis_flag_off_reports_skip(tmp_path, monkeypatch):
    import consolidate_worker as cw
    m = tmp_path / ".lw"
    monkeypatch.setattr(cw, "_LAST_CONSOLIDATE_MARKER", m)
    _neutralize_consolidate(monkeypatch, cw)
    monkeypatch.setenv("MEMORY_FLAG_GENESIS_ENABLED", "0")  # force OFF (hermetic; live may be ON)
    out = cw.process_consolidate({"force": True})
    assert out["genesis"] == {"skipped": "flag_off"}


def test_run_marks_unreadable_epilogue(tmp_path, monkeypatch):
    import genesis
    e = tmp_path / "2026-06-02-1000.md"
    e.mkdir()  # a directory -> read_text raises -> a STABLE failure, must be marked
    monkeypatch.setattr(genesis, "_enabled", lambda n: True)
    monkeypatch.setattr(genesis, "_index_clean", lambda: True)
    monkeypatch.setattr(genesis, "_finalized_epilogues", lambda: [e])
    monkeypatch.setattr(genesis, "_log_telemetry", lambda r: None)
    out = genesis.run()
    assert out["skipped_unreadable"] == 1
    assert e.with_name(e.name + ".genesis").exists()  # marked -> not rescanned forever


def test_run_write_failure_does_not_abort_rest_of_pass(tmp_path, monkeypatch):
    import genesis
    epis = [_epi(tmp_path, "2026-06-01-1000.md"), _epi(tmp_path, "2026-06-02-1000.md")]
    _stub_run(monkeypatch, genesis, epis,
              novelty=lambda t: (True, 0.40, "n.md"),
              draft=lambda t, n: {"name": "N", "type": "self", "body": "b", "model": "m:m"})
    calls = {"n": 0}
    def _boom_write(d, p, s, c):
        calls["n"] += 1
        raise OSError("disk full")
    monkeypatch.setattr(genesis, "_write_candidate", _boom_write)
    out = genesis.run()  # must NOT raise; both epilogues attempted despite write failures
    assert calls["n"] == 2 and out["drafted"] == 0


def test_end_to_end_one_novel_session(tmp_path, monkeypatch):
    import genesis
    (tmp_path / "epi").mkdir()
    monkeypatch.setattr(genesis, "EPILOGUE_DIR", tmp_path / "epi")
    monkeypatch.setattr(genesis, "CANDIDATES_DIR", tmp_path / "cand")
    monkeypatch.setattr(genesis, "TELEMETRY_PATH", tmp_path / "t.jsonl")
    monkeypatch.setattr(genesis, "_enabled", lambda n: True)
    monkeypatch.setattr(genesis, "_index_clean", lambda: True)
    epi = (tmp_path / "epi" / "2026-06-03-1553.md")
    epi.write_text(EPI_SAMPLE, encoding="utf-8")
    # real extraction/significance/marker/writer; stub only embed+LLM boundaries
    monkeypatch.setattr(genesis, "_novelty", lambda t: (True, 0.33, "near.md"))
    monkeypatch.setattr(genesis, "_draft_single_session",
                        lambda t, n: {"name": "Loop alive", "description": "d",
                                      "type": "self", "body": "yo, it clicked.",
                                      "model": "anthropic:claude-sonnet-4-6"})
    out1 = genesis.run()
    assert out1["drafted"] == 1
    cands = list((tmp_path / "cand").glob("*.md"))
    assert len(cands) == 1 and "2026-06-03-1553" in cands[0].name
    txt = cands[0].read_text(encoding="utf-8")
    assert "source: genesis" in txt and "cited_epilogue: 2026-06-03-1553.md" in txt
    assert epi.with_name(epi.name + ".genesis").exists()
    # second pass: already marked -> nothing new
    out2 = genesis.run()
    assert out2["drafted"] == 0
