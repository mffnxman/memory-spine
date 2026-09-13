"""
promotion_candidate_generator.py - v2.4 of plan_homemade_observer.

Scans observations.db for rows that have been referenced via prefetch
fallback enough times to be candidates for crystallization into the v3.2
spine. Writes a draft markdown file per cluster into:

    _meta/promotion_candidates/observer/<ts>-<slug>.md

The user (or claude in review mode) reviews these files manually and either:
  - promotes (move to memory/, update observations.promoted_to_memory_id)
  - rejects (mark observations rejected)

Clustering (v1, simple):
  - one candidate per session_id with >= 2 observations having
    referenced_count >= REF_THRESHOLD
  - topic from TF-IDF over the eligible observations
  - title from top topic

Usage:
    python promotion_candidate_generator.py --generate    # produce candidates
    python promotion_candidate_generator.py --list        # list pending
    python promotion_candidate_generator.py --status      # summary
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import paths as _paths  # noqa: E402

try:
    from observer_lib import (
        get_connection,
        ensure_db,
        log_error,
    )
except Exception:
    sys.exit(0)

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

META_DIR = _paths.META_DIR
CANDIDATES_DIR = META_DIR / "promotion_candidates" / "observer"
FLAGS_PATH = META_DIR / "feature_flags.json"

REF_THRESHOLD = 1  # min referenced_count for an obs to be eligible (low for early use)
MIN_ELIGIBLE_PER_SESSION = 2  # need this many eligible obs to make a candidate

# v2.1 auto-promote safety gates - tighter than --generate
AUTO_CLUSTER_SCORE_MIN = 0.8
AUTO_REF_MIN = 3
AUTO_AGE_MIN_HOURS = 24

# v2.5 auto-accept graduation gates (2026-07-09 — the user closed the trust
# window explicitly: "make it so it auto promotes"). Candidates that clear
# these graduate straight into memory/ in spine format.
AUTO_ACCEPT_SCORE_MIN = 0.5
AUTO_ACCEPT_AGE_MIN_HOURS = 24

AUTO_DIR = META_DIR / "promotion_candidates" / "observer" / "auto"
ACCEPTED_DIR = META_DIR / "promotion_candidates" / "observer" / "accepted"
REJECTED_DIR = META_DIR / "promotion_candidates" / "observer" / "rejected"
MEMORY_DIR = META_DIR.parent  # the memory/ root


def _flag(name, default):
    env_key = "OBSERVER_FLAG_" + name.upper()
    if env_key in os.environ:
        v = os.environ[env_key].lower()
        if isinstance(default, bool):
            return v in ("1", "true", "yes", "on")
        try:
            return type(default)(os.environ[env_key])
        except Exception:
            return default
    try:
        if FLAGS_PATH.exists():
            with open(FLAGS_PATH, encoding="utf-8") as f:
                return json.load(f).get(name, default)
    except Exception:
        pass
    return default


STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "if",
        "for",
        "to",
        "of",
        "in",
        "on",
        "at",
        "by",
        "with",
        "from",
        "is",
        "are",
        "was",
        "were",
        "be",
        "this",
        "that",
        "these",
        "those",
        "what",
        "you",
        "your",
        "us",
        "py",
        "js",
        "md",
        "json",
        "log",
        "tmp",
        "cd",
        "ls",
        "cat",
        "rm",
        "mv",
        "cp",
        "mkdir",
        "echo",
        "import",
        "from",
        "def",
        "class",
        "return",
        "true",
        "false",
        "none",
        "users",
        "claude",
        "c",
        "home",
        "tmp",
        "var",
        "projects",
        "scripts",
        "_meta",
        "memory",
        "path",
        "sys",
        "get_connection",
        "ensure_db",
    }
)


def _tok(s):
    if not s:
        return []
    return [
        t
        for t in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", s.lower())
        if t not in STOPWORDS
    ]


def _slug(text, max_len=50):
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", text.lower()).strip("-")
    return s[:max_len] or "candidate"


def _topic_from_obs(obs_rows):
    """Extract top topic from a list of observation rows."""
    bag = Counter()
    for r in obs_rows:
        bag.update(_tok(r["cmd_excerpt"]))
        if r["file_paths"]:
            try:
                for p in json.loads(r["file_paths"]):
                    if isinstance(p, str):
                        bn = p.replace("\\", "/").rsplit("/", 1)[-1]
                        bag.update(_tok(bn))
            except Exception:
                pass
    top = bag.most_common(5)
    return top


def find_candidates(
    conn, ref_threshold=REF_THRESHOLD, min_per_session=MIN_ELIGIBLE_PER_SESSION
):
    """Group eligible observations into per-session candidate clusters.

    Eligibility: referenced_count >= ref_threshold AND promoted_to_memory_id IS NULL.
    Returns list of (session_id, [obs_row, ...]).
    """
    eligible = conn.execute(
        """SELECT id, session_id, ts, tool_name, cmd_excerpt,
                  tool_input_excerpt, tool_output_excerpt, file_paths,
                  referenced_count, last_referenced_at
           FROM observations
           WHERE referenced_count >= ?
             AND promoted_to_memory_id IS NULL
           ORDER BY session_id, ts""",
        (ref_threshold,),
    ).fetchall()
    by_session = {}
    for r in eligible:
        by_session.setdefault(r["session_id"], []).append(r)
    return [
        (sid, rows) for sid, rows in by_session.items() if len(rows) >= min_per_session
    ]


def _pending_sessions() -> set:
    """Session ids that already have a pending candidate file (idempotency)."""
    out = set()
    if CANDIDATES_DIR.exists():
        for f in CANDIDATES_DIR.glob("*.md"):
            try:
                m = re.search(r"source_session:\s*(\S+)", f.read_text(encoding="utf-8"))
                if m:
                    out.add(m.group(1))
            except Exception:
                pass
    return out


def run_generation(
    ref_threshold=REF_THRESHOLD, min_per_session=MIN_ELIGIBLE_PER_SESSION
) -> dict:
    """Programmatic candidate generation for triggers (consolidate / CLI).

    Idempotent: skips sessions that already have a pending candidate file, so it
    can run every cycle without flooding. Honors the
    observer_candidate_generation_enabled flag. NEVER promotes — only writes
    review candidates into staging. Returns {clusters, written:[names]}.
    """
    ensure_db()
    if not _flag("observer_candidate_generation_enabled", True):
        return {"skipped": True, "clusters": 0, "written": []}
    with get_connection() as conn:
        clusters = find_candidates(conn, ref_threshold, min_per_session)
    pending = _pending_sessions()
    written = []
    for sid, rows in clusters:
        if sid in pending:
            continue
        try:
            written.append(write_candidate(sid, rows).name)
        except Exception:
            pass
    return {"clusters": len(clusters), "written": written}


def eligible_summary() -> dict:
    """Counts for boot surfacing: total obs, eligible-unpromoted, promoted, pending files."""
    ensure_db()
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        eligible = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE referenced_count >= ? AND promoted_to_memory_id IS NULL",
            (REF_THRESHOLD,),
        ).fetchone()[0]
        promoted = conn.execute(
            "SELECT COUNT(*) FROM observations WHERE promoted_to_memory_id IS NOT NULL"
        ).fetchone()[0]
    pending_files = (
        len(list(CANDIDATES_DIR.glob("*.md"))) if CANDIDATES_DIR.exists() else 0
    )
    return {
        "total": total,
        "eligible": eligible,
        "promoted": promoted,
        "pending_files": pending_files,
    }


def write_candidate(session_id, obs_rows):
    """Write one candidate file for a cluster. Returns the written Path."""
    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)

    # Junk tokens (path fragments, shell verbs) never make the topic line:
    # junk-led identical topics are how 29/120 spine memories became mutually
    # indistinguishable boilerplate (BIGBUFF 2.0 D1-04 root cause).
    topics = [
        (t, c)
        for (t, c) in _topic_from_obs(obs_rows)
        if t.lower() not in JUNK_TOPIC_TOKENS
    ]
    top_topic = topics[0][0] if topics else "untitled"
    topic_str = ", ".join(t for t, _ in topics) or "(no clear topic)"

    # collect files
    file_counts = Counter()
    for r in obs_rows:
        if not r["file_paths"]:
            continue
        try:
            for p in json.loads(r["file_paths"]):
                if isinstance(p, str):
                    file_counts[p] += 1
        except Exception:
            pass

    # All-junk clusters fall back to the dominant FILE, not "untitled" — a
    # filename is at least unique and retrievable by its own literal.
    if not topics and file_counts:
        top_topic = (
            str(file_counts.most_common(1)[0][0]).replace("\\", "/").rsplit("/", 1)[-1]
        )
        topic_str = top_topic

    started = min(r["ts"] for r in obs_rows)
    ended = max(r["ts"] for r in obs_rows)
    total_refs = sum(r["referenced_count"] for r in obs_rows)
    obs_ids = [r["id"] for r in obs_rows]

    # cluster score - simple heuristic
    cluster_score = round(min(1.0, total_refs / 10.0 + len(obs_rows) * 0.05), 2)

    ts_str = datetime.fromtimestamp(int(time.time())).strftime("%Y%m%d-%H%M%S")
    # Include a session fingerprint: two sessions can slug to the same topic in
    # the same second, and `{ts}-{slug}.md` then collides — one session's
    # candidate silently overwrites another's (lost candidate + breaks the
    # idempotency dedup). The sid suffix guarantees one file per session.
    fname = "{}-{}-{}.md".format(ts_str, _slug(top_topic), _slug(str(session_id))[:8])
    fpath = CANDIDATES_DIR / fname

    # suggest a memory filename based on dominant tool + topic
    suggested = "observer_promoted_{}.md".format(_slug(top_topic))

    body_lines = [
        "---",
        "name: auto-cluster: {}".format(top_topic),
        "metadata:",
        "  node_type: promotion_candidate",
        "  origin: observer",
        "  source_session: {}".format(session_id),
        "  source_obs_ids: {}".format(json.dumps(obs_ids)),
        "  cluster_score: {}".format(cluster_score),
        "  generated_at: {}".format(int(time.time())),
        "  status: pending",
        "---",
        "",
        "# Observer: {} ({} -> {})".format(
            top_topic,
            datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M"),
            datetime.fromtimestamp(ended).strftime("%H:%M"),
        ),
        "",
        "## Topic",
        "{}".format(topic_str),
        "",
        "## Stats",
        "- session: `{}`".format(session_id),
        "- observations in cluster: {}".format(len(obs_rows)),
        "- total references via prefetch fallback: {}".format(total_refs),
        "- time range: {} -> {}".format(
            datetime.fromtimestamp(started).strftime("%Y-%m-%d %H:%M"),
            datetime.fromtimestamp(ended).strftime("%Y-%m-%d %H:%M"),
        ),
        "- cluster_score: {}".format(cluster_score),
        "",
    ]

    if file_counts:
        body_lines.append("## Files involved")
        for path, cnt in file_counts.most_common(10):
            bn = str(path).replace("\\", "/").rsplit("/", 1)[-1]
            body_lines.append("- `{}` (x{})".format(bn, cnt))
        body_lines.append("")

    body_lines.append("## Sample activity")
    for r in obs_rows[:5]:
        excerpt = (r["cmd_excerpt"] or r["tool_input_excerpt"] or "")[:160]
        excerpt = excerpt.replace("\n", " ").replace("`", "'")
        body_lines.append("- **{}**: `{}`".format(r["tool_name"], excerpt))
    body_lines.append("")

    body_lines.extend(
        [
            "## Recommended memory file",
            "`{}`".format(suggested),
            "",
            "## Decision",
            "- [ ] promote (move to `memory/`, populate frontmatter, update `observations.promoted_to_memory_id`)",
            "- [ ] reject (set observations.promoted_to_memory_id = 'rejected', won't recandidate)",
            "",
        ]
    )

    fpath.write_text("\n".join(body_lines), encoding="utf-8")
    return fpath


def cmd_generate():
    res = run_generation()
    if res.get("skipped"):
        print("observer_candidate_generation_enabled is false; skipping.")
        return
    if not res["written"]:
        print(
            "no new clusters (ref >= {}, min {}/session; existing pending skipped).".format(
                REF_THRESHOLD, MIN_ELIGIBLE_PER_SESSION
            )
        )
        return
    print("wrote {} candidate(s):".format(len(res["written"])))
    for name in res["written"]:
        print("  wrote: {}".format(name))
    print("review with `python promotion_candidate_generator.py --list`")


def cmd_list():
    if not CANDIDATES_DIR.exists():
        print("no candidates dir yet.")
        return
    files = sorted(CANDIDATES_DIR.glob("*.md"))
    if not files:
        print("no candidates pending.")
        return
    print("=== pending candidates ({}) ===".format(len(files)))
    for f in files:
        head = f.read_text(encoding="utf-8").splitlines()[:30]
        # find topic line
        topic = ""
        for ln in head:
            if ln.startswith("name: auto-cluster: "):
                topic = ln.replace("name: auto-cluster: ", "")
                break
        print("  - {}  ({})".format(f.name, topic))


def cmd_status():
    s = eligible_summary()
    print("observations:        {}".format(s["total"]))
    print("eligible (ref >= {}): {}".format(REF_THRESHOLD, s["eligible"]))
    print("already promoted:    {}".format(s["promoted"]))
    print("candidate files:     {}".format(s["pending_files"]))


def cmd_auto_suggest():
    """v2.1 — tighter gates, writes to auto/ subdir, never touches memory/."""
    ensure_db()
    if not _flag("auto_promote_high_confidence_enabled", False):
        print("auto_promote_high_confidence_enabled is OFF. (safety gate)")
        return
    with get_connection() as conn:
        clusters = find_candidates(
            conn, ref_threshold=AUTO_REF_MIN, min_per_session=MIN_ELIGIBLE_PER_SESSION
        )
        promoted_count = 0
        skipped_count = 0
        for sid, rows in clusters:
            # gate 1: cluster score
            total_refs = sum(r["referenced_count"] for r in rows)
            cluster_score = round(min(1.0, total_refs / 10.0 + len(rows) * 0.05), 2)
            if cluster_score < AUTO_CLUSTER_SCORE_MIN:
                skipped_count += 1
                continue
            # gate 2: age - newest observation must be at least AUTO_AGE_MIN_HOURS old
            newest_ts = max(r["ts"] for r in rows)
            age_h = (time.time() - newest_ts) / 3600.0
            if age_h < AUTO_AGE_MIN_HOURS:
                skipped_count += 1
                continue
            # write to auto/ subdir instead of base dir
            AUTO_DIR.mkdir(parents=True, exist_ok=True)
            # reuse write_candidate but redirect dir
            global CANDIDATES_DIR
            original = CANDIDATES_DIR
            CANDIDATES_DIR = AUTO_DIR
            try:
                fpath = write_candidate(sid, rows)
                # tag as auto-suggested in frontmatter
                content = fpath.read_text(encoding="utf-8")
                content = content.replace("status: pending", "status: auto-suggested")
                fpath.write_text(content, encoding="utf-8")
                print("  auto-suggested: {}".format(fpath.name))
                promoted_count += 1
            finally:
                CANDIDATES_DIR = original
        print("--- summary ---")
        print("clusters considered: {}".format(len(clusters)))
        print("auto-suggested:      {}".format(promoted_count))
        print("skipped (gates):     {}".format(skipped_count))
        if promoted_count > 0:
            print("review with: python promotion_candidate_generator.py --review-auto")


def _emit_reindex(path):
    """Enqueue a reindex job for a freshly promoted memory (fail-soft).

    Same lane the Edit/Write hook uses (memory_write_postprocess -> event_bus
    -> outbox_worker -> reindex_embeddings), so promoted memories become
    retrievable on the next drain without blocking this pass."""
    try:
        from event_bus import emit_event

        emit_event("reindex", {"file_path": str(path)})
    except Exception:
        pass


def parse_candidate(path):
    """Parse an observer candidate file into a metadata dict.

    Returns None if the file isn't observer-format (e.g. an epilogue-tier
    draft from auto_promote.py sharing the candidates dir)."""
    content = path.read_text(encoding="utf-8")
    m_ids = re.search(r"source_obs_ids:\s*(\[.*?\])", content)
    m_rec = re.search(r"## Recommended memory file\s*\n`(.+?)`", content)
    if not m_ids or not m_rec:
        return None
    try:
        obs_ids = json.loads(m_ids.group(1))
    except Exception:
        return None
    m_score = re.search(r"cluster_score:\s*([0-9.]+)", content)
    m_gen = re.search(r"generated_at:\s*(\d+)", content)
    m_sid = re.search(r"source_session:\s*(\S+)", content)
    m_topic = re.search(r"## Topic\s*\n(.+)", content)
    m_file = re.search(r"## Files involved\s*\n- `(.+?)`", content)
    return {
        "obs_ids": obs_ids,
        "recommended": m_rec.group(1).strip(),
        "cluster_score": float(m_score.group(1)) if m_score else 0.0,
        "generated_at": int(m_gen.group(1)) if m_gen else 0,
        "source_session": m_sid.group(1) if m_sid else "?",
        "topic": (m_topic.group(1).strip() if m_topic else "(no clear topic)"),
        "top_file": m_file.group(1).strip() if m_file else "",
        "content": content,
    }


# TF-IDF topic extraction picks up path fragments and generic shell tokens
# ('c--users-alex', 'grep', 'env') — a memory named after those is retrieval
# noise. Graduation names fall back to the cluster's dominant FILE instead.
JUNK_TOPIC_TOKENS = frozenset(
    {
        _paths.PROJECT_SLUG.lower(),
        *(part.lower() for part in _paths.PROJECT_SLUG.split("-") if part),
        "users",
        "appdata",
        "roaming",
        "downloads",
        "desktop",
        "documents",
        "untitled",
        "output",
        "env",
        "grep",
        "html",
        "png",
        "jpg",
        "jpeg",
        "json",
        "yaml",
        "settings",
        "tools",
        "local",
        "temp",
        "write-output",
        "get-childitem",
        "select-object",
        "foreach-object",
        "python",
        "screenshot",
        "searcher",
        "claude",
        "candidate",
        "scripts",
    }
)


def _display_topic(meta):
    """Best human-facing topic for a candidate: first non-junk TF-IDF term,
    else the dominant file, else the session fingerprint."""
    for t in (x.strip() for x in meta.get("topic", "").split(",")):
        if t and t.lower() not in JUNK_TOPIC_TOKENS:
            return t
    if meta.get("top_file"):
        return meta["top_file"]
    return "session-" + str(meta.get("source_session", "?"))[:8]


def graduation_target(meta):
    """Memory filename (pre-uniquify) a candidate graduates to."""
    return "observer_{}.md".format(_slug(_display_topic(meta), 40))


def _uniquify(target):
    """Return a non-colliding path by suffixing -2, -3, ... before .md."""
    if not target.exists():
        return target
    stem, suffix = target.stem, target.suffix
    for i in range(2, 100):
        cand = target.with_name("{}-{}{}".format(stem, i, suffix))
        if not cand.exists():
            return cand
    raise RuntimeError("could not uniquify {}".format(target))


def _graduate(candidate_path, meta, conn):
    """Promote one parsed candidate into memory/ in SPINE format.

    Flat frontmatter (name/description/type/weight) — not the nested metadata
    block — so the index, prefetch, and /recall treat it like any curated
    memory. Updates observations bookkeeping, archives the candidate to
    accepted/, emits a reindex event. Returns the memory filename."""
    target = _uniquify(MEMORY_DIR / graduation_target(meta))

    body = re.sub(r"## Decision.*$", "", meta["content"], flags=re.DOTALL)
    parts = body.split("---", 2)
    body = parts[-1].strip() if len(parts) >= 3 else body.strip()

    weight = "medium" if meta["cluster_score"] >= 0.8 else "low"
    gen_date = (
        datetime.fromtimestamp(meta["generated_at"]).strftime("%Y-%m-%d")
        if meta["generated_at"]
        else "?"
    )
    # Distinguishing frontmatter (BIGBUFF 2.0 D1-04): topics junk-filtered,
    # dominant file named, no shared "auto-promoted observer cluster" prefix —
    # the old template made every promotion a near-dup of every other one.
    topics_clean = ", ".join(
        t
        for t in (x.strip() for x in meta.get("topic", "").split(","))
        if t and t.lower() not in JUNK_TOPIC_TOKENS
    ) or _display_topic(meta)
    top_file = meta.get("top_file")
    frontmatter = "\n".join(
        [
            "---",
            "name: observer: {} ({})".format(_display_topic(meta), gen_date),
            "description: Observer trace ({}): {}{}; {} observations from session {}.".format(
                gen_date,
                topics_clean,
                "; main file {}".format(top_file) if top_file else "",
                len(meta["obs_ids"]),
                str(meta["source_session"])[:8],
            ),
            "type: project",
            "weight: {}".format(weight),
            "origin: observer-promoted",
            "source_candidate: {}".format(candidate_path.name),
            "source_session: {}".format(meta["source_session"]),
            "source_obs_ids: {}".format(json.dumps(meta["obs_ids"])),
            "cluster_score: {}".format(meta["cluster_score"]),
            "promoted_at: {}".format(datetime.now().strftime("%Y-%m-%d %H:%M")),
            "platform_source: claude_code",
            "---",
        ]
    )
    target.write_text(frontmatter + "\n\n" + body + "\n", encoding="utf-8")

    for oid in meta["obs_ids"]:
        conn.execute(
            "UPDATE observations SET promoted_to_memory_id = ? WHERE id = ?",
            (target.name, oid),
        )
    conn.commit()

    ACCEPTED_DIR.mkdir(parents=True, exist_ok=True)
    candidate_path.rename(ACCEPTED_DIR / candidate_path.name)
    _emit_reindex(target)
    return target.name


def _skip(summary, reason):
    summary["skipped"][reason] = summary["skipped"].get(reason, 0) + 1


def auto_accept(
    score_min=AUTO_ACCEPT_SCORE_MIN, age_min_hours=AUTO_ACCEPT_AGE_MIN_HOURS, now=None
):
    """Graduate every pending observer candidate that clears the gates.

    Gates: auto_promote_high_confidence_enabled flag ON, cluster_score >=
    score_min, candidate age >= age_min_hours, and at least one source
    observation still unresolved. Skipped candidates stay pending (they get
    another shot next cycle). Returns a summary dict."""
    ensure_db()
    if not _flag("auto_promote_high_confidence_enabled", False):
        return {
            "skipped_flag_off": True,
            "promoted": [],
            "skipped": {},
            "considered": 0,
        }
    now = now or time.time()
    summary = {"considered": 0, "promoted": [], "skipped": {}}
    if not CANDIDATES_DIR.exists():
        return summary
    with get_connection() as conn:
        for path in sorted(CANDIDATES_DIR.glob("*.md")):
            summary["considered"] += 1
            try:
                meta = parse_candidate(path)
            except Exception:
                _skip(summary, "unparseable")
                continue
            if meta is None or not meta.get("obs_ids"):
                _skip(summary, "not_observer_format")
                continue
            if meta["cluster_score"] < score_min:
                _skip(summary, "score")
                continue
            if (now - meta["generated_at"]) / 3600.0 < age_min_hours:
                _skip(summary, "age")
                continue
            placeholders = ",".join("?" * len(meta["obs_ids"]))
            unresolved = conn.execute(
                "SELECT COUNT(*) FROM observations WHERE id IN ({}) "
                "AND promoted_to_memory_id IS NULL".format(placeholders),
                meta["obs_ids"],
            ).fetchone()[0]
            if unresolved == 0:
                _skip(summary, "obs_resolved")
                continue
            try:
                summary["promoted"].append(_graduate(path, meta, conn))
            except Exception as e:
                _skip(summary, "error")
                try:
                    log_error("auto_accept graduate failed: " + str(e))
                except Exception:
                    pass
    return summary


def cmd_auto_accept():
    res = auto_accept()
    if res.get("skipped_flag_off"):
        print("auto_promote_high_confidence_enabled is OFF. (safety gate)")
        return
    print("considered: {}".format(res["considered"]))
    print("promoted:   {}".format(len(res["promoted"])))
    for name in res["promoted"]:
        print("  -> {}".format(name))
    if res["skipped"]:
        print("skipped:    {}".format(res["skipped"]))


def cmd_accept(arg):
    """Move a candidate file to memory/ and update observations bookkeeping.

    arg is the candidate filename (basename, no path).
    """
    ensure_db()
    # Find the candidate file in any of the candidate dirs
    candidate_path = None
    for d in (CANDIDATES_DIR, AUTO_DIR):
        p = d / arg
        if p.exists():
            candidate_path = p
            break
    if not candidate_path:
        print("candidate not found: {}".format(arg))
        return

    content = candidate_path.read_text(encoding="utf-8")
    # extract source_obs_ids and recommended memory file
    m_ids = re.search(r"source_obs_ids:\s*(\[.*?\])", content)
    m_recommended = re.search(r"## Recommended memory file\s*\n`(.+?)`", content)
    if not m_ids or not m_recommended:
        print("could not parse candidate frontmatter; aborting accept.")
        return
    try:
        obs_ids = json.loads(m_ids.group(1))
    except Exception:
        print("could not parse source_obs_ids; aborting.")
        return
    recommended = m_recommended.group(1).strip()
    target = MEMORY_DIR / recommended

    if target.exists():
        print("target memory file already exists: {}".format(target))
        print("rename the recommended filename in the candidate and retry.")
        return

    # Build the memory file from the candidate (minimal frontmatter + body)
    memory_body = (
        "---\n"
        "name: {}\n"
        "metadata:\n"
        "  node_type: memory\n"
        "  type: project\n"
        "  origin: observer-promoted\n"
        "  source_candidate: {}\n"
        "  source_obs_ids: {}\n"
        "  created: {}\n"
        "---\n\n"
        "# Auto-promoted from observer cluster\n\n"
        "_Promoted on {} from candidate `{}`._\n\n"
        "{}\n"
    ).format(
        recommended.replace(".md", "").replace("observer_promoted_", "Promoted: "),
        candidate_path.name,
        json.dumps(obs_ids),
        datetime.now().strftime("%Y-%m-%d"),
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        candidate_path.name,
        # include the candidate body sans the decision section
        re.sub(r"## Decision.*$", "", content, flags=re.DOTALL)
        .split("---", 2)[-1]
        .strip(),
    )

    target.write_text(memory_body, encoding="utf-8")

    # Update observations bookkeeping
    with get_connection() as conn:
        for oid in obs_ids:
            conn.execute(
                "UPDATE observations SET promoted_to_memory_id = ? WHERE id = ?",
                (recommended, oid),
            )
        conn.commit()

    # Move candidate to accepted/
    ACCEPTED_DIR.mkdir(parents=True, exist_ok=True)
    new_path = ACCEPTED_DIR / candidate_path.name
    candidate_path.rename(new_path)

    print("PROMOTED: {} -> {}".format(candidate_path.name, target))
    print("updated {} observations with promoted_to_memory_id".format(len(obs_ids)))


def cmd_reject(arg):
    """Mark observations rejected, move candidate to rejected/ dir."""
    ensure_db()
    candidate_path = None
    for d in (CANDIDATES_DIR, AUTO_DIR):
        p = d / arg
        if p.exists():
            candidate_path = p
            break
    if not candidate_path:
        print("candidate not found: {}".format(arg))
        return

    content = candidate_path.read_text(encoding="utf-8")
    m_ids = re.search(r"source_obs_ids:\s*(\[.*?\])", content)
    if not m_ids:
        print("could not parse source_obs_ids; aborting.")
        return
    obs_ids = json.loads(m_ids.group(1))

    with get_connection() as conn:
        for oid in obs_ids:
            conn.execute(
                "UPDATE observations SET promoted_to_memory_id = 'rejected' WHERE id = ?",
                (oid,),
            )
        conn.commit()

    REJECTED_DIR.mkdir(parents=True, exist_ok=True)
    new_path = REJECTED_DIR / candidate_path.name
    candidate_path.rename(new_path)
    print(
        "REJECTED: {} ({} observations marked rejected)".format(
            candidate_path.name, len(obs_ids)
        )
    )


def cmd_review_auto():
    """List auto/ candidates."""
    if not AUTO_DIR.exists():
        print("no auto candidates yet.")
        return
    files = sorted(AUTO_DIR.glob("*.md"))
    if not files:
        print("no auto candidates pending review.")
        return
    print("=== auto-suggested ({}) ===".format(len(files)))
    for f in files:
        head = f.read_text(encoding="utf-8").splitlines()[:30]
        topic = ""
        for ln in head:
            if ln.startswith("name: auto-cluster: "):
                topic = ln.replace("name: auto-cluster: ", "")
                break
        print("  - {}  ({})".format(f.name, topic))
    print("\naccept: python promotion_candidate_generator.py --accept <filename>")
    print("reject: python promotion_candidate_generator.py --reject <filename>")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    try:
        if cmd == "--generate":
            cmd_generate()
        elif cmd == "--list":
            cmd_list()
        elif cmd == "--status":
            cmd_status()
        elif cmd == "--auto-suggest":
            cmd_auto_suggest()
        elif cmd == "--auto-accept":
            cmd_auto_accept()
        elif cmd == "--review-auto":
            cmd_review_auto()
        elif cmd == "--accept":
            if not arg:
                print("usage: --accept <candidate_filename>")
                sys.exit(1)
            cmd_accept(arg)
        elif cmd == "--reject":
            if not arg:
                print("usage: --reject <candidate_filename>")
                sys.exit(1)
            cmd_reject(arg)
        else:
            print("unknown command: {}".format(cmd))
            print(__doc__)
            sys.exit(1)
    except Exception as e:
        try:
            log_error("promotion_candidate: " + str(e))
        except Exception:
            pass
        print("error: {}".format(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
