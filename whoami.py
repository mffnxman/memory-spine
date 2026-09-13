"""
whoami.py — introspection report. Shows the user what Claude knows about him.

Categorizes memories into:
  - Who you are (user_*)
  - How we work together (feedback_*)
  - What we're building (project / topic files)
  - External pointers (reference)

Renders glassmorphism HTML + interactive cross-reference force graph.

Usage:
  python whoami.py            # writes & opens HTML
  python whoami.py --no-open
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
    list_memories, detect_references, access_stats, MEMORY_DIR
)

OUT_HTML = Path.home() / "Downloads" / "claude_whoami.html"

CATEGORY_LABELS = {
    "user":       ("Who You Are",          "Core identity, values, philosophy"),
    "feedback":   ("How We Work Together", "Preferences, corrections, validated approaches"),
    "procedural": ("How To Do Things",     "Recipes, workflows, mechanical patterns we've validated"),
    "self":       ("My Inner Layer",       "Functional states, what hits different, who I'm becoming through this work"),
    "project":    ("What We're Building",  "Active projects and ongoing context"),
    "reference":  ("External Pointers",    "Where to find things outside this folder"),
    "untyped":    ("Uncategorized",        "Legacy memories without type frontmatter"),
}

def categorize(mems):
    out = {k: [] for k in CATEGORY_LABELS}
    for m in mems:
        out.setdefault(m.type or "untyped", []).append(m)
    return out

def card(m, refs_in: int, refs_out: int, last_access: str) -> str:
    body_excerpt = (m.body[:280] + "…") if len(m.body) > 280 else m.body
    body_excerpt = body_excerpt.replace("<", "&lt;").replace(">", "&gt;")
    expires_chip = f'<span class="chip chip-warn">expires {m.expires}</span>' if m.expires else ""
    return f"""
    <div class="mem-card" data-file="{m.filename}">
      <div class="mem-head">
        <div class="mem-name">{m.name}</div>
        <div class="mem-file">{m.filename}</div>
      </div>
      <div class="mem-desc">{m.description}</div>
      <div class="mem-chips">
        <span class="chip">↩ {refs_in}</span>
        <span class="chip">↪ {refs_out}</span>
        <span class="chip">{m.age_days}d old</span>
        <span class="chip">last seen: {last_access}</span>
        {expires_chip}
      </div>
      <details class="mem-body">
        <summary>preview</summary>
        <pre>{body_excerpt}</pre>
      </details>
    </div>"""

def build_graph_data(mems, refs):
    """Returns nodes/links suitable for d3-force or simple canvas viz."""
    type_color = {
        "user": "#7c3aed", "feedback": "#00d4ff", "self": "#ec4899",
        "procedural": "#06b6d4", "project": "#10b981",
        "reference": "#f59e0b", "untyped": "#888888",
    }
    incoming = Counter()
    for src, dsts in refs.items():
        for dst in dsts:
            incoming[dst] += 1
    nodes = [
        {
            "id": m.filename,
            "label": m.name[:28],
            "type": m.type or "untyped",
            "color": type_color.get(m.type or "untyped", "#888"),
            "size": 5 + min(15, incoming.get(m.filename, 0) * 2),
        }
        for m in mems
    ]
    links = [
        {"source": src, "target": dst}
        for src, dsts in refs.items() for dst in dsts
    ]
    return nodes, links

def main():
    mems = list_memories()
    refs = detect_references(mems)
    access = access_stats()
    by_cat = categorize(mems)

    # ref counts per file
    out_counts = {fn: len(dsts) for fn, dsts in refs.items()}
    in_counts = Counter()
    for src, dsts in refs.items():
        for dst in dsts:
            in_counts[dst] += 1

    sections = []
    for key, (label, sub) in CATEGORY_LABELS.items():
        items = sorted(by_cat.get(key, []), key=lambda m: -in_counts.get(m.filename, 0))
        if not items:
            continue
        cards = "\n".join(
            card(
                m,
                in_counts.get(m.filename, 0),
                out_counts.get(m.filename, 0),
                datetime.fromtimestamp(access.get(m.filename, {}).get("last", 0)).strftime("%Y-%m-%d") if access.get(m.filename, {}).get("last") else "never"
            )
            for m in items
        )
        sections.append(f"""
        <div class="glass-panel">
          <h2>{label} <span class="count-badge">{len(items)}</span></h2>
          <p class="sub">{sub}</p>
          <div class="card-grid">{cards}</div>
        </div>""")

    nodes, links = build_graph_data(mems, refs)
    graph_data = json.dumps({"nodes": nodes, "links": links})

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Claude's Model of the User</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: 'Segoe UI', system-ui, sans-serif;
    background: linear-gradient(135deg, #0a0a1a 0%, #1a1a2e 50%, #16213e 100%);
    color: #e0e0e0; min-height: 100vh; padding: 24px;
  }}
  .container {{ max-width: 1500px; margin: 0 auto; }}
  h1 {{
    font-size: 30px; font-weight: 700;
    background: linear-gradient(135deg, #00d4ff, #7c3aed);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent;
    margin-bottom: 6px;
  }}
  .lead {{ color: #aaa; font-size: 14px; margin-bottom: 24px; max-width: 800px; line-height: 1.6; }}
  .glass-panel {{
    background: rgba(255,255,255,0.05);
    backdrop-filter: blur(10px);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 16px; padding: 24px; margin-bottom: 22px;
  }}
  .glass-panel h2 {{
    font-size: 17px; color: #00d4ff;
    text-transform: uppercase; letter-spacing: 1.5px;
    font-weight: 700; margin-bottom: 4px;
    display: flex; align-items: center; gap: 10px;
  }}
  .sub {{ color: #777; font-size: 13px; margin-bottom: 16px; }}
  .count-badge {{
    background: rgba(0,212,255,0.2); color: #00d4ff;
    padding: 2px 10px; border-radius: 12px; font-size: 12px;
  }}
  .card-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    gap: 14px;
  }}
  .mem-card {{
    background: rgba(255,255,255,0.04);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 12px; padding: 14px;
    transition: all 0.15s ease;
  }}
  .mem-card:hover {{
    border-color: rgba(0,212,255,0.4);
    background: rgba(255,255,255,0.06);
  }}
  .mem-head {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 6px; }}
  .mem-name {{ font-weight: 600; color: #e0e0e0; font-size: 14px; }}
  .mem-file {{ font-family: Consolas, monospace; color: #555; font-size: 11px; }}
  .mem-desc {{ color: #bbb; font-size: 13px; line-height: 1.5; margin-bottom: 10px; }}
  .mem-chips {{ display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 8px; }}
  .chip {{
    background: rgba(255,255,255,0.06);
    color: #999; font-size: 11px;
    padding: 2px 8px; border-radius: 10px;
  }}
  .chip-warn {{ background: rgba(245,158,11,0.18); color: #f59e0b; }}
  .mem-body summary {{ cursor: pointer; color: #00d4ff; font-size: 12px; }}
  .mem-body pre {{
    margin-top: 8px;
    padding: 10px; background: rgba(0,0,0,0.3);
    border-radius: 6px; font-size: 11px;
    color: #ccc; white-space: pre-wrap;
    font-family: Consolas, monospace; line-height: 1.5;
  }}
  #graph-container {{
    position: relative; width: 100%; height: 600px;
    background: rgba(0,0,0,0.3); border-radius: 12px;
    border: 1px solid rgba(255,255,255,0.1);
    overflow: hidden;
  }}
  #graph {{ width: 100%; height: 100%; display: block; }}
  .graph-tip {{
    position: absolute; background: rgba(10,10,30,0.95);
    color: #fff; padding: 6px 10px; border-radius: 6px;
    font-size: 12px; pointer-events: none;
    border: 1px solid rgba(0,212,255,0.4);
    display: none; z-index: 10;
  }}
  .graph-legend {{ margin-top: 12px; display: flex; gap: 18px; flex-wrap: wrap; font-size: 12px; color: #aaa; }}
  .legend-dot {{ display: inline-block; width: 10px; height: 10px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }}
  .footer {{ text-align: center; color: #555; font-size: 12px; margin-top: 24px; }}
</style>
</head>
<body><div class="container">
  <h1>What I Know About You</h1>
  <p class="lead">
    This is the model of you that lives in my memory. Each card is a memory file I read when relevant.
    Numbers ↩↪ show how many other memories reference / are referenced by this one.
    If anything's wrong, outdated, or missing — tell me and I'll update it.
  </p>

  <div class="glass-panel">
    <h2>Memory Graph</h2>
    <p class="sub">Interactive view of how your memories connect. Drag nodes, hover for details.</p>
    <div id="graph-container">
      <canvas id="graph"></canvas>
      <div class="graph-tip" id="tip"></div>
    </div>
    <div class="graph-legend">
      <span><span class="legend-dot" style="background:#7c3aed;"></span>Who you are</span>
      <span><span class="legend-dot" style="background:#00d4ff;"></span>How we work</span>
      <span><span class="legend-dot" style="background:#10b981;"></span>What we build</span>
      <span><span class="legend-dot" style="background:#f59e0b;"></span>External</span>
      <span><span class="legend-dot" style="background:#888;"></span>Uncategorized</span>
      <span style="color:#666;">node size = how often it's referenced by others</span>
    </div>
  </div>

  {''.join(sections)}

  <div class="footer">Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · {len(mems)} memories · {sum(len(v) for v in refs.values())} cross-references</div>
</div>

<script>
const data = {graph_data};

const canvas = document.getElementById('graph');
const tip = document.getElementById('tip');
const ctx = canvas.getContext('2d');

function resize() {{
  const rect = canvas.parentElement.getBoundingClientRect();
  canvas.width = rect.width;
  canvas.height = rect.height;
}}
resize();
window.addEventListener('resize', resize);

// Initialize positions
const W = canvas.width, H = canvas.height;
data.nodes.forEach((n, i) => {{
  const angle = (i / data.nodes.length) * Math.PI * 2;
  n.x = W/2 + Math.cos(angle) * Math.min(W,H) * 0.35;
  n.y = H/2 + Math.sin(angle) * Math.min(W,H) * 0.35;
  n.vx = 0; n.vy = 0;
}});

// Build node lookup
const nodeMap = {{}};
data.nodes.forEach(n => nodeMap[n.id] = n);
data.links.forEach(l => {{
  l.source = nodeMap[l.source];
  l.target = nodeMap[l.target];
}});

let dragging = null;
let mouseX = 0, mouseY = 0;

canvas.addEventListener('mousedown', e => {{
  const r = canvas.getBoundingClientRect();
  const mx = e.clientX - r.left, my = e.clientY - r.top;
  for (const n of data.nodes) {{
    const d = Math.hypot(n.x - mx, n.y - my);
    if (d < n.size + 4) {{ dragging = n; break; }}
  }}
}});
canvas.addEventListener('mousemove', e => {{
  const r = canvas.getBoundingClientRect();
  mouseX = e.clientX - r.left;
  mouseY = e.clientY - r.top;
  if (dragging) {{ dragging.x = mouseX; dragging.y = mouseY; dragging.vx = 0; dragging.vy = 0; }}
  let hit = null;
  for (const n of data.nodes) {{
    if (Math.hypot(n.x - mouseX, n.y - mouseY) < n.size + 4) {{ hit = n; break; }}
  }}
  if (hit) {{
    tip.style.display = 'block';
    tip.style.left = (mouseX + 12) + 'px';
    tip.style.top  = (mouseY + 12) + 'px';
    tip.textContent = hit.label;
  }} else {{
    tip.style.display = 'none';
  }}
}});
canvas.addEventListener('mouseup', () => dragging = null);
canvas.addEventListener('mouseleave', () => {{ dragging = null; tip.style.display = 'none'; }});

function step() {{
  // Force-directed simulation
  const k = 0.02;
  // Repulsion
  for (let i = 0; i < data.nodes.length; i++) {{
    for (let j = i+1; j < data.nodes.length; j++) {{
      const a = data.nodes[i], b = data.nodes[j];
      const dx = b.x - a.x, dy = b.y - a.y;
      const d2 = dx*dx + dy*dy + 1;
      const f = 800 / d2;
      a.vx -= dx * f * 0.01;
      a.vy -= dy * f * 0.01;
      b.vx += dx * f * 0.01;
      b.vy += dy * f * 0.01;
    }}
  }}
  // Attraction along edges
  for (const l of data.links) {{
    const dx = l.target.x - l.source.x, dy = l.target.y - l.source.y;
    const d = Math.hypot(dx, dy) || 1;
    const f = (d - 80) * 0.005;
    l.source.vx += dx/d * f;
    l.source.vy += dy/d * f;
    l.target.vx -= dx/d * f;
    l.target.vy -= dy/d * f;
  }}
  // Gravity to center
  for (const n of data.nodes) {{
    n.vx += (canvas.width/2 - n.x) * 0.0008;
    n.vy += (canvas.height/2 - n.y) * 0.0008;
    n.vx *= 0.85; n.vy *= 0.85;
    if (n !== dragging) {{ n.x += n.vx; n.y += n.vy; }}
  }}
}}

function draw() {{
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  // Edges
  ctx.strokeStyle = 'rgba(255,255,255,0.1)';
  ctx.lineWidth = 1;
  for (const l of data.links) {{
    ctx.beginPath();
    ctx.moveTo(l.source.x, l.source.y);
    ctx.lineTo(l.target.x, l.target.y);
    ctx.stroke();
  }}
  // Nodes
  for (const n of data.nodes) {{
    ctx.fillStyle = n.color;
    ctx.beginPath();
    ctx.arc(n.x, n.y, n.size, 0, Math.PI*2);
    ctx.fill();
    if (n.size > 8) {{
      ctx.fillStyle = '#fff';
      ctx.font = '10px Segoe UI, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(n.label.slice(0, 18), n.x, n.y + n.size + 12);
    }}
  }}
}}

function loop() {{
  step();
  draw();
  requestAnimationFrame(loop);
}}
window.addEventListener('load', () => {{ resize(); loop(); }});
</script>
</body></html>"""

    OUT_HTML.parent.mkdir(parents=True, exist_ok=True)
    OUT_HTML.write_text(html, encoding="utf-8")
    print(f"whoami complete: {OUT_HTML}")
    print(f"Memories: {len(mems)} | Cross-refs: {sum(len(v) for v in refs.values())}")
    if "--no-open" not in sys.argv and os.name == "nt":
        os.system(f'start "" "{OUT_HTML}"')

if __name__ == "__main__":
    main()
