"""
session_summarize.py - Stop hook for the homemade observer.

Phase 3 of plan_homemade_observer_v1. Reads stdin payload (session_id),
queries observations, writes a heuristic summary into session_summaries.

Heuristic-only in v1:
  - top 5 tools by call count
  - top 10 file paths touched (deduplicated)
  - top topics (keyword frequency from cmd_excerpts + file basenames)
  - short narrative paragraph
  - duration + obs total

LLM-based summary is a future upgrade (gated on OBSERVER_LLM_SUMMARY_ENABLED).

Hook contract:
  - Stop hook with no matcher
  - stdin = JSON payload (session_id)
  - exit 0 always (fail-open)

Standalone use (for testing):
  python session_summarize.py --session-id <uuid>
  python session_summarize.py --session-id LATEST
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import paths as _paths  # noqa: E402

try:
    from observer_lib import (
        ensure_db, get_connection, resolve_session_id, log_error,
    )
except Exception:
    sys.exit(0)


# ---- config ----
MIN_OBS_FOR_SUMMARY = 3      # don't summarize tiny sessions
TOP_TOOLS = 5
TOP_FILES = 10
TOP_TOPICS = 10

# common words to drop from topic extraction
STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "but", "if", "for", "to", "of", "in", "on",
    "at", "by", "with", "from", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "this", "that", "these", "those",
    "i", "you", "he", "she", "it", "we", "they", "my", "your", "his", "her",
    "its", "our", "their", "me", "him", "us", "them",
    "py", "js", "ts", "md", "json", "txt", "log", "tmp",
    "cd", "ls", "cat", "echo", "rm", "mv", "cp", "mkdir",
    "import", "from", "def", "class", "return", "if", "else", "for",
    "true", "false", "null", "none",
    "users", "claude", "c", "home", "tmp", "var",
})


def _read_stdin():
    try:
        raw = sys.stdin.read()
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return {}


def _basename(path):
    s = str(path).replace("\\", "/")
    return s.rsplit("/", 1)[-1]


def _tokenize_for_topics(text):
    """Split into lowercase alnum tokens, drop stopwords, drop length<3."""
    if not text:
        return []
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text.lower())
    return [t for t in tokens if t not in STOPWORDS]


def _session_corpus_tokens(conn, session_id):
    """Return list of topic tokens for one session (from cmd_excerpts + file basenames)."""
    rows = conn.execute(
        "SELECT cmd_excerpt, file_paths FROM observations WHERE session_id = ?",
        (session_id,),
    ).fetchall()
    chunks = []
    for r in rows:
        if r["cmd_excerpt"]:
            chunks.append(r["cmd_excerpt"])
        if r["file_paths"]:
            try:
                for p in json.loads(r["file_paths"]):
                    if isinstance(p, str):
                        chunks.append(_basename(p))
            except Exception:
                pass
    return _tokenize_for_topics(" ".join(chunks))


def _extract_topics_tfidf(conn, session_id, top_n=TOP_TOPICS):
    """Compute TF-IDF for tokens in this session vs the rest of observations.db.

    For a small corpus we just count documents (sessions) containing each
    term. Logarithmic IDF, normalized to top-N. Falls back to keyword
    frequency if there's only one session in the corpus.

    Returns list[str] of top topics.
    """
    import math
    session_tokens = _session_corpus_tokens(conn, session_id)
    if not session_tokens:
        return []
    session_tf = Counter(session_tokens)

    # all session ids with at least one observation
    sessions = [r["session_id"] for r in conn.execute(
        "SELECT DISTINCT session_id FROM observations"
    ).fetchall()]
    if len(sessions) < 2:
        # not enough corpus to compute IDF — fall back to plain frequency
        return [t for t, _ in session_tf.most_common(top_n)]

    # document frequency: for each term, how many sessions contain it?
    other_sessions = [s for s in sessions if s != session_id]
    term_df = Counter()
    for s in other_sessions:
        toks = set(_session_corpus_tokens(conn, s))
        for t in toks:
            term_df[t] += 1

    N = len(sessions)
    scored = []
    for term, tf in session_tf.items():
        df = term_df.get(term, 0) + 1  # +1 for this session
        idf = math.log(N / df) if df > 0 else 0
        tfidf = tf * (idf + 1)  # +1 smoothing so terms unique to this session score high
        scored.append((term, tfidf))
    scored.sort(key=lambda x: -x[1])
    return [t for t, _ in scored[:top_n]]


def _topics_for_session(conn, session_id, obs_rows):
    """Choose topic extractor based on flag. Default: TF-IDF (better)."""
    flag = _topic_flag()
    if flag:
        try:
            return _extract_topics_tfidf(conn, session_id)
        except Exception:
            pass
    # fallback: legacy keyword frequency
    topic_corpus = []
    for r in obs_rows:
        if r["cmd_excerpt"]:
            topic_corpus.append(r["cmd_excerpt"])
    # files come from caller (already deduped top list)
    return [t for t, _ in Counter(_tokenize_for_topics(" ".join(topic_corpus))).most_common(TOP_TOPICS)]


def _topic_flag():
    """Read observer_topic_tfidf_enabled. Default True (TF-IDF is strictly better)."""
    flag_path = _paths.META_DIR / "feature_flags.json"
    env_key = "OBSERVER_FLAG_OBSERVER_TOPIC_TFIDF_ENABLED"
    if env_key in os.environ:
        return os.environ[env_key].lower() in ("1", "true", "yes", "on")
    try:
        if flag_path.exists():
            with open(flag_path, encoding="utf-8") as f:
                return json.load(f).get("observer_topic_tfidf_enabled", True)
    except Exception:
        pass
    return True


def summarize_session(session_id):
    """Build a summary dict from observations.

    Returns dict with: summary_text, key_files, key_topics, observation_count,
    started_at, ended_at. Returns None if too few obs.
    """
    with get_connection() as conn:
        row = conn.execute(
            "SELECT started_at, obs_count, cwd FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return None

        obs_rows = conn.execute(
            """SELECT tool_name, tool_input_excerpt, tool_output_excerpt,
                      file_paths, cmd_excerpt, ts
               FROM observations WHERE session_id = ?
               ORDER BY id""",
            (session_id,),
        ).fetchall()

    if len(obs_rows) < MIN_OBS_FOR_SUMMARY:
        return None

    # tool counts
    tool_counts = Counter(r["tool_name"] for r in obs_rows)
    top_tools = tool_counts.most_common(TOP_TOOLS)

    # file paths (dedup, preserve insertion order, top N most-frequent)
    file_counts = Counter()
    for r in obs_rows:
        fp_json = r["file_paths"]
        if not fp_json:
            continue
        try:
            paths = json.loads(fp_json)
        except Exception:
            continue
        for p in paths:
            if isinstance(p, str) and p:
                file_counts[p] += 1
    top_files = [p for p, _ in file_counts.most_common(TOP_FILES)]

    # topic extraction — TF-IDF if flag on (default), else legacy frequency
    with get_connection() as conn:
        top_topics = _topics_for_session(conn, session_id, obs_rows)

    # time range
    started_at = row["started_at"]
    ended_at = obs_rows[-1]["ts"]
    duration_s = max(1, ended_at - started_at)
    if duration_s >= 3600:
        duration_str = "{}h {}m".format(duration_s // 3600, (duration_s % 3600) // 60)
    elif duration_s >= 60:
        duration_str = "{}m {}s".format(duration_s // 60, duration_s % 60)
    else:
        duration_str = "{}s".format(duration_s)

    # narrative paragraph
    tool_phrase = ", ".join(["{}x{}".format(n, c) for n, c in top_tools])
    summary_text = (
        "session {} obs over {}, tools: {}. "
        "touched {} unique files. "
        "topics: {}".format(
            len(obs_rows), duration_str, tool_phrase,
            len(file_counts),
            ", ".join(top_topics[:5]) if top_topics else "(none extracted)",
        )
    )

    return {
        "session_id": session_id,
        "summary_text": summary_text,
        "key_files": json.dumps(top_files),
        "key_topics": json.dumps(top_topics),
        "observation_count": len(obs_rows),
        "started_at": started_at,
        "ended_at": ended_at,
    }


def write_summary(summary):
    """Upsert into session_summaries and mark session ended."""
    now = int(time.time())
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO session_summaries(
                 session_id, summary_text, key_files, key_topics,
                 observation_count, generated_at
               ) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 summary_text=excluded.summary_text,
                 key_files=excluded.key_files,
                 key_topics=excluded.key_topics,
                 observation_count=excluded.observation_count,
                 generated_at=excluded.generated_at""",
            (
                summary["session_id"], summary["summary_text"],
                summary["key_files"], summary["key_topics"],
                summary["observation_count"], now,
            ),
        )
        conn.execute(
            "UPDATE sessions SET status='ended', ended_at=? WHERE session_id=? AND status='active'",
            (summary["ended_at"], summary["session_id"]),
        )
        conn.commit()


