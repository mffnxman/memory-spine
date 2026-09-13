"""
posttooluse_dispatcher.py — single-process PostToolUse hook (v15).

v14.1 replaced three separate Python subprocesses (session_log,
memory_write_postprocess, outbox_worker tick) that each cold-started
~80-100ms apiece. v15 folds in the four remaining PostToolUse hooks
(black format, html auto-open, read_gate_record, observation_capture)
so the whole event is ONE process for every tool, routed by tool_name:

  Write|Edit → 1. black on .py files (subprocess on purpose: the black CLI
                  does pyproject.toml config discovery the API call skips)
               2. html auto-open on Write of .html (os.startfile)
               3. session_log.handle_post_tool_use — rolling activity log
               4. memory_write_postprocess.main()  — memory .md → emit event
               5. outbox_worker.drain(max=5)       — drain pending events
  Read       → read_gate_record.main() — cache a preview for read_gate
  all tools  → observation_capture.main() — observer row (flag-gated)

Each handler is fail-soft on its own. Handlers are wrapped with BaseException
because observation_capture sys.exit(0)s in a finally — a plain Exception
catch would let that SystemExit kill the rest of the dispatch. The dispatcher
itself never raises — any unhandled exception is swallowed so we don't block
the tool.

Hook contract: stdin = JSON payload, stdout = silent (no permission decisions).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import paths as _paths  # noqa: E402

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def _read_stdin_once() -> tuple[str, dict]:
    """Read stdin once; return (raw_text, parsed_dict). Handlers that need
    JSON can use the dict; those that need raw text can re-pass it.

    The raw text is preserved so memory_write_postprocess can pipe it via a
    fake stdin if it expects stdin input."""
    try:
        raw = sys.stdin.read()
    except Exception:
        return "", {}
    try:
        return raw, json.loads(raw) if raw else {}
    except Exception:
        return raw, {}


def _run_session_log(payload: dict) -> None:
    try:
        import session_log

        # session_log.handle_post_tool_use reads stdin internally — pass through
        # by temporarily replacing sys.stdin
        import io

        old_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(payload))
        try:
            session_log.handle_post_tool_use()
        finally:
            sys.stdin = old_stdin
    except Exception:
        pass


def _run_memory_write_postprocess(payload: dict, raw: str) -> None:
    """Only fires for Write/Edit on a top-level memory .md file."""
    tool_name = payload.get("tool_name") or payload.get("toolName") or ""
    if tool_name not in ("Write", "Edit"):
        return
    inp = payload.get("tool_input") or payload.get("toolInput") or {}
    file_path = inp.get("file_path") or inp.get("filePath") or ""
    if not file_path:
        return
    # Cheap path guard before importing
    if str(_paths.MEMORY_DIR).replace(
        "\\", "/"
    ) + "/" not in file_path.replace(
        "\\", "/"
    ):  # portability v15
        return
    if not file_path.endswith(".md"):
        return
    try:
        import memory_write_postprocess
        import io

        old_stdin = sys.stdin
        sys.stdin = io.StringIO(raw or json.dumps(payload))
        try:
            memory_write_postprocess.main()
        finally:
            sys.stdin = old_stdin
    except Exception:
        pass


def _run_outbox_drain() -> None:
    """Drain a small batch on the hot path.

    This runs INSIDE the Write/Edit PostToolUse hook (60s budget). Two guards
    keep it off the freeze path that dropped tool_results (diagnosed 2026-06-03):
      1. hot_path=True → outbox_worker skips LLM-backed kinds (consolidate/
         reflection/importance_score) and caps wall-clock at HOT_PATH_BUDGET_SEC.
         Those jobs wait for the cron drainer instead.
      2. MEMORY_DISABLE_SUBSCRIPTION_PROVIDER=1 → belt-and-suspenders: even if a
         "cheap" job unexpectedly routes to an LLM, the router (tier_router.py)
         will NOT shell out to the nested `claude -p` CLI (180s, the UI-freeze
         vector). Set only for this in-hook process; the cron drainer is
         unaffected.
    """
    try:
        import os

        os.environ.setdefault("MEMORY_DISABLE_SUBSCRIPTION_PROVIDER", "1")
        import outbox_worker

        outbox_worker.drain(max_jobs=5, verbose=False, hot_path=True)
    except Exception:
        pass


def _call_with_stdin(fn, raw: str) -> None:
    """Run a handler that reads stdin itself, feeding it the raw payload.
    Catches BaseException so a handler's sys.exit() can't kill the dispatch
    (observation_capture sys.exit(0)s in a finally, always)."""
    import io

    old_stdin = sys.stdin
    sys.stdin = io.StringIO(raw)
    try:
        fn()
    except BaseException:
        pass
    finally:
        sys.stdin = old_stdin


def _run_black_format(file_path: str) -> None:
    """Write|Edit on .py — format via the black CLI. Subprocess on purpose:
    the CLI does pyproject.toml config discovery that the API call skips, so
    in-process black could format differently inside configured repos."""
    try:
        import subprocess

        subprocess.run(
            [sys.executable, "-m", "black", "--quiet", file_path],
            capture_output=True,
            timeout=25,
        )
    except Exception:
        pass


def _run_html_autoopen(file_path: str) -> None:
    """Write of .html — open in the default browser (was: sh `start`)."""
    try:
        import os

        if Path(file_path).exists():
            os.startfile(file_path)
    except Exception:
        pass


def _run_read_gate_record(raw: str) -> None:
    """Read — stash a preview so read_gate can short-circuit repeat reads."""
    try:
        import read_gate_record

        _call_with_stdin(read_gate_record.main, raw)
    except BaseException:
        pass


def _run_observation_capture(raw: str) -> None:
    """All tools — write one observer row (flag-gated inside the module).
    BaseException on the import too: the module sys.exit(0)s at import time
    if observer_lib is missing."""
    try:
        import observation_capture

        _call_with_stdin(observation_capture.main, raw)
    except BaseException:
        pass


def main():
    try:
        raw, payload = _read_stdin_once()
        tool_name = payload.get("tool_name") or payload.get("toolName") or ""
        tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
        if not isinstance(tool_input, dict):
            tool_input = {}
        file_path = tool_input.get("file_path") or tool_input.get("filePath") or ""

        if tool_name in ("Write", "Edit"):
            if file_path.lower().endswith(".py"):
                _run_black_format(file_path)
            if tool_name == "Write" and file_path.lower().endswith(".html"):
                _run_html_autoopen(file_path)
            # Order matters: log first (cheapest, captures activity), then
            # memory-specific processing, then drain whatever the previous
            # steps may have queued.
            _run_session_log(payload)
            _run_memory_write_postprocess(payload, raw)
            _run_outbox_drain()
        elif tool_name == "Read":
            _run_read_gate_record(raw)

        # Observer sees every tool, last — it captures what the others did.
        _run_observation_capture(raw)
    except Exception:
        pass


if __name__ == "__main__":
    main()
