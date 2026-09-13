"""
obsidian_sync.py v2 — smart drift detection between auto-memory and Obsidian brain.

v1 problem: it flagged category-mismatch as drift. Auto-memory tracks
people/projects/feedback; Obsidian brain tracks stack/tools. Listing memory
files that don't appear as brain slugs is noise, not signal.

v2 detects real drift signals with severity levels:

  CRITICAL — Continuity stack invisible in brain
              (the mobile reader can't tell the system exists)
  WARN     — High-weight "spine" memory not represented in brain
              (foundational context missing from mobile mirror)
  WARN     — Stack snapshot stale relative to newest memory edits
              (brain is older than the most recent desktop changes)
  INFO     — Shared topic with date cross-references
              (verify both layers agree on a date)

Suppressed (no longer reported as drift):
  - Memory files without a matching brain slug (orthogonal layer, expected)
  - Brain slugs without a memory file (tool/skill names, expected)

Usage:
  python obsidian_sync.py             # console summary
  python obsidian_sync.py --html      # write & open HTML report
  python obsidian_sync.py --json      # machine-readable
  python obsidian_sync.py --verbose   # include suppressed category data
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import paths as _paths  # noqa: E402
from memory_engine import list_memories, MEMORY_DIR

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

OBSIDIAN_BRAIN_DIRS = (
    [_paths.OBSIDIAN_VAULT / "brain"] if _paths.OBSIDIAN_VAULT else []
)
OUT_HTML = _paths.OUTPUT_DIR / "obsidian_sync_report.html"

DATE_RE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")

# The build cadence ships a new capability tier roughly every 2-3 weeks, so a
# snapshot lagging the newest spine memory by more than this has likely missed
# one. Tuned above the cadence so routine spine edits don't cry wolf.
SNAPSHOT_STALE_DAYS = 21
SLUG_RE = re.compile(r"`([a-z][a-z0-9_-]+)`")

CONTINUITY_MARKERS = [
    r"memory[\s_-]?engine",
    r"/recall\b",
    r"/whoami\b",
    r"/memory-audit\b",
    r"/memory-sync\b",
    r"\bself_continuity",
    r"continuity[\s-]stack",
    r"epilogue[\s-]ritual",
    r"boot[\s-]ritual",
    r"\bself\b.*\btype\b",
]


def find_brain_files() -> list[Path]:
    seen = set()
    out = []
    for d in OBSIDIAN_BRAIN_DIRS:
        if d.exists():
            for f in sorted(d.glob("*.md")):
                key = str(f.resolve()).lower()
                if key not in seen:
                    seen.add(key)
                    out.append(f)
    return out


def read_brain_text(brain_files: list[Path]) -> tuple[str, dict]:
    blob_parts = []
    metadata = {}
    for f in brain_files:
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        blob_parts.append(text)
        metadata[f.name] = {
            "path": str(f),
            "size": len(text),
            "mtime": f.stat().st_mtime,
            "mtime_iso": datetime.fromtimestamp(f.stat().st_mtime).isoformat(
                timespec="seconds"
            ),
        }
    return "\n".join(blob_parts).lower(), metadata


def check_continuity_visibility(brain_blob: str) -> dict:
    hits = []
    for pattern in CONTINUITY_MARKERS:
        if re.search(pattern, brain_blob, re.IGNORECASE):
            hits.append(pattern)
    visible = len(hits) >= 3
    return {
        "severity": "OK" if visible else "CRITICAL",
        "title": "Continuity stack visibility in brain",
        "visible": visible,
        "markers_found": hits,
        "markers_checked": CONTINUITY_MARKERS,
        "recommendation": (
            None
            if visible
            else (
                "Add a 'Memory & Continuity Stack' section to "
                "$OBSIDIAN_VAULT/brain/stack-snapshot.md and brain-full.md "
                "so mobile-Claude knows the system exists."
            )
        ),
    }


def check_high_weight_coverage(mems, brain_blob: str) -> dict:
    high_weight = [m for m in mems if getattr(m, "weight", None) == "high"]
    missing = []
    for m in high_weight:
        stem = m.path.stem.lower()
        variants = {stem, stem.replace("_", "-"), stem.replace("_", " ")}
        if not any(v in brain_blob for v in variants):
            missing.append({"file": m.filename, "name": m.name, "type": m.type})
    return {
        "severity": "OK" if not missing else "WARN",
        "title": "High-weight 'spine' memories represented in brain",
        "high_weight_total": len(high_weight),
        "missing_count": len(missing),
        "missing": missing,
        "recommendation": (
            None
            if not missing
            else (
                f"{len(missing)} foundational memories aren't mentioned in the brain — "
                "consider adding them to the Memory & Continuity section so mobile "
                "context inherits the spine."
            )
        ),
    }


def check_brain_freshness(mems, brain_metadata: dict) -> dict:
    if not mems or not brain_metadata:
        return {"severity": "OK", "title": "Brain freshness", "skipped": True}

    newest_mem = max((m.path.stat().st_mtime for m in mems), default=0)
    newest_brain = max((meta["mtime"] for meta in brain_metadata.values()), default=0)

    delta_hours = (newest_mem - newest_brain) / 3600 if newest_brain else 0
    stale = delta_hours > 24

    return {
        "severity": "WARN" if stale else "OK",
        "title": "Brain freshness vs newest memory edit",
        "newest_memory_mtime": datetime.fromtimestamp(newest_mem).isoformat(
            timespec="seconds"
        ),
        "newest_brain_mtime": datetime.fromtimestamp(newest_brain).isoformat(
            timespec="seconds"
        ),
        "delta_hours": round(delta_hours, 1),
        "recommendation": (
            None
            if not stale
            else (
                "Brain is more than 24h behind your newest memory edit. "
                "Run the brain regen prompt on desktop, then 'push brain to drive'."
            )
        ),
    }


def _snapshot_date(brain_metadata: dict) -> tuple[str | None, str | None]:
    """Return (date, version) from stack-snapshot.md's frontmatter, or (None, None)."""
    for f_name, meta in brain_metadata.items():
        if "stack-snapshot" not in f_name:
            continue
        try:
            head = Path(meta["path"]).read_text(encoding="utf-8", errors="ignore")[:600]
        except Exception:
            return (None, None)
        d = re.search(r"^date:\s*(20\d{2}-\d{2}-\d{2})", head, re.MULTILINE)
        v = re.search(r"^version:\s*(\S+)", head, re.MULTILINE)
        return (d.group(1) if d else None, v.group(1) if v else None)
    return (None, None)


