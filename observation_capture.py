"""
observation_capture.py - PostToolUse hook for the homemade observer.

Phase 1 of plan_homemade_observer_v1. Reads stdin JSON payload, extracts
the tool call's signal, applies privacy filters, writes one observation row.

Hook contract:
  - PostToolUse on all tools (matcher: "" or "*")
  - stdin = JSON payload (tool_name, tool_input, [tool_response|tool_output])
  - exit 0 always (fail-open, never block)

Feature flag:
  - observer_capture_enabled in _meta/feature_flags.json
  - default false (capture off until phase 2 wires + flips on)

Performance budget:
  - <50ms p95 in-process write (already proven in phase 0 self-test: 11ms p95)
  - cold-start full hook ~70ms (python import overhead)

Usage (from settings.json):
    python observation_capture.py 2>/dev/null
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import paths as _paths  # noqa: E402

try:
    from observer_lib import (
        ensure_db, get_connection, ensure_session, record_observation,
        resolve_session_id, truncate_bytes,
        is_path_excluded, has_bash_credential, redact_tokens,
        log_error,
    )
except Exception:
    sys.exit(0)


# ---- feature flag ----
META_DIR = _paths.META_DIR
FLAGS_PATH = META_DIR / "feature_flags.json"


def _read_flag(name, default=False):
    # env override beats file (handy for tests + ad-hoc enable)
    env_key = "OBSERVER_FLAG_" + name.upper()
    if env_key in os.environ:
        return os.environ[env_key].lower() in ("1", "true", "yes", "on")
    try:
        if FLAGS_PATH.exists():
            with open(FLAGS_PATH, encoding="utf-8") as f:
                flags = json.load(f)
            return flags.get(name, default)
    except Exception:
        pass
    return default


def _read_int_flag(name, default):
    env_key = "OBSERVER_FLAG_" + name.upper()
    if env_key in os.environ:
        try:
            return int(os.environ[env_key])
        except Exception:
            pass
    try:
        if FLAGS_PATH.exists():
            with open(FLAGS_PATH, encoding="utf-8") as f:
                flags = json.load(f)
            val = flags.get(name, default)
            return int(val)
    except Exception:
        pass
    return default


# ---- payload helpers ----
def _read_payload():
    try:
        raw = sys.stdin.read()
        if not raw:
            return {}
        return json.loads(raw)
    except Exception:
        return {}


def _get(d, *keys):
    """Get the first non-empty value from snake/camel candidate keys."""
    for k in keys:
        if k in d and d[k]:
            return d[k]
    return None


def _safe_str(x, max_chars=10000):
    """Best-effort stringification with a sanity ceiling."""
    if x is None:
        return None
    if isinstance(x, str):
        return x[:max_chars]
    try:
        return json.dumps(x, default=str)[:max_chars]
    except Exception:
        return str(x)[:max_chars]


# ---- per-tool extractors ----
def _extract_file_paths(tool_name, tool_input):
    """Pull file paths from common tool input shapes. Returns list[str]."""
    if not isinstance(tool_input, dict):
        return []
    paths = []
    fp = _get(tool_input, "file_path", "filePath")
    if isinstance(fp, str):
        paths.append(fp)
    elif isinstance(fp, list):
        paths.extend([p for p in fp if isinstance(p, str)])
    # Some tools use "path" or "files"
    p = _get(tool_input, "path")
    if isinstance(p, str):
        paths.append(p)
    files = _get(tool_input, "files")
    if isinstance(files, list):
        paths.extend([f for f in files if isinstance(f, str)])
    # Plural 'paths' key (e.g. read_multiple_files) — was missed, so an
    # excluded path passed this way could bypass redaction.
    ps = _get(tool_input, "paths")
    if isinstance(ps, list):
        paths.extend([p for p in ps if isinstance(p, str)])
    return paths


def _extract_command(tool_name, tool_input):
    """Pull bash command (or equivalent) if applicable. Returns str or None."""
    if tool_name not in ("Bash", "PowerShell"):
        return None
    if not isinstance(tool_input, dict):
        return None
    return _get(tool_input, "command", "cmd")


# ---- main ----
def main():
    try:
        # Fast bail if flag is off — minimizes hook cost.
        if not _read_flag("observer_capture_enabled", False):
            sys.exit(0)

        payload = _read_payload()
        if not payload:
            sys.exit(0)

        tool_name = _get(payload, "tool_name", "toolName")
        if not tool_name:
            sys.exit(0)

        tool_input = _get(payload, "tool_input", "toolInput") or {}
        tool_output = _get(payload, "tool_response", "toolResponse",
                            "tool_output", "toolOutput")

        max_bytes = _read_int_flag("observer_max_excerpt_bytes", 1024)
        # Hard ceiling per the plan to prevent unbounded growth.
        max_bytes = min(max_bytes, 4096)

        # --- file path extraction + privacy gate ---
        file_paths = _extract_file_paths(tool_name, tool_input)
        # If ANY path is on the exclusion list, skip the whole observation.
        if any(is_path_excluded(p) for p in file_paths):
            sys.exit(0)

        # --- command extraction + redaction ---
        cmd = _extract_command(tool_name, tool_input)
        cmd_excerpt = None
        if cmd:
            cmd_str = str(cmd)
            if has_bash_credential(cmd_str):
                cmd_excerpt = "[REDACTED-CREDENTIAL]"
            else:
                # 200-char cap per plan, then redact token shapes just in case
                cmd_excerpt = redact_tokens(cmd_str[:200])

        # --- input excerpt + size ---
        tool_input_str = _safe_str(tool_input)
        if tool_input_str:
            tool_input_str = redact_tokens(tool_input_str)
        tool_input_excerpt, tool_input_size = truncate_bytes(tool_input_str, max_bytes)

        # --- output excerpt + size ---
        tool_output_str = _safe_str(tool_output)
        if tool_output_str:
            tool_output_str = redact_tokens(tool_output_str)
        tool_output_excerpt, tool_output_size = truncate_bytes(tool_output_str, max_bytes)

        # --- session id ---
        session_id = resolve_session_id(payload)

        # --- cwd (for ensure_session if we end up creating the row here) ---
        cwd = _get(payload, "cwd") or os.getcwd()

        # --- ms_elapsed (if claude code provides it; otherwise NULL) ---
        ms_elapsed = None
        for k in ("duration_ms", "durationMs", "ms_elapsed", "elapsed_ms"):
            v = payload.get(k)
            if isinstance(v, (int, float)):
                ms_elapsed = int(v)
                break

        # --- write ---
        ensure_db()
        with get_connection() as conn:
            ensure_session(conn, session_id, cwd=cwd)
            record_observation(
                conn,
                session_id=session_id,
                tool_name=str(tool_name),
                tool_input_excerpt=tool_input_excerpt,
                tool_input_size=tool_input_size,
                tool_output_excerpt=tool_output_excerpt,
                tool_output_size=tool_output_size,
                file_paths=file_paths or None,
                cmd_excerpt=cmd_excerpt,
                ms_elapsed=ms_elapsed,
            )
            conn.commit()

    except Exception as e:
        try:
            log_error("observation_capture: " + str(e))
        except Exception:
            pass
    finally:
        sys.exit(0)


if __name__ == "__main__":
    main()
