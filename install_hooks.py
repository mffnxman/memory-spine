"""
install_hooks.py — idempotently register the memory hooks in Claude Code's
~/.claude/settings.json, and optionally install the slash commands.

Safe by design:
  - Backs up settings.json before any change
  - Every hook entry carries a trailing `# tag` comment; install skips tags
    that are already present and uninstall removes only tagged entries
  - Every command path is derived from where THIS file lives, so the same
    installer works on any machine and any project slug

Hooks registered (one process per event, routed inside the dispatchers):

  SessionStart      boot_ritual.py             surface latest epilogue + spine memories + health
                    session_log.py session-start
                    observer_session_start.py  rotate the observer session id
  UserPromptSubmit  prefetch.py                hybrid retrieval → inject relevant memories
                    session_log.py user-prompt
  PreToolUse        pretooluse_dispatcher.py   protected-file guard, conflict check, provenance snapshot, read gate
  PostToolUse       posttooluse_dispatcher.py  session log, memory-write postprocess, outbox drain, observer capture
  SessionEnd        session_end.py             epilogue-due flag + auto-draft
                    session_summarize.py       observer session summary

Usage:
  python install_hooks.py               # check + install hooks
  python install_hooks.py --commands    # also copy commands/*.md into ~/.claude/commands
  python install_hooks.py --check
  python install_hooks.py --uninstall   # hooks only; commands are left in place
  python install_hooks.py --env KEY=VALUE ...   # also record env (e.g. MEMORY_HOME) in settings.json "env"
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

SETTINGS = Path.home() / ".claude" / "settings.json"
COMMANDS_DST = Path.home() / ".claude" / "commands"
SCRIPTS_DIR = Path(__file__).parent.resolve()
COMMANDS_SRC = SCRIPTS_DIR / "commands"

PY = "python"


def _cmd(script: str, *args: str) -> str:
    parts = [PY, f'"{SCRIPTS_DIR / script}"', *args]
    return " ".join(parts)


# event -> list of (matcher, command, tag)
HOOKS = {
    "SessionStart": [
        ("", _cmd("boot_ritual.py"), "memory_engine_boot_ritual"),
        ("", _cmd("session_log.py", "session-start"), "memory_engine_session_log_boot"),
        ("", _cmd("observer_session_start.py"), "memory_engine_observer_session_start"),
    ],
    "UserPromptSubmit": [
        ("", _cmd("prefetch.py"), "memory_engine_prefetch"),
        ("", _cmd("session_log.py", "user-prompt"), "memory_engine_session_log_prompt"),
    ],
    "PreToolUse": [
        ("", _cmd("pretooluse_dispatcher.py"), "memory_engine_pretooluse_dispatcher"),
    ],
    "PostToolUse": [
        ("", _cmd("posttooluse_dispatcher.py"), "memory_engine_posttooluse_dispatcher"),
    ],
    "SessionEnd": [
        ("", _cmd("session_end.py"), "memory_engine_session_end"),
        ("", _cmd("session_summarize.py"), "memory_engine_observer_summarize"),
    ],
}
ALL_TAGS = {tag for entries in HOOKS.values() for _, _, tag in entries}


def load_settings() -> dict:
    return json.loads(SETTINGS.read_text(encoding="utf-8"))


def save_settings(s: dict) -> None:
    SETTINGS.write_text(json.dumps(s, indent=2), encoding="utf-8")


def backup() -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    bk = SETTINGS.with_suffix(f".json.bak_{ts}")
    shutil.copy2(SETTINGS, bk)
    return bk


def _tag_present(s: dict, event: str, tag: str) -> bool:
    for entry in s.get("hooks", {}).get(event, []):
        for h in entry.get("hooks", []):
            if tag in h.get("command", ""):
                return True
    return False


def is_installed(s: dict) -> dict:
    return {
        tag: _tag_present(s, event, tag)
        for event, entries in HOOKS.items()
        for _, _, tag in entries
    }


def _add_hook(s: dict, event: str, matcher: str, command: str, tag: str) -> None:
    hooks = s.setdefault("hooks", {}).setdefault(event, [])
    entry = {
        "hooks": [{"type": "command", "command": f"{command} 2>/dev/null # {tag}"}]
    }
    if matcher:
        entry["matcher"] = matcher
    hooks.append(entry)


def install(s: dict) -> dict:
    status = is_installed(s)
    for event, entries in HOOKS.items():
        for matcher, command, tag in entries:
            if not status[tag]:
                _add_hook(s, event, matcher, command, tag)
    return s


def uninstall(s: dict) -> dict:
    """Remove every hook entry whose command contains one of our tags."""
    hooks = s.get("hooks", {})
    for evt in list(hooks.keys()):
        new_entries = []
        for entry in hooks[evt]:
            kept = [
                h
                for h in entry.get("hooks", [])
                if not any(tag in h.get("command", "") for tag in ALL_TAGS)
            ]
            if kept:
                entry["hooks"] = kept
                new_entries.append(entry)
        hooks[evt] = new_entries
    return s


def install_commands() -> list[str]:
    """Render commands/*.md into ~/.claude/commands, substituting {{SCRIPTS_DIR}}."""
    if not COMMANDS_SRC.exists():
        print(f"no commands dir at {COMMANDS_SRC}")
        return []
    COMMANDS_DST.mkdir(parents=True, exist_ok=True)
    written = []
    scripts_posix = SCRIPTS_DIR.as_posix()
    for src in sorted(COMMANDS_SRC.glob("*.md")):
        text = src.read_text(encoding="utf-8").replace("{{SCRIPTS_DIR}}", scripts_posix)
        dst = COMMANDS_DST / src.name
        dst.write_text(text, encoding="utf-8")
        written.append(dst.name)
    return written


def apply_env(s: dict, pairs: list[str]) -> dict:
    env = s.setdefault("env", {})
    for p in pairs:
        if "=" not in p:
            print(f"skip malformed --env value: {p!r}")
            continue
        k, v = p.split("=", 1)
        env[k.strip()] = v.strip()
    return s


def main():
    argv = sys.argv[1:]
    env_pairs = []
    if "--env" in argv:
        i = argv.index("--env")
        env_pairs = [a for a in argv[i + 1 :] if not a.startswith("--")]
        argv = argv[:i] + [a for a in argv[i + 1 :] if a.startswith("--")]

    if not SETTINGS.exists():
        print(
            f"ERR: {SETTINGS} not found — open Claude Code once so it creates the file"
        )
        sys.exit(1)

    s = load_settings()
    status = is_installed(s)

    if "--check" in argv:
        print("Hooks:")
        for k, v in status.items():
            print(f"  [{'OK ' if v else '-- '}] {k}")
        print(
            f"Commands dir: {COMMANDS_DST}  ({len(list(COMMANDS_DST.glob('*.md'))) if COMMANDS_DST.exists() else 0} files)"
        )
        return

    if "--uninstall" in argv:
        if not any(status.values()):
            print("Nothing to uninstall.")
            return
        bk = backup()
        s = uninstall(s)
        save_settings(s)
        print(f"Uninstalled memory hooks. Backup: {bk}")
        return

    changed = False
    if not all(status.values()) or env_pairs:
        bk = backup()
        s = install(s)
        if env_pairs:
            s = apply_env(s, env_pairs)
        save_settings(s)
        changed = True
        print(f"Installed memory hooks. Backup: {bk}")
        new_status = is_installed(s)
        for k, v in new_status.items():
            print(f"  [{'+' if (v and not status.get(k)) else ' '}] {k}")
    else:
        print("All memory hooks already installed — no changes.")

    if "--commands" in argv:
        written = install_commands()
        print(
            f"Installed {len(written)} commands into {COMMANDS_DST}: {', '.join(written)}"
        )
        changed = True

    if changed:
        print(
            "Restart Claude Code (or /hooks reload) for the new hooks to take effect."
        )


if __name__ == "__main__":
    main()