def check_snapshot_semantic_freshness(mems, brain_metadata: dict) -> dict:
    """Content-level freshness — NOT mtime.

    check_brain_freshness compares mtimes, but brain-full.md is auto-regenerated
    twice daily by the scheduler, so its mtime is always fresh even when the
    human-authored stack-snapshot.md is months stale (it just wraps the frozen
    snapshot). This check parses the snapshot's own `date:` frontmatter and
    compares it against the newest high-weight 'spine' memory — the real signal
    that a new capability tier shipped but the mobile briefing never caught up.
    """
    title = "Snapshot content freshness vs newest spine memory"
    snap_date, snap_version = _snapshot_date(brain_metadata)
    if not snap_date:
        return {
            "severity": "INFO",
            "title": title,
            "note": "stack-snapshot.md not found or has no date: frontmatter",
        }

    # Newest high-weight memory: prefer event_date, fall back to file mtime date.
    def _mem_date(m) -> str:
        if getattr(m, "event_date", None):
            return m.event_date
        return datetime.fromtimestamp(m.path.stat().st_mtime).strftime("%Y-%m-%d")

    high_weight = [m for m in mems if getattr(m, "weight", None) == "high"]
    if not high_weight:
        return {"severity": "OK", "title": title, "snapshot_date": snap_date}

    dated = sorted(((_mem_date(m), m.filename) for m in high_weight), reverse=True)
    newest_date, newest_file = dated[0]
    lag_days = (
        datetime.strptime(newest_date, "%Y-%m-%d")
        - datetime.strptime(snap_date, "%Y-%m-%d")
    ).days
    stale = lag_days > SNAPSHOT_STALE_DAYS

    return {
        "severity": "WARN" if stale else "OK",
        "title": title,
        "snapshot_date": snap_date,
        "snapshot_version": snap_version,
        "newest_spine_memory": newest_file,
        "newest_spine_date": newest_date,
        "lag_days": lag_days,
        "recommendation": (
            None
            if not stale
            else (
                f"stack-snapshot.md is dated {snap_date} (v{snap_version}) but the newest "
                f"high-weight memory ({newest_file}) is dated {newest_date} — {lag_days} days "
                f"newer. The mobile briefing has likely missed a capability tier. Regenerate "
                f"stack-snapshot.md's Memory & Continuity section, bump date+version, and let "
                f"the scheduler propagate it to brain-full.md / Drive."
            )
        ),
    }


