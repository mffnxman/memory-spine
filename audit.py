"""
audit.py — memory health audit. Outputs glassmorphism HTML triage.

Detects:
  - Stale (>60d, low access)
  - Untyped (missing frontmatter)
  - Orphaned (in index but file missing)
  - Unindexed (file but not in MEMORY.md)
  - Expired (past `expires:` date)
  - Duplicates (cosine similarity >= DUP_THRESHOLD; cross-session observer pairs excluded)
  - Outdated cross-references

Usage:
  python audit.py            # writes HTML to ~/Downloads/, opens it
  python audit.py --json     # outputs raw JSON only
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from memory_engine import (
    list_memories,
    find_orphans,
    detect_references,
    detect_duplicates,
    access_stats,
    cache_references,
    MEMORY_DIR,
)

OUT_HTML = Path.home() / "Downloads" / "memory_audit.html"
STALE_DAYS = 60
DUP_THRESHOLD = 0.70  # raised from 0.55 — prevents false-positive vocab overlap

# Severity-weighted scoring. Health score = 100 - sum(count[k] * WEIGHTS[k]),
# with each category's penalty capped so one noisy detector can't zero the
# score on its own (46 template-similar dupes once cost 138 points alone).
CATEGORY_CAP = 30
WEIGHTS = {
    "missing": 10,  # broken link, real damage to the index
    "expired": 5,  # past expires: date — real but easy to fix
    "duplicates": 3,  # raised threshold means these are real candidates
    "untyped": 2,  # cosmetic, breaks sorting/filtering
    "unindexed": 1,  # file exists, just not in MEMORY.md — minor
    # `no_description` removed from scoring — it overlapped 1:1 with `untyped`
}


def run_audit() -> dict:
    mems = list_memories()
    refs = detect_references(mems)
    cache_references(refs)  # update sidecar
    incoming = Counter()
    for src, dsts in refs.items():
        for dst in dsts:
            incoming[dst] += 1
    access = access_stats()
    orph = find_orphans()
    dups = detect_duplicates(mems, threshold=DUP_THRESHOLD)

    by_type = Counter(m.type or "untyped" for m in mems)

    issues = {
        "expired": [],
        "stale": [],
        "untyped": [],
        "missing": orph["missing"],
        "unindexed": orph["unindexed"],
        "duplicates": [],
        "no_description": [],
    }

    for m in mems:
        a = access.get(m.filename, {})
        last = a.get("last", 0)
        last_str = (
            datetime.fromtimestamp(last).strftime("%Y-%m-%d") if last else "never"
        )
        rec = {
            "file": m.filename,
            "name": m.name,
            "type": m.type or "—",
            "age_days": m.age_days,
            "last_access": last_str,
            "access_30d": a.get("d30", 0),
            "incoming_refs": incoming.get(m.filename, 0),
            "expires": m.expires or "—",
        }
        if m.is_expired:
            issues["expired"].append(rec)
        if m.age_days > STALE_DAYS and a.get("d30", 0) == 0:
            issues["stale"].append(rec)
        if not m.type:
            issues["untyped"].append(rec)
        if not m.description:
            issues["no_description"].append(rec)

    for a, b, sim in dups:
        issues["duplicates"].append(
            {
                "a": a.filename,
                "a_name": a.name,
                "b": b.filename,
                "b_name": b.name,
                "similarity": round(sim, 3),
            }
        )

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "memory_dir": str(MEMORY_DIR),
        "total_memories": len(mems),
        "by_type": dict(by_type),
        "total_refs": sum(len(v) for v in refs.values()),
        "top_hubs": [{"file": fn, "incoming": n} for fn, n in incoming.most_common(8)],
        "issues": issues,
        "issue_counts": {k: len(v) for k, v in issues.items()},
    }


def render_html(audit: dict) -> str:
    ic = audit["issue_counts"]
    # Weighted health score — missing/expired hurt more than untyped/unindexed
    penalty = sum(min(ic.get(k, 0) * w, CATEGORY_CAP) for k, w in WEIGHTS.items())
    health_score = max(0, 100 - penalty)
    health_color = (
        "#10b981"
        if health_score >= 85
        else "#f59e0b" if health_score >= 70 else "#ef4444"
    )

    def issue_table(title: str, key: str, cols: list[tuple[str, str]]) -> str:
        items = audit["issues"][key]
        if not items:
            return ""
        if cols:
            rows = "\n".join(
                "<tr>"
                + "".join(f"<td>{item.get(c[1], '')}</td>" for c in cols)
                + "</tr>"
                for item in items
            )
            head = "".join(f"<th>{c[0]}</th>" for c in cols)
        else:
            # Plain-string issue lists (unindexed/missing are bare filenames):
            # one cell per row. These used to render literally blank <tr></tr>
            # rows, hiding the unindexed list from the report — which is how 5
            # invisible memories stayed invisible (BIGBUFF 2.0 D1-02).
            rows = "\n".join(f"<tr><td>{item}</td></tr>" for item in items)
            head = "<th>File</th>"
        return f"""
        <div class="glass-panel">
          <h2>{title} <span class="count-badge">{len(items)}</span></h2>
          <table><thead><tr>{head}</tr></thead>
          <tbody>{rows}</tbody></table>
        </div>"""

    if audit["issues"]["duplicates"]:
        dup_rows = "\n".join(
            f"<tr><td>{d['a']}</td><td>{d['a_name']}</td><td>{d['b']}</td><td>{d['b_name']}</td><td><span class='badge badge-yellow'>{d['similarity']}</span></td></tr>"
            for d in audit["issues"]["duplicates"]
        )
        dup_html = f"""
        <div class="glass-panel">
          <h2>Duplicates / Near-Dupes <span class="count-badge">{len(audit["issues"]["duplicates"])}</span></h2>
          <p style="color:#888;font-size:13px;margin-bottom:12px;">Cosine similarity ≥ {DUP_THRESHOLD} — review for merging.</p>
          <table><thead><tr><th>File A</th><th>Name A</th><th>File B</th><th>Name B</th><th>Similarity</th></tr></thead>
          <tbody>{dup_rows}</tbody></table>
        </div>"""
    else:
        dup_html = ""

    hubs_rows = (
        "\n".join(
            f"<tr><td>{h['file']}</td><td><span class='badge badge-blue'>{h['incoming']}</span></td></tr>"
            for h in audit["top_hubs"]
        )
        or "<tr><td colspan='2' style='color:#666;text-align:center;'>No cross-references yet.</td></tr>"
    )

    type_chips = " ".join(
        f'<span class="badge badge-purple">{t}: {n}</span>'
        for t, n in audit["by_type"].items()
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Memory Health Audit</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: linear-gradient(135deg, #0a0a1a 0%, #1a1a2e 50%, #16213e 100%);
    color: #e0e0e0; min-height: 100vh; padding: 24px;
  }}
  .container {{ max-width: 1400px; margin: 0 auto; }}
  h1 {{
    font-size: 28px; font-weight: 700;
    background: linear-gradient(135deg, #00d4ff, #7c3aed);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    margin-bottom: 8px;
  }}
  .subtitle {{ color: #888; font-size: 14px; margin-bottom: 24px; }}
  .stats-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 14px; margin-bottom: 24px;
  }}
  .stat-card {{
    background: rgba(255,255,255,0.05);
    backdrop-filter: blur(10px);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 16px; padding: 18px; text-align: center;
  }}
  .stat-card .value {{ font-size: 30px; font-weight: 700; color: #00d4ff; }}
  .stat-card .label {{ font-size: 11px; color: #888; margin-top: 6px; text-transform: uppercase; letter-spacing: 1px; }}
  .stat-card.health .value {{ color: {health_color}; font-size: 42px; }}
  .glass-panel {{
    background: rgba(255,255,255,0.05);
    backdrop-filter: blur(10px);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 16px; padding: 24px; margin-bottom: 20px;
  }}
  .glass-panel h2 {{
    font-size: 16px; color: #00d4ff;
    text-transform: uppercase; letter-spacing: 1.5px;
    margin-bottom: 14px; font-weight: 700;
    display: flex; align-items: center; gap: 10px;
  }}
  .count-badge {{
    background: rgba(239,68,68,0.2); color: #ef4444;
    padding: 2px 10px; border-radius: 12px; font-size: 12px;
  }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ text-align: left; padding: 10px; border-bottom: 2px solid rgba(255,255,255,0.1); color: #00d4ff; font-size: 12px; text-transform: uppercase; letter-spacing: 1px; }}
  td {{ padding: 9px 10px; border-bottom: 1px solid rgba(255,255,255,0.05); font-size: 13px; }}
  tr:hover {{ background: rgba(255,255,255,0.03); }}
  .badge {{ display: inline-block; padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: 600; }}
  .badge-green {{ background: rgba(16,185,129,0.2); color: #10b981; }}
  .badge-yellow {{ background: rgba(245,158,11,0.2); color: #f59e0b; }}
  .badge-red {{ background: rgba(239,68,68,0.2); color: #ef4444; }}
  .badge-blue {{ background: rgba(0,212,255,0.2); color: #00d4ff; }}
  .badge-purple {{ background: rgba(124,58,237,0.2); color: #7c3aed; }}
  .chips {{ display: flex; gap: 8px; flex-wrap: wrap; }}
  .footer {{ text-align: center; color: #555; font-size: 12px; margin-top: 24px; }}
</style>
</head>
<body><div class="container">
  <h1>Memory Health Audit</h1>
  <div class="subtitle">{audit['memory_dir']}</div>

  <div class="stats-grid">
    <div class="stat-card health"><div class="value">{health_score}</div><div class="label">Health Score</div></div>
    <div class="stat-card"><div class="value">{audit['total_memories']}</div><div class="label">Memories</div></div>
    <div class="stat-card"><div class="value">{audit['total_refs']}</div><div class="label">Cross-Refs</div></div>
    <div class="stat-card"><div class="value">{ic['stale']}</div><div class="label">Stale</div></div>
    <div class="stat-card"><div class="value">{ic['untyped']}</div><div class="label">Untyped</div></div>
    <div class="stat-card"><div class="value">{ic['unindexed']}</div><div class="label">Unindexed</div></div>
    <div class="stat-card"><div class="value">{ic['expired']}</div><div class="label">Expired</div></div>
    <div class="stat-card"><div class="value">{ic['duplicates']}</div><div class="label">Dup Pairs</div></div>
  </div>

  <div class="glass-panel">
    <h2>Memory Composition</h2>
    <div class="chips">{type_chips}</div>
  </div>

  <div class="glass-panel">
    <h2>Top Knowledge Hubs</h2>
    <p style="color:#888;font-size:13px;margin-bottom:12px;">Files most referenced by other memories — these are your "spine" concepts.</p>
    <table><thead><tr><th>File</th><th>Incoming Refs</th></tr></thead>
    <tbody>{hubs_rows}</tbody></table>
  </div>

  {issue_table("Expired Memories", "expired", [("File","file"),("Name","name"),("Expired","expires")])}
  {issue_table("Stale (>60d, no recent access)", "stale", [("File","file"),("Type","type"),("Age (days)","age_days"),("Last Access","last_access")])}
  {issue_table("Untyped (missing frontmatter type)", "untyped", [("File","file"),("Name","name"),("Age","age_days")])}
  {issue_table("Unindexed (file exists but not in MEMORY.md)", "unindexed", [])}
  {issue_table("Missing (in MEMORY.md but file deleted)", "missing", [])}
  {dup_html}

  <div class="footer">Generated {audit['generated_at']} · memory_engine v1</div>
</div></body></html>"""


def main():
    if "--json" in sys.argv:
        print(json.dumps(run_audit(), indent=2))
        return
    audit = run_audit()
    # Special handling for plain string lists (unindexed, missing)
    audit["issues"]["unindexed"] = [
        {"unindexed": fn} for fn in audit["issues"]["unindexed"]
    ]
    audit["issues"]["missing"] = [{"missing": fn} for fn in audit["issues"]["missing"]]
    html = render_html(audit)
    OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text(html, encoding="utf-8")
    print(f"Audit complete: {OUT_HTML}")
    print(f"Issues: {audit['issue_counts']}")
    if "--no-open" not in sys.argv and os.name == "nt":
        os.system(f'start "" "{OUT_HTML}"')


if __name__ == "__main__":
    main()