def _resolve_latest_active():
    """Return the most-recently-started active session_id, or None."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT session_id FROM sessions WHERE status='active' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        return row["session_id"] if row else None


def main():
    try:
        ensure_db()

        # CLI path (for testing)
        if len(sys.argv) > 1 and sys.argv[1] in ("--session-id", "-s"):
            sid = sys.argv[2] if len(sys.argv) > 2 else None
            if sid == "LATEST" or sid is None:
                sid = _resolve_latest_active()
            if not sid:
                print("no active session found")
                sys.exit(0)
        else:
            # Hook path: read stdin
            payload = _read_stdin()
            sid = resolve_session_id(payload)

        summary = summarize_session(sid)
        if summary is None:
            # Don't fail — small/missing sessions are normal.
            sys.exit(0)

        write_summary(summary)

        # Print to stdout only when invoked interactively (not from hook).
        if sys.argv[0].endswith("session_summarize.py") and len(sys.argv) > 1:
            print("session_id:", summary["session_id"])
            print("obs:", summary["observation_count"])
            print("summary:", summary["summary_text"])
            print("key_files:", summary["key_files"])
            print("key_topics:", summary["key_topics"])

    except Exception as e:
        try:
            log_error("session_summarize: " + str(e))
        except Exception:
            pass
    finally:
        sys.exit(0)


if __name__ == "__main__":
    main()