def check_date_cross_refs(mems, brain_metadata: dict) -> dict:
    refs = []
    for f_name, meta in brain_metadata.items():
        try:
            text = Path(meta["path"]).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for date_match in DATE_RE.finditer(text):
            line_start = text.rfind("\n", 0, date_match.start()) + 1
            line_end = text.find("\n", date_match.end())
            snippet = text[line_start:line_end].strip()[:160]
            snippet_lower = snippet.lower()
            for m in mems:
                stem = m.path.stem.lower()
                if stem in snippet_lower or stem.replace("_", "-") in snippet_lower:
                    refs.append(
                        {
                            "memory": m.filename,
                            "brain_file": f_name,
                            "brain_date": date_match.group(1),
                            "snippet": snippet,
                        }
                    )
                    break
    return {
        "severity": "INFO",
        "title": "Date cross-references",
        "count": len(refs),
        "refs": refs[:50],
    }


def collect_suppressed(mems, brain_blob: str) -> dict:
    mem_only = []
    for m in mems:
        stem = m.path.stem.lower()
        if stem.replace("_", "-") not in brain_blob and stem not in brain_blob:
            mem_only.append({"file": m.filename, "name": m.name, "type": m.type})

    brain_slugs = set(SLUG_RE.findall(brain_blob))
    mem_stems = {m.path.stem.lower() for m in mems}
    mem_stems |= {s.replace("_", "-") for s in mem_stems}
    brain_only = sorted(s for s in brain_slugs if s not in mem_stems)

    return {
        "title": "Suppressed (category mismatch — not real drift)",
        "memory_only_count": len(mem_only),
        "brain_only_count": len(brain_only),
        "memory_only": mem_only,
        "brain_only": brain_only,
        "note": (
            "Auto-memory tracks people/projects/feedback; brain tracks tools/skills. "
            "These lists are large by design — they reflect orthogonal layers, not drift."
        ),
    }


def run_all_checks(verbose: bool = False) -> dict:
    brain_files = find_brain_files()
    if not brain_files:
        return {
            "error": "No Obsidian brain files found.",
            "tried": [str(d) for d in OBSIDIAN_BRAIN_DIRS],
        }

    mems = list_memories()
    brain_blob, brain_metadata = read_brain_text(brain_files)

    checks = [
        check_continuity_visibility(brain_blob),
        check_high_weight_coverage(mems, brain_blob),
        check_brain_freshness(mems, brain_metadata),
        check_snapshot_semantic_freshness(mems, brain_metadata),
        check_date_cross_refs(mems, brain_metadata),
    ]

    severity_counts = {"CRITICAL": 0, "WARN": 0, "INFO": 0, "OK": 0}
    for c in checks:
        severity_counts[c["severity"]] = severity_counts.get(c["severity"], 0) + 1

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "auto_memory_count": len(mems),
        "brain_files_indexed": [f.name for f in brain_files],
        "brain_metadata": {
            k: {kk: vv for kk, vv in v.items() if kk != "path"}
            for k, v in brain_metadata.items()
        },
        "severity_counts": severity_counts,
        "checks": checks,
    }

    if verbose:
        report["suppressed"] = collect_suppressed(mems, brain_blob)

    return report


