"""
recall.py — unified search across all memory layers.

Layers searched:
  1. Auto-memory (~/.claude/.../memory/*.md) — TF-IDF + hybrid via memory_engine
  2. Obsidian brain — grep across vault paths if available
  3. Observer — FTS5 search across observations.db (v14 phase 2.3)

Usage:
  python recall.py "<query>"               # all layers, text output
  python recall.py "<query>" --observer    # observer only
  python recall.py "<query>" --no-observer # skip observer
  python recall.py "<query>" --json        # JSON output
  python recall.py "<query>" --html        # write & open HTML
  python recall.py "<query>" --deep        # show full body of top hits
"""
from __future__ import annotations

import html
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# Force UTF-8 stdout on Windows so memory snippets with unicode render
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).parent))
import paths as _paths  # noqa: E402
from memory_engine import search, search_hybrid, list_memories, log_access

# Optional Obsidian vault (set OBSIDIAN_VAULT to enable)
OBSIDIAN_VAULTS = [_paths.OBSIDIAN_VAULT] if _paths.OBSIDIAN_VAULT else []
OUT_HTML = _paths.OUTPUT_DIR / "recall_results.html"


def grep_obsidian(query: str, max_results: int = 10) -> list[dict]:
    """Simple grep across Obsidian markdown files (case-insensitive)."""
    results = []
    q_lower = query.lower()
    q_words = [w for w in q_lower.split() if len(w) > 2]
    for vault in OBSIDIAN_VAULTS:
        if not vault.exists():
            continue
        for md in vault.rglob("*.md"):
            try:
                text = md.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            text_lower = text.lower()
            score = sum(text_lower.count(w) for w in q_words)
            if score == 0:
                continue
            # Find first match line
            snippet = ""
            for line in text.splitlines():
                if any(w in line.lower() for w in q_words):
                    snippet = line.strip()[:200]
                    break
            results.append({
                "path": str(md.relative_to(vault)),
                "vault": vault.name,
                "score": score,
                "snippet": snippet,
            })
    results.sort(key=lambda r: -r["score"])
    return results[:max_results]


def query_observer(query, limit=10):
    """Search observations.db via FTS5. Fail-open returns [] on any error."""
    try:
        from observer_query import search as obs_search
    except Exception:
        return []
    try:
        hits = obs_search(query, limit=limit)
    except Exception:
        return []
    # render for recall consumption — short, comparable to auto/obsidian shape
    out = []
    from datetime import datetime as _dt
    for h in hits:
        ts = h.get("ts", 0)
        when = _dt.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"
        # short file basenames if any
        fps = h.get("file_paths") or []
        basenames = [str(p).replace("\\", "/").rsplit("/", 1)[-1] for p in fps[:3]]
        out.append({
            "id": h.get("id"),
            "session": (h.get("session_id") or "?")[:8],
            "when": when,
            "tool": h.get("tool_name"),
            "files": basenames,
            "excerpt": (h.get("excerpt") or "")[:240],
            "score": h.get("score", 0.5),
        })
    return out


def run_recall(query, include_observer=True, observer_only=False):
    auto_results = []
    obsidian = []
    if not observer_only:
        mems = list_memories()
        auto = search_hybrid(query, mems=mems, top_k=10)
        obsidian = grep_obsidian(query)
        for m, score, matched in auto:
            log_access(m.filename, source="recall")
            snippet = ""
            for line in m.body.splitlines():
                if any(t in line.lower() for t in matched):
                    snippet = line.strip()[:240]
                    break
            auto_results.append({
                "file": m.filename,
                "name": m.name,
                "type": m.type,
                "description": m.description,
                "score": round(score, 3),
                "matched": matched,
                "snippet": snippet,
            })

    observer = query_observer(query) if include_observer else []

    return {
        "query": query,
        "auto_memory": auto_results,
        "obsidian": obsidian,
        "observer": observer,
        "_observer_included": include_observer,
        "_observer_only": observer_only,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }


