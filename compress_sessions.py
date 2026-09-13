"""compress_sessions.py — sleep-time semantic compression of sessions
(v15, 2026-07-09).

claude-mem compresses observations with AI at capture time; we deliberately
keep the capture hot path dumb and <50ms. This pass adds the same semantic
density at SLEEP time instead: each ended session's raw observation excerpts
get LLM-compressed into one dense summary paragraph + topics.

Routing is LOCAL-FIRST via tier_router task `observation_compression`
(prefer_local -> d'Artagnan, $0.00, zero subscription quota when the local model
is awake; subscription/cloud fallback when not). Capped per cycle so the
904-session backlog amortizes across weeks without a big bill.

Summaries upsert into session_summaries (compressed_at stamped) and mirror
into a summaries_fts table so the prefetch observer-fallback can match on
MEANING, not just raw command text.

CLI:
    python compress_sessions.py run [cap]     # compress up to cap sessions
    python compress_sessions.py status        # coverage counts
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import paths as _paths  # noqa: E402

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

MEMORY_DIR = _paths.MEMORY_DIR
DEFAULT_DB = MEMORY_DIR / "_meta" / "observations.db"

CAP_PER_CYCLE = 10
MIN_OBS = 3

COMPRESS_PROMPT = """These are raw tool-call observations from ONE work session in the user and Claude's system (Claude Code on Windows). Compress them into a dense, retrieval-friendly summary.

SESSION OBSERVATIONS ({n_obs} total, excerpts):
{obs_block}

FILES TOUCHED: {files}

Output ONE JSON object:
  "summary": one dense paragraph (3-5 sentences) — what was actually worked on, which files/systems, what the session accomplished or attempted. Write for future retrieval: concrete nouns, filenames, project names. No fluff.
  "topics": array of 3-6 short topic keywords

Output ONLY the JSON, no markdown fences."""


def _conn(db_path=None):
    import sqlite3

    return sqlite3.connect(str(db_path or DEFAULT_DB), timeout=5.0)


def ensure_schema(db_path=None):
    """Idempotent migration: compressed_at column + summaries FTS table."""
    with _conn(db_path) as conn:
        cols = [d[1] for d in conn.execute("PRAGMA table_info(session_summaries)")]
        if "compressed_at" not in cols:
            conn.execute(
                "ALTER TABLE session_summaries ADD COLUMN compressed_at INTEGER"
            )
        conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS summaries_fts USING fts5(
                   session_id, summary_text, key_topics)""")
        conn.commit()


def select_targets(db_path=None, cap=CAP_PER_CYCLE):
    """Ended sessions with >= MIN_OBS observations and no compressed summary,
    newest first."""
    ensure_schema(db_path)
    with _conn(db_path) as conn:
        rows = conn.execute(
            """SELECT s.session_id FROM sessions s
               LEFT JOIN session_summaries m ON m.session_id = s.session_id
               WHERE s.status != 'active'
                 AND s.obs_count >= ?
                 AND (m.compressed_at IS NULL)
               ORDER BY s.started_at DESC LIMIT ?""",
            (MIN_OBS, cap),
        ).fetchall()
    return [r[0] for r in rows]


def build_prompt(session_id, db_path=None, max_obs=30):
    """Assemble the compression prompt from a session's observations."""
    with _conn(db_path) as conn:
        rows = conn.execute(
            """SELECT tool_name, cmd_excerpt, tool_input_excerpt, file_paths
               FROM observations WHERE session_id = ? ORDER BY ts LIMIT ?""",
            (session_id, max_obs),
        ).fetchall()
    obs_lines, files = [], set()
    for tool, cmd, tin, fps in rows:
        excerpt = (cmd or tin or "").replace("\n", " ")[:200]
        obs_lines.append(f"- {tool}: {excerpt}")
        if fps:
            try:
                for p in json.loads(fps):
                    if isinstance(p, str):
                        files.add(p.replace("\\", "/").rsplit("/", 1)[-1])
            except (json.JSONDecodeError, TypeError):
                pass
    return COMPRESS_PROMPT.format(
        n_obs=len(rows),
        obs_block="\n".join(obs_lines),
        files=", ".join(sorted(files)[:15]) or "(none recorded)",
    )


def _draft(prompt):
    """LLM compression via tier_router (local-first). None = skip this cycle."""
    try:
        from providers import get_provider
        from tier_router import route, log_use
    except Exception:
        return None
    try:
        provider_name, model, params = route("observation_compression")
        provider = get_provider(provider_name)
        if not provider.health_check():
            return None
        resp = provider.generate(prompt, model=model, max_tokens=768, temperature=0.3)
    except Exception:
        return None
    txt = (resp.text or "").strip()
    txt = re.sub(r"^```(?:json)?\s*", "", txt)
    txt = re.sub(r"\s*```$", "", txt)
    try:
        parsed = json.loads(txt)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", txt, re.DOTALL)
        if not m:
            return None
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    if not parsed.get("summary"):
        return None
    try:
        log_use(
            "observation_compression",
            model,
            tokens_in=resp.tokens_in or 0,
            tokens_out=resp.tokens_out or 0,
        )
    except Exception:
        pass
    return parsed


def run(db_path=None, cap=CAP_PER_CYCLE):
    """One compression cycle. Returns summary dict."""
    ensure_schema(db_path)
    targets = select_targets(db_path, cap)
    summary = {"considered": len(targets), "compressed": 0, "skipped_draft_failed": 0}
    now = int(time.time())
    for sid in targets:
        prompt = build_prompt(sid, db_path)
        drafted = _draft(prompt)
        if not drafted:
            summary["skipped_draft_failed"] += 1
            continue
        topics = json.dumps(drafted.get("topics", []))
        with _conn(db_path) as conn:
            n_obs = conn.execute(
                "SELECT COUNT(*) FROM observations WHERE session_id = ?", (sid,)
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO session_summaries
                       (session_id, summary_text, key_files, key_topics,
                        observation_count, generated_at, compressed_at)
                   VALUES (?,?,COALESCE((SELECT key_files FROM session_summaries
                                         WHERE session_id=?), '[]'),?,?,?,?)
                   ON CONFLICT(session_id) DO UPDATE SET
                       summary_text=excluded.summary_text,
                       key_topics=excluded.key_topics,
                       compressed_at=excluded.compressed_at""",
                (sid, drafted["summary"].strip(), sid, topics, n_obs, now, now),
            )
            conn.execute(
                "INSERT INTO summaries_fts (session_id, summary_text, key_topics) "
                "VALUES (?,?,?)",
                (sid, drafted["summary"].strip(), topics),
            )
            conn.commit()
        summary["compressed"] += 1
    return summary


def status(db_path=None):
    ensure_schema(db_path)
    with _conn(db_path) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE status != 'active' AND obs_count >= ?",
            (MIN_OBS,),
        ).fetchone()[0]
        done = conn.execute(
            "SELECT COUNT(*) FROM session_summaries WHERE compressed_at IS NOT NULL"
        ).fetchone()[0]
    return {"eligible_sessions": total, "compressed": done}


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "run":
        cap = int(sys.argv[2]) if len(sys.argv) > 2 else CAP_PER_CYCLE
        print(json.dumps(run(cap=cap), indent=2))
    elif cmd == "status":
        print(json.dumps(status(), indent=2))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