def render_console(report: dict) -> str:
    if "error" in report:
        return f"ERROR: {report['error']}\n  Tried: {', '.join(report['tried'])}"

    lines = []
    lines.append("Memory <-> Obsidian Brain Sync (v2)")
    lines.append(f"Generated: {report['generated_at']}")
    lines.append(
        f"Memory files: {report['auto_memory_count']}  |  "
        f"Brain files: {len(report['brain_files_indexed'])}"
    )
    lines.append("")
    sc = report["severity_counts"]
    lines.append(
        f"  CRITICAL: {sc.get('CRITICAL', 0)}    "
        f"WARN: {sc.get('WARN', 0)}    "
        f"INFO: {sc.get('INFO', 0)}    "
        f"OK: {sc.get('OK', 0)}"
    )
    lines.append("")

    for c in report["checks"]:
        marker = {"CRITICAL": "[!!]", "WARN": "[~]", "INFO": "[i]", "OK": "[ok]"}[
            c["severity"]
        ]
        lines.append(f"{marker} {c['title']}")
        if c["severity"] == "CRITICAL":
            lines.append(
                f"     Markers found: {len(c.get('markers_found', []))}/{len(c.get('markers_checked', []))}"
            )
            lines.append(f"     -> {c['recommendation']}")
        elif c["severity"] == "WARN" and "missing" in c:
            lines.append(
                f"     {c['missing_count']}/{c['high_weight_total']} high-weight memories missing"
            )
            for m in c["missing"][:5]:
                lines.append(f"       - {m['file']}")
            if c.get("recommendation"):
                lines.append(f"     -> {c['recommendation']}")
        elif c["severity"] == "WARN" and "delta_hours" in c:
            lines.append(f"     Brain {c['delta_hours']}h behind newest memory edit")
            lines.append(f"     -> {c['recommendation']}")
        elif c["severity"] == "INFO":
            lines.append(f"     {c.get('count', 0)} cross-references")

    return "\n".join(lines)