def render_text(r: dict) -> str:
    lines = [f"== recall: '{r['query']}' ==\n"]
    observer_only = r.get("_observer_only", False)
    observer_included = r.get("_observer_included", True)

    if not observer_only:
        lines.append("--- Auto-Memory ---")
        if not r["auto_memory"]:
            lines.append("  (no matches)")
        for h in r["auto_memory"]:
            lines.append(f"  [{h['score']:.2f}] {h['file']}  ({h['type'] or '-'})")
            lines.append(f"          {h['name']}")
            if h["snippet"]:
                lines.append(f"          -> {h['snippet']}")

        lines.append("\n--- Obsidian Vault ---")
        if not r["obsidian"]:
            lines.append("  (no matches or vault not found)")
        for h in r["obsidian"][:6]:
            lines.append(f"  [{h['score']:>3}] {h['vault']}/{h['path']}")
            if h["snippet"]:
                lines.append(f"          -> {h['snippet']}")

    if observer_included:
        lines.append(f"\n--- Observer (live activity) ---")
        obs = r.get("observer") or []
        if not obs:
            lines.append("  (no matches in observations.db)")
        for h in obs[:6]:
            files_str = (", ".join(h["files"]) if h["files"] else "")
            lines.append(f"  [{h['when']}] {h['tool']}  session {h['session']}...")
            if files_str:
                lines.append(f"          files: {files_str}")
            if h["excerpt"]:
                lines.append(f"          -> {h['excerpt'][:200]}")
    return "\n".join(lines)


