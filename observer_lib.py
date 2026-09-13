"""
observer_lib.py - shared library for the homemade observer.

Phase 0 of plan_homemade_observer_v1. Native python, sqlite-backed, replaces
the bricked claude-mem v13 windows install.

Provides:
- ensure_db()         idempotent schema creation
- get_connection()    WAL-mode sqlite connection
- truncate_bytes()    safe UTF-8 boundary truncation
- is_path_excluded()  privacy glob check
- has_bash_credential() bash credential heuristic
- redact_tokens()     regex strip token shapes
- resolve_session_id() payload -> env -> file -> uuid
- ensure_session()    upsert session row
- record_observation() insert observation row
- log_error()         append to observer_errors.log with rotation

Design rules:
- ASCII-only output (windows cp1252 can't print unicode in print/log)
- fail-open: callers should wrap calls in try/except and exit 0
- never raise from hot-path library functions

Run directly for a self-test:
    python observer_lib.py
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path
import sys as _sys  # noqa: E402
_sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths as _paths  # noqa: E402

# ---- paths ----
# Portability (v15): derive from this file's location — the memory dir travels
# as a unit. The old expanduser path broke on any machine whose username (and
# therefore Claude Code project-dir slug) differed.
MEMORY_ROOT = _paths.MEMORY_DIR
META_DIR = MEMORY_ROOT / "_meta"
DB_PATH = META_DIR / "observations.db"
ERROR_LOG = META_DIR / "observer_errors.log"
SESSION_ID_FILE = Path(os.path.expanduser("~/.claude/.session_id"))

# ---- privacy: paths we never observe ----
EXCLUDED_PATH_GLOBS = (
    "*.env",
    "*.env.*",
    "**/credentials*",
    "**/.credentials*",
    "**/.git/objects/**",
    "**/node_modules/**",
    "**/__pycache__/**",
    "*.key",
    "*.pem",
    "**/.ssh/**",
    "**/.gnupg/**",
    "*history",
    "*_history",
    "*.bak-pre-*",
)

# ---- privacy: bash tokens that mean "drop the excerpt" ----
BASH_CREDENTIAL_TOKENS = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "bearer",
    "--auth",
    "authorization:",
)

# ---- privacy: regex patterns scrubbed from tool output ----
TOKEN_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),  # openai / anthropic
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),  # github PAT
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key
    re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),  # JWT
)
REDACTED = "[REDACTED]"

# ---- schema ----
SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS sessions (
  session_id     TEXT PRIMARY KEY,
  started_at     INTEGER NOT NULL,
  ended_at       INTEGER,
  cwd            TEXT,
  prompt_count   INTEGER DEFAULT 0,
  obs_count      INTEGER DEFAULT 0,
  status         TEXT DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS observations (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id            TEXT NOT NULL,
  ts                    INTEGER NOT NULL,
  tool_name             TEXT NOT NULL,
  tool_input_excerpt    TEXT,
  tool_input_size       INTEGER,
  tool_output_excerpt   TEXT,
  tool_output_size      INTEGER,
  file_paths            TEXT,
  cmd_excerpt           TEXT,
  ms_elapsed            INTEGER,
  promoted_to_memory_id TEXT,
  referenced_count      INTEGER DEFAULT 0,
  last_referenced_at    INTEGER,
  FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE TABLE IF NOT EXISTS session_summaries (
  session_id        TEXT PRIMARY KEY,
  summary_text      TEXT,
  key_files         TEXT,
  key_topics        TEXT,
  observation_count INTEGER,
  generated_at      INTEGER,
  FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_obs_session  ON observations(session_id);
CREATE INDEX IF NOT EXISTS idx_obs_ts       ON observations(ts);
CREATE INDEX IF NOT EXISTS idx_obs_tool     ON observations(tool_name);
CREATE INDEX IF NOT EXISTS idx_obs_promoted ON observations(promoted_to_memory_id);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at);

CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts USING fts5(
  tool_input_excerpt,
  tool_output_excerpt,
  cmd_excerpt,
  file_paths,
  content='observations',
  content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS obs_ai AFTER INSERT ON observations BEGIN
  INSERT INTO observations_fts(rowid, tool_input_excerpt, tool_output_excerpt, cmd_excerpt, file_paths)
  VALUES (new.id, new.tool_input_excerpt, new.tool_output_excerpt, new.cmd_excerpt, new.file_paths);
END;

CREATE TRIGGER IF NOT EXISTS obs_ad AFTER DELETE ON observations BEGIN
  INSERT INTO observations_fts(observations_fts, rowid, tool_input_excerpt, tool_output_excerpt, cmd_excerpt, file_paths)
  VALUES ('delete', old.id, old.tool_input_excerpt, old.tool_output_excerpt, old.cmd_excerpt, old.file_paths);
END;
"""


