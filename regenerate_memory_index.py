"""
regenerate_memory_index.py — refresh ONLY the auto-indexed section of MEMORY.md.

MEMORY.md is mostly hand-curated content: User Context, Communication Style,
Topic Files with READ IF directives, Custom Skills, Setup notes, Workflow
Patterns, etc. We never touch those.

What we DO maintain: a marker-delimited auto-index at the bottom listing every
memory file grouped by type, with frontmatter description. Re-running this
script regenerates ONLY the content between the markers.

Markers:
  <!-- AUTO-INDEX-START -->
  ... auto-generated content ...
  <!-- AUTO-INDEX-END -->

If markers are absent on first run: we append them + the index at the end.
On subsequent runs: we replace everything between them.

Usage:
  python regenerate_memory_index.py           # in-place update
  python regenerate_memory_index.py --dry-run # print what would change
  python regenerate_memory_index.py --diff    # show old vs new
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from memory_engine import list_memories, MEMORY_DIR, INDEX_FILE

MARKER_START = "<!-- AUTO-INDEX-START -->"
MARKER_END = "<!-- AUTO-INDEX-END -->"

# Display labels and order for memory types. Anything not listed lands in "Other".
TYPE_ORDER = [
    ("self",      "Self (continuity / phenomenology)"),
    ("user",      "User Context Memories"),
    ("feedback",  "Feedback (operating preferences)"),
    ("project",   "Projects"),
    ("procedural", "Procedural (how-to habits)"),
    ("reference", "Reference"),
    ("digest",    "Weekly Digests"),
    ("agent",     "Agent Configuration"),
    ("hack",      "Security / Hacking"),
    ("session",   "Session Handoffs"),
    ("config",    "Configuration"),
    ("",          "Untyped"),
]


def _classify(mem) -> str:
    """Pick a display bucket for a memory based on type + filename."""
    t = (mem.type or "").lower()
    fn = mem.filename.lower()
    # Map filename prefixes when the type frontmatter is missing/loose
    if t in ("self", "user", "feedback", "project", "digest", "config"):
        return t
    if fn.startswith("self_"):
        return "self"
    if fn.startswith("user_"):
        return "user"
    if fn.startswith("feedback_"):
        return "feedback"
    if fn.startswith("digest_"):
        return "digest"
    if fn.startswith("dartagnan_") or fn.startswith("duck"):
        return "agent"
    if "hack" in fn or fn in ("project_alpha.md",):
        return "hack"
    if fn.startswith("session_handoff"):
        return "session"
    return ""


def render_index(mems: list) -> str:
    """Return the markdown block that goes BETWEEN the markers."""
    by_type: dict[str, list] = defaultdict(list)
    for m in mems:
        by_type[_classify(m)].append(m)

    lines: list[str] = []
    lines.append("")
    lines.append("## Auto-Indexed Memories")
    lines.append("")
    lines.append(
        f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} by "
        "`regenerate_memory_index.py`. Re-run anytime memories are added or weight "
        "changes._"
    )
    lines.append("")
    lines.append(f"**Total memories indexed:** {len(mems)}")
    lines.append("")

    for type_key, label in TYPE_ORDER:
        bucket = sorted(by_type.get(type_key, []), key=lambda m: m.filename)
        if not bucket:
            continue
        lines.append(f"### {label} ({len(bucket)})")
        lines.append("")
        for m in bucket:
            desc = (m.description or m.name or "").replace("\n", " ").strip()
            weight_tag = f"  *[weight: {m.weight}]*" if m.weight and m.weight.lower() == "high" else ""
            lines.append(f"- `{m.filename}` — {desc}{weight_tag}")
        lines.append("")

    # Lastly, anything that survived our classifier
    other_keys = set(by_type.keys()) - {k for k, _ in TYPE_ORDER}
    if other_keys:
        lines.append(f"### Other ({sum(len(by_type[k]) for k in other_keys)})")
        lines.append("")
        for k in sorted(other_keys):
            for m in sorted(by_type[k], key=lambda mm: mm.filename):
                desc = (m.description or m.name or "").replace("\n", " ").strip()
                lines.append(f"- `{m.filename}` [{k}] — {desc}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def splice(current_text: str, new_block: str) -> str:
    """Insert/replace the auto-index block between markers. Returns full new text."""
    block = f"{MARKER_START}\n{new_block}\n{MARKER_END}\n"
    if MARKER_START in current_text and MARKER_END in current_text:
        # Replace existing block
        pattern = re.compile(
            re.escape(MARKER_START) + r".*?" + re.escape(MARKER_END) + r"\n?",
            re.DOTALL,
        )
        return pattern.sub(block, current_text)
    # Append (with separator)
    suffix = "" if current_text.endswith("\n") else "\n"
    return current_text + suffix + "\n---\n\n" + block


def main():
    ap = argparse.ArgumentParser(description="Regenerate MEMORY.md auto-index section")
    ap.add_argument("--dry-run", action="store_true", help="Print result, don't write")
    ap.add_argument("--diff", action="store_true", help="Show old auto-index vs new")
    args = ap.parse_args()

    if not INDEX_FILE.exists():
        # If MEMORY.md is missing entirely, bootstrap one with just the auto-index
        current = "# Memory Index\n\n_(curated sections to be filled in by hand)_\n\n"
    else:
        current = INDEX_FILE.read_text(encoding="utf-8")

    mems = list_memories()
    new_block = render_index(mems)
    new_text = splice(current, new_block)

    if args.dry_run:
        print(new_text)
        return

    if args.diff:
        # Extract old block if present
        old_match = re.search(
            re.escape(MARKER_START) + r"\n(.*?)\n" + re.escape(MARKER_END),
            current, re.DOTALL,
        )
        old_block = old_match.group(1) if old_match else "(no previous auto-index)"
        print("=" * 60)
        print("OLD AUTO-INDEX:")
        print("=" * 60)
        print(old_block[:2000] + ("\n...[truncated]" if len(old_block) > 2000 else ""))
        print()
        print("=" * 60)
        print("NEW AUTO-INDEX:")
        print("=" * 60)
        print(new_block[:2000] + ("\n...[truncated]" if len(new_block) > 2000 else ""))
        return

    INDEX_FILE.write_text(new_text, encoding="utf-8")
    print(f"Updated {INDEX_FILE}")
    print(f"  Indexed: {len(mems)} memories")
    print(f"  File size: {INDEX_FILE.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