def render_html(r: dict) -> str:
    q = html.escape(str(r.get("query", "")))  # Q6: a query with < or </style> mangled the report
    auto_rows = "\n".join(
        f"""<tr>
            <td><span class="score">{h['score']}</span></td>
            <td><strong>{h['file']}</strong><div class="sub">{h['name']}</div></td>
            <td><span class="badge">{h['type'] or '—'}</span></td>
            <td>{h['snippet'] or h['description']}</td>
        </tr>"""
        for h in r["auto_memory"]
    ) or "<tr><td colspan='4' style='text-align:center;color:#666;'>No matches</td></tr>"

    obs_rows = "\n".join(
        f"""<tr>
            <td><span class="score">{h['score']}</span></td>
            <td><strong>{h['vault']}</strong>/{h['path']}</td>
            <td>{h['snippet']}</td>
        </tr>"""
        for h in r["obsidian"]
    ) or "<tr><td colspan='3' style='text-align:center;color:#666;'>No matches in Obsidian (or vault not found)</td></tr>"

    observer_rows = "\n".join(
        f"""<tr>
            <td><span class="score">{h['when']}</span></td>
            <td><span class="badge">{h['tool']}</span></td>
            <td><div class="sub">{", ".join(h["files"]) if h["files"] else "—"}</div></td>
            <td>{h['excerpt']}</td>
        </tr>"""
        for h in (r.get("observer") or [])
    ) or "<tr><td colspan='4' style='text-align:center;color:#666;'>No observations matched</td></tr>"

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>recall: {q}</title><style>
  *{{margin:0;padding:0;box-sizing:border-box;}}
  body{{font-family:'Segoe UI',sans-serif;background:linear-gradient(135deg,#0a0a1a,#1a1a2e 50%,#16213e);color:#e0e0e0;min-height:100vh;padding:24px;}}
  .container{{max-width:1200px;margin:0 auto;}}
  h1{{font-size:24px;background:linear-gradient(135deg,#00d4ff,#7c3aed);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:6px;}}
  .lead{{color:#888;margin-bottom:20px;font-size:13px;}}
  .panel{{background:rgba(255,255,255,0.05);backdrop-filter:blur(10px);border:1px solid rgba(255,255,255,0.1);border-radius:14px;padding:20px;margin-bottom:18px;}}
  h2{{font-size:14px;color:#00d4ff;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:12px;}}
  table{{width:100%;border-collapse:collapse;}}
  th{{text-align:left;padding:10px;border-bottom:2px solid rgba(255,255,255,0.1);color:#00d4ff;font-size:11px;text-transform:uppercase;letter-spacing:1px;}}
  td{{padding:10px;border-bottom:1px solid rgba(255,255,255,0.05);font-size:13px;vertical-align:top;}}
  tr:hover{{background:rgba(255,255,255,0.03);}}
  .score{{font-family:Consolas,monospace;color:#00d4ff;font-weight:600;}}
  .badge{{background:rgba(124,58,237,0.2);color:#7c3aed;padding:2px 8px;border-radius:10px;font-size:10px;text-transform:uppercase;}}
  .sub{{color:#666;font-size:11px;}}
</style></head><body><div class="container">
  <h1>recall: "{q}"</h1>
  <div class="lead">Searched auto-memory (TF-IDF) + Obsidian vault grep · {r['generated_at']}</div>

  <div class="panel">
    <h2>Auto-Memory <span style="color:#666;font-size:11px;">({len(r['auto_memory'])} hits)</span></h2>
    <table><thead><tr><th>Score</th><th>File</th><th>Type</th><th>Snippet</th></tr></thead>
    <tbody>{auto_rows}</tbody></table>
  </div>

  <div class="panel">
    <h2>Obsidian Vault <span style="color:#666;font-size:11px;">({len(r['obsidian'])} hits)</span></h2>
    <table><thead><tr><th>Score</th><th>Path</th><th>Snippet</th></tr></thead>
    <tbody>{obs_rows}</tbody></table>
  </div>

  <div class="panel">
    <h2>Observer <span style="color:#666;font-size:11px;">({len(r.get('observer') or [])} hits)</span></h2>
    <table><thead><tr><th>When</th><th>Tool</th><th>Files</th><th>Excerpt</th></tr></thead>
    <tbody>{observer_rows}</tbody></table>
  </div>
</div></body></html>"""


def render_text_deep(r: dict) -> str:
    """v13 Phase 10: --deep mode prints full body of each top auto-memory hit."""
    lines = [f"== recall (DEEP): '{r['query']}' ==\n"]
    lines.append("--- Auto-Memory (full content) ---")
    if not r["auto_memory"]:
        lines.append("  (no matches)")
    for h in r["auto_memory"][:5]:
        lines.append(f"\n[{h['score']:.2f}] {h['file']}  ({h['type'] or '-'})")
        lines.append(f"  {h['name']}")
        try:
            from memory_engine import MEMORY_DIR
            p = MEMORY_DIR / h["file"]
            if p.exists():
                body = p.read_text(encoding="utf-8", errors="ignore")
                lines.append("  " + body[:1800].replace("\n", "\n  "))
                if len(body) > 1800:
                    lines.append(f"  ... [{len(body) - 1800} more chars]")
        except Exception:
            pass
    lines.append("\n--- Obsidian Vault ---")
    for h in r["obsidian"][:3]:
        lines.append(f"  [{h['score']:>3}] {h['vault']}/{h['path']}")
        if h.get("snippet"):
            lines.append(f"          -> {h['snippet']}")
    return "\n".join(lines)


def main():
    if len(sys.argv) < 2:
        print("Usage: recall.py <query> [--json|--html|--deep]")
        sys.exit(1)
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    query = " ".join(args)
    include_observer = "--no-observer" not in flags
    observer_only = "--observer" in flags
    r = run_recall(query, include_observer=include_observer, observer_only=observer_only)
    if "--json" in flags:
        print(json.dumps(r, indent=2))
    elif "--html" in flags:
        OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
        OUT_HTML.write_text(render_html(r), encoding="utf-8")
        print(f"Wrote {OUT_HTML}")
        if "--no-open" not in flags and os.name == "nt":
            os.system(f'start "" "{OUT_HTML}"')
    elif "--deep" in flags:
        print(render_text_deep(r))
    else:
        print(render_text(r))

if __name__ == "__main__":
    main()