def render_html(report: dict) -> str:
    if "error" in report:
        return f"<html><body><h1>Error</h1><p>{report['error']}</p></body></html>"

    sc = report["severity_counts"]

    def severity_badge(sev):
        colors = {
            "CRITICAL": ("#ef4444", "rgba(239,68,68,0.15)"),
            "WARN": ("#f59e0b", "rgba(245,158,11,0.15)"),
            "INFO": ("#00d4ff", "rgba(0,212,255,0.15)"),
            "OK": ("#10b981", "rgba(16,185,129,0.15)"),
        }
        fg, bg = colors[sev]
        return f"<span style='display:inline-block;padding:3px 10px;border-radius:10px;background:{bg};color:{fg};font-size:11px;font-weight:700;letter-spacing:1px;'>{sev}</span>"

    check_panels = []
    for c in report["checks"]:
        body = ""
        if c["severity"] == "CRITICAL":
            found = c.get("markers_found", [])
            checked = c.get("markers_checked", [])
            body = f"""
            <p>Markers found: <strong>{len(found)}/{len(checked)}</strong></p>
            <p style="color:#aaa;font-size:13px;">Found: {", ".join("<code>"+f+"</code>" for f in found) or "<em>none</em>"}</p>
            <div style="margin-top:12px;padding:12px;background:rgba(239,68,68,0.08);border-left:3px solid #ef4444;border-radius:6px;">
              <strong>Action:</strong> {c['recommendation']}
            </div>"""
        elif c["severity"] == "WARN" and "missing" in c:
            rows = "".join(
                f"<tr><td>{m['file']}</td><td>{m['name']}</td><td><span class='badge badge-purple'>{m['type']}</span></td></tr>"
                for m in c["missing"]
            )
            body = f"""
            <p>{c['missing_count']} of {c['high_weight_total']} high-weight memories not mentioned in brain.</p>
            <table>
              <thead><tr><th>File</th><th>Name</th><th>Type</th></tr></thead>
              <tbody>{rows}</tbody>
            </table>
            <div style="margin-top:12px;padding:12px;background:rgba(245,158,11,0.08);border-left:3px solid #f59e0b;border-radius:6px;">
              <strong>Action:</strong> {c['recommendation']}
            </div>"""
        elif c["severity"] == "WARN" and "delta_hours" in c:
            body = f"""
            <p>Newest memory edit: <code>{c['newest_memory_mtime']}</code></p>
            <p>Newest brain edit:  <code>{c['newest_brain_mtime']}</code></p>
            <p>Delta: <strong>{c['delta_hours']}h</strong></p>
            <div style="margin-top:12px;padding:12px;background:rgba(245,158,11,0.08);border-left:3px solid #f59e0b;border-radius:6px;">
              <strong>Action:</strong> {c['recommendation']}
            </div>"""
        elif c["severity"] == "INFO":
            refs = c.get("refs", [])
            if refs:
                rows = "".join(
                    f"<tr><td>{r['memory']}</td><td>{r['brain_file']}</td><td><span class='badge badge-blue'>{r['brain_date']}</span></td><td><code style='font-size:11px;color:#aaa;'>{r['snippet'][:120]}</code></td></tr>"
                    for r in refs
                )
                body = f"""
                <p>{c.get('count', 0)} cross-references between memory and brain. Verify both layers agree on dates.</p>
                <table>
                  <thead><tr><th>Memory</th><th>Brain File</th><th>Date</th><th>Snippet</th></tr></thead>
                  <tbody>{rows}</tbody>
                </table>"""
            else:
                body = "<p style='color:#888;'><em>No date cross-references found.</em></p>"
        elif c["severity"] == "OK":
            if "markers_found" in c:
                body = f"""
                <p style="color:#10b981;">All good — continuity stack visible in brain ({len(c['markers_found'])}/{len(c['markers_checked'])} markers present).</p>
                <p style="color:#aaa;font-size:12px;">Found: {", ".join("<code>"+f+"</code>" for f in c['markers_found'])}</p>"""
            elif "high_weight_total" in c:
                body = f"<p style='color:#10b981;'>All {c['high_weight_total']} high-weight memories represented in brain.</p>"
            elif "delta_hours" in c:
                body = f"<p style='color:#10b981;'>Brain in sync (delta {c['delta_hours']}h, within 24h threshold).</p>"
            else:
                body = "<p style='color:#10b981;'>Check passed.</p>"

        check_panels.append(f"""
        <div class="glass-panel">
          <h2 style="display:flex;align-items:center;gap:12px;">{severity_badge(c['severity'])} {c['title']}</h2>
          {body}
        </div>""")

    indexed = "<br>".join(report["brain_files_indexed"])

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Memory <-> Obsidian Sync (v2)</title><style>
  *{{margin:0;padding:0;box-sizing:border-box;}}
  body{{font-family:'Segoe UI',sans-serif;background:linear-gradient(135deg,#0a0a1a,#1a1a2e 50%,#16213e);color:#e0e0e0;min-height:100vh;padding:24px;}}
  .container{{max-width:1300px;margin:0 auto;}}
  h1{{font-size:30px;background:linear-gradient(135deg,#00d4ff,#7c3aed);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:6px;}}
  .lead{{color:#888;margin-bottom:22px;font-size:14px;}}
  .stats-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin-bottom:22px;}}
  .stat-card{{background:rgba(255,255,255,0.05);backdrop-filter:blur(10px);border:1px solid rgba(255,255,255,0.1);border-radius:14px;padding:18px;text-align:center;}}
  .stat-card .v{{font-size:34px;font-weight:700;}}
  .stat-card .l{{font-size:11px;color:#888;margin-top:4px;text-transform:uppercase;letter-spacing:1px;}}
  .stat-critical .v{{color:#ef4444;}} .stat-warn .v{{color:#f59e0b;}}
  .stat-info .v{{color:#00d4ff;}}     .stat-ok .v{{color:#10b981;}}
  .glass-panel{{background:rgba(255,255,255,0.05);backdrop-filter:blur(10px);border:1px solid rgba(255,255,255,0.1);border-radius:16px;padding:22px;margin-bottom:18px;}}
  .glass-panel h2{{font-size:16px;color:#e0e0e0;margin-bottom:14px;}}
  table{{width:100%;border-collapse:collapse;margin-top:10px;}}
  th{{text-align:left;padding:10px;border-bottom:2px solid rgba(255,255,255,0.1);color:#00d4ff;font-size:11px;text-transform:uppercase;letter-spacing:1px;}}
  td{{padding:9px 10px;border-bottom:1px solid rgba(255,255,255,0.05);font-size:13px;vertical-align:top;}}
  tr:hover{{background:rgba(255,255,255,0.03);}}
  .badge{{display:inline-block;padding:2px 9px;border-radius:10px;font-size:11px;font-weight:600;}}
  .badge-purple{{background:rgba(124,58,237,0.2);color:#7c3aed;}}
  .badge-blue{{background:rgba(0,212,255,0.2);color:#00d4ff;}}
  code{{background:rgba(0,0,0,0.3);padding:2px 6px;border-radius:4px;font-size:12px;color:#00d4ff;font-family:Consolas,monospace;}}
</style></head><body><div class="container">
  <h1>Memory &lt;-&gt; Obsidian Sync — v2</h1>
  <div class="lead">Severity-based drift detection. Category mismatch (memory vs brain by topic) is suppressed — those layers are orthogonal by design.</div>

  <div class="stats-grid">
    <div class="stat-card stat-critical"><div class="v">{sc.get('CRITICAL', 0)}</div><div class="l">Critical</div></div>
    <div class="stat-card stat-warn"><div class="v">{sc.get('WARN', 0)}</div><div class="l">Warn</div></div>
    <div class="stat-card stat-info"><div class="v">{sc.get('INFO', 0)}</div><div class="l">Info</div></div>
    <div class="stat-card stat-ok"><div class="v">{sc.get('OK', 0)}</div><div class="l">OK</div></div>
    <div class="stat-card"><div class="v" style="color:#aaa;">{report['auto_memory_count']}</div><div class="l">Memory files</div></div>
    <div class="stat-card"><div class="v" style="color:#aaa;">{len(report['brain_files_indexed'])}</div><div class="l">Brain files</div></div>
  </div>

  <div class="glass-panel">
    <h2>Indexed Brain Files</h2>
    <div style="font-family:Consolas,monospace;font-size:13px;color:#aaa;">{indexed}</div>
  </div>

  {"".join(check_panels)}

  <div style="text-align:center;color:#555;font-size:12px;margin-top:22px;">Generated {report['generated_at']} - v2</div>
</div></body></html>"""


def open_in_browser(path: Path) -> None:
    """Cross-platform open. On Windows uses os.startfile (no shell)."""
    if sys.platform == "win32":
        import os as _os

        _os.startfile(str(path))  # noqa: S606 — static path, no user input
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


def main():
    verbose = "--verbose" in sys.argv
    report = run_all_checks(verbose=verbose)

    if "--json" in sys.argv:
        print(json.dumps(report, indent=2, default=str))
        return

    if "--html" in sys.argv:
        OUT_HTML.write_text(render_html(report), encoding="utf-8")
        print(f"Wrote {OUT_HTML}")
        if "--no-open" not in sys.argv:
            try:
                open_in_browser(OUT_HTML)
            except Exception as e:
                print(f"(could not auto-open: {e})")
        return

    print(render_console(report))


if __name__ == "__main__":
    main()
