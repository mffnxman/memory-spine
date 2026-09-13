"""
read_gate_record.py — PostToolUse hook on Read.

Companion to read_gate.py. After Claude reads a file successfully, stash a
preview-summary so subsequent reads in this 24h window can be short-circuited.

Quiet — never blocks or pollutes stdout. Best-effort.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

MAX_PREVIEW_CHARS = 1200  # v14.2: was 400; richer preview for gated small files


def build_preview(text: str, size: int) -> str:
    """Build a cache preview. Markdown section headers (if any) lead so the
    preview is structure-aware, then a flattened body slice + an honest size/
    partial-content hint so a preview is never mistaken for the full file.
    """
    headings = [ln.strip() for ln in text.splitlines()
                if ln.lstrip().startswith("#")][:8]
    body = " ".join(text.split())  # flatten whitespace
    if len(body) > MAX_PREVIEW_CHARS:
        body = body[:MAX_PREVIEW_CHARS] + "..."
    parts = []
    if headings:
        parts.append("Section headers: " + " | ".join(headings))
    parts.append(body)
    parts.append(f"(file size: {size:,} bytes — PARTIAL preview captured by "
                 f"read_gate, not the full file)")
    return "\n\n".join(parts)


def main():
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        return

    if payload.get("tool_name") != "Read":
        return

    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path") or ""
    if not file_path:
        return

    try:
        from read_gate import _is_excluded, record, _safe_path, MIN_FILE_BYTES
    except Exception:
        return

    if _is_excluded(file_path):
        return

    try:
        p = Path(_safe_path(file_path))
        if not p.exists() or p.is_dir():
            return
        size = p.stat().st_size
        if size < MIN_FILE_BYTES:
            return
        # Structure-aware preview (headings + flattened body slice + size hint).
        with p.open("r", encoding="utf-8", errors="ignore") as f:
            raw = f.read(4000)
        summary = build_preview(raw, size)
        record(file_path, summary)
    except Exception:
        pass


if __name__ == "__main__":
    main()
