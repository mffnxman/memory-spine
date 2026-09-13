"""
pretooluse_dispatcher.py — single-process PreToolUse hook (v15).

Replaces five separate hook entries (two inline sh protected-file guards,
conflict.py, provenance.py, read_gate.py) that each cost a process spawn per
tool call. Reads stdin ONCE and routes by tool_name:

  Edit|Write      → protected-file guard (ask on settings/credentials paths)
                    then, for top-level memory .md targets only:
                      Write      → conflict.main()      (near-dupe → ask)
                      Write|Edit → provenance.hook_main() (pre-edit snapshot)
  Bash|PowerShell → protected-command guard (ask when cmd mentions those files)
  Read            → read_gate.main() (cache hit → deny with cached summary)

Decision semantics: conflict and read_gate print their own hookSpecificOutput
JSON; the guards print via _ask(). Trigger conditions are mutually exclusive
(protected config paths vs top-level memory .md vs Read cache), so at most one
decision is ever printed per call — same net behavior as the separate hooks.
If a guard fires we stop dispatching; nothing later acts on those paths anyway.

The heavy imports (conflict → memory_engine) only happen after a cheap regex
gate on the target path, so ordinary Writes stay fast.

Each handler is fail-soft. The dispatcher itself never raises — any unhandled
exception is swallowed (default-allow) so we don't block the tool.

Hook contract: stdin = JSON {tool_name, tool_input}, stdout = at most one
permission-decision JSON, silence = allow.
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import paths as _paths  # noqa: E402

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Mirrors the retired inline sh guards (bigbuff_phase3, 2026-07-06). Substring
# match on the slash-normalized path — same effect as the old `grep -q "$P"`.
PROTECTED_PATH_PARTS = (
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".claude/.credentials.json",
)
PROTECTED_CMD_RE = re.compile(
    r"settings\.json|settings\.local\.json|\.credentials\.json"
)
# Top-level memory .md only — same pattern the inline wrappers grepped for.
MEMORY_MD_RE = re.compile(re.escape(_paths.PROJECT_SLUG) + r"/memory/[^/]*\.md$")


def _norm(p: str) -> str:
    return (p or "").replace("\\", "/")


def _ask(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "ask",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )


def _call_with_stdin(fn, raw: str) -> None:
    """Run a handler that reads stdin itself, feeding it the raw payload.
    Catches BaseException so a handler's sys.exit() can't kill the dispatch."""
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(raw)
    try:
        fn()
    except BaseException:
        pass
    finally:
        sys.stdin = old_stdin


def main():
    try:
        raw = sys.stdin.read()
    except Exception:
        return
    try:
        payload = json.loads(raw) if raw else {}
    except Exception:
        return

    tool_name = payload.get("tool_name") or payload.get("toolName") or ""
    tool_input = payload.get("tool_input") or payload.get("toolInput") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    if tool_name in ("Bash", "PowerShell"):
        cmd = str(tool_input.get("command") or tool_input.get("cmd") or "")
        if PROTECTED_CMD_RE.search(cmd):
            _ask("Shell command touches a protected config file — confirm")
        return

    if tool_name in ("Edit", "Write"):
        file_path = _norm(
            tool_input.get("file_path") or tool_input.get("filePath") or ""
        )
        if any(part in file_path for part in PROTECTED_PATH_PARTS):
            _ask("Protected config file — confirm before editing")
            return
        if MEMORY_MD_RE.search(file_path):
            if tool_name == "Write":
                try:
                    import conflict

                    _call_with_stdin(conflict.main, raw)
                except BaseException:
                    pass
            try:
                import provenance

                _call_with_stdin(provenance.hook_main, raw)
            except BaseException:
                pass
        return

    if tool_name == "Read":
        try:
            import read_gate

            _call_with_stdin(read_gate.main, raw)
        except BaseException:
            pass
        return


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