# ---- connection / schema ----
def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """Open a sqlite connection. Caller closes."""
    conn = sqlite3.connect(str(db_path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_db(db_path: Path = DB_PATH) -> None:
    """Create db file + tables if missing. Idempotent."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_connection(db_path) as conn:
        conn.executescript(SCHEMA)


# ---- byte-safe truncation ----
def truncate_bytes(text, max_bytes):
    """Truncate text to fit in max_bytes UTF-8 bytes at a char boundary.

    Returns (truncated_text, original_byte_size).
    text=None returns (None, 0).
    """
    if text is None:
        return None, 0
    if not isinstance(text, str):
        text = str(text)
    encoded = text.encode("utf-8", errors="replace")
    original_size = len(encoded)
    if original_size <= max_bytes:
        # Decode from the replaced bytes rather than returning `text` as-is:
        # lone surrogates (unpaired \ud800-\udfff in JSON payloads) are not
        # valid UTF-8 and make sqlite3 reject the bind downstream.
        return encoded.decode("utf-8", errors="ignore"), original_size
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return truncated, original_size


# ---- glob matching with ** support ----
def _glob_match(path, pattern):
    """Match a path against a gitignore-style glob.

    Rules:
      - patterns without '/' match against the basename only (gitignore-style)
      - '**' matches any sequence (including slashes)
      - '*'  matches any sequence except slash
      - '?'  matches any single char except slash
    """
    import fnmatch

    path_norm = path.replace("\\", "/")
    pattern_norm = pattern.replace("\\", "/")

    if "/" not in pattern_norm:
        basename = path_norm.rsplit("/", 1)[-1]
        return fnmatch.fnmatch(basename, pattern_norm)

    # build a regex char-by-char
    out = []
    i = 0
    while i < len(pattern_norm):
        c = pattern_norm[i]
        if c == "*" and i + 1 < len(pattern_norm) and pattern_norm[i + 1] == "*":
            out.append(".*")
            i += 2
            if i < len(pattern_norm) and pattern_norm[i] == "/":
                out.append("/?")
                i += 1
        elif c == "*":
            out.append("[^/]*")
            i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c in r".+()[]{}|^$\\":
            out.append(re.escape(c))
            i += 1
        else:
            out.append(c)
            i += 1
    regex = "".join(out)
    return bool(re.fullmatch(regex, path_norm))


def is_path_excluded(path):
    """True if path matches any privacy exclusion glob."""
    if not path:
        return False
    return any(_glob_match(path, p) for p in EXCLUDED_PATH_GLOBS)


def has_bash_credential(cmd):
    """True if a bash command contains credential-like tokens."""
    if not cmd:
        return False
    lower = str(cmd).lower()
    return any(tok in lower for tok in BASH_CREDENTIAL_TOKENS)


def redact_tokens(text):
    """Regex-strip common token shapes from text. Pass-through for None / empty."""
    if not text:
        return text
    out = text
    for pat in TOKEN_PATTERNS:
        out = pat.sub(REDACTED, out)
    return out


# ---- session id resolution ----
def resolve_session_id(payload=None):
    """Resolve session_id with fallback chain:
    1. payload['session_id']    (claude code hook payload)
    2. CLAUDE_SESSION_ID env
    3. ~/.claude/.session_id file
    4. new uuid (persisted to file)
    """
    if payload and isinstance(payload, dict):
        sid = payload.get("session_id")
        if sid:
            return str(sid)
    env_sid = os.environ.get("CLAUDE_SESSION_ID")
    if env_sid:
        return env_sid
    try:
        if SESSION_ID_FILE.exists():
            content = SESSION_ID_FILE.read_text(encoding="utf-8").strip()
            if content:
                return content
    except Exception:
        pass
    new_sid = str(uuid.uuid4())
    try:
        SESSION_ID_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_ID_FILE.write_text(new_sid, encoding="utf-8")
    except Exception:
        pass
    return new_sid


# ---- write helpers ----
def ensure_session(conn, session_id, cwd=None):
    """Upsert a session row. Idempotent."""
    now = int(time.time())
    conn.execute(
        "INSERT OR IGNORE INTO sessions(session_id, started_at, cwd, status) VALUES (?, ?, ?, 'active')",
        (session_id, now, cwd),
    )


def record_observation(
    conn,
    session_id,
    tool_name,
    tool_input_excerpt=None,
    tool_input_size=0,
    tool_output_excerpt=None,
    tool_output_size=0,
    file_paths=None,
    cmd_excerpt=None,
    ms_elapsed=None,
):
    """Insert one observation. Returns new row id."""
    ts = int(time.time())
    fp_json = json.dumps(file_paths) if file_paths else None

    # sqlite3 rejects str binds containing lone surrogates ("surrogates not
    # allowed"), losing the whole observation — scrub every text param.
    def _scrub(s):
        if isinstance(s, str):
            return s.encode("utf-8", errors="replace").decode("utf-8", errors="ignore")
        return s

    tool_name = _scrub(tool_name)
    tool_input_excerpt = _scrub(tool_input_excerpt)
    tool_output_excerpt = _scrub(tool_output_excerpt)
    cmd_excerpt = _scrub(cmd_excerpt)
    cur = conn.execute(
        """
        INSERT INTO observations(
            session_id, ts, tool_name,
            tool_input_excerpt, tool_input_size,
            tool_output_excerpt, tool_output_size,
            file_paths, cmd_excerpt, ms_elapsed
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            ts,
            tool_name,
            tool_input_excerpt,
            tool_input_size,
            tool_output_excerpt,
            tool_output_size,
            fp_json,
            cmd_excerpt,
            ms_elapsed,
        ),
    )
    conn.execute(
        "UPDATE sessions SET obs_count = obs_count + 1 WHERE session_id = ?",
        (session_id,),
    )
    return cur.lastrowid


# ---- error logging with rotation ----
ERROR_LOG_MAX_BYTES = 1_048_576  # 1 MB


def log_error(msg):
    """Append a single-line error. Rotates at 1 MB. Never raises."""
    try:
        ERROR_LOG.parent.mkdir(parents=True, exist_ok=True)
        if ERROR_LOG.exists() and ERROR_LOG.stat().st_size > ERROR_LOG_MAX_BYTES:
            rotated = ERROR_LOG.with_suffix(".log.1")
            try:
                if rotated.exists():
                    rotated.unlink()
                ERROR_LOG.rename(rotated)
            except Exception:
                ERROR_LOG.write_text("", encoding="utf-8")
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            f.write("[" + ts + "] " + str(msg) + "\n")
    except Exception:
        pass


# ---- self-test ----
if __name__ == "__main__":
    print("observer_lib.py self-test")
    print("  DB_PATH:   " + str(DB_PATH))
    print("  ERROR_LOG: " + str(ERROR_LOG))

    # truncate
    t, sz = truncate_bytes("hello", 100)
    assert t == "hello" and sz == 5, "truncate short"
    t, sz = truncate_bytes("a" * 1000, 50)
    assert len(t) == 50 and sz == 1000, "truncate long"
    t, sz = truncate_bytes(None, 100)
    assert t is None and sz == 0, "truncate None"
    # multi-byte boundary
    t, sz = truncate_bytes("a" * 10 + "é" * 5, 12)  # latin1 char = 2 bytes utf-8
    assert sz == 20, "multi-byte sz"
    assert len(t.encode("utf-8")) <= 12, "multi-byte truncated within budget"
    print("  truncate_bytes: OK")

    # path exclusion
    assert is_path_excluded("/some/path/.env"), ".env"
    assert is_path_excluded("/x/credentials.json"), "credentials*"
    assert is_path_excluded("/home/me/.ssh/id_rsa"), ".ssh/**"
    assert is_path_excluded("/x/node_modules/foo/bar.js"), "node_modules"
    assert is_path_excluded(
        "C:/Users/alex/.claude/settings.json.bak-pre-upgrade"
    ), "bak-pre"
    assert is_path_excluded("/home/me/.bash_history"), "history"
    assert not is_path_excluded("/x/src/main.py"), "normal py"
    assert not is_path_excluded(""), "empty"
    print("  is_path_excluded: OK")

    # bash credential
    assert has_bash_credential("export TOKEN=abc"), "TOKEN"
    assert has_bash_credential("curl -H 'Authorization: Bearer xyz'"), "Auth"
    assert has_bash_credential("aws --auth ..."), "--auth"
    assert not has_bash_credential("ls -la"), "normal"
    assert not has_bash_credential(None), "None"
    print("  has_bash_credential: OK")

    # token redaction
    assert REDACTED in redact_tokens(
        "my key is " + "sk-" + "abcdef0123456789012345"
    ), "openai key"
    assert REDACTED in redact_tokens(
        "token=" + "ghp_" + "a" * 32
    ), "github PAT"
    assert REDACTED in redact_tokens("AWS " + "AKIA" + "1234567890ABCDEF"), "AWS"
    jwt = "eyJhbGc.eyJzdWI.signaturepart"
    assert REDACTED in redact_tokens(jwt), "JWT"
    assert "hello world" == redact_tokens("hello world"), "clean"
    assert None is redact_tokens(None), "None"
    print("  redact_tokens: OK")

    # session id
    sid = resolve_session_id({"session_id": "test-payload-sid"})
    assert sid == "test-payload-sid", "payload sid"
    os.environ["CLAUDE_SESSION_ID"] = "env-sid"
    try:
        sid = resolve_session_id()
        assert sid == "env-sid", "env sid"
    finally:
        del os.environ["CLAUDE_SESSION_ID"]
    print("  resolve_session_id: OK")

    print("ALL TESTS PASS")
