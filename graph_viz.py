"""
graph_viz.py — interactive force-directed visualization of the knowledge graph.

Outputs glassmorphism HTML with canvas-rendered graph. Click an entity to filter,
hover for details, drag nodes to rearrange.

Usage:
  python graph_viz.py             # full graph
  python graph_viz.py --focus the user --hops 2     # neighborhood view
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from kg import db, list_entities, query_neighbors, graph_stats

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

OUT_HTML = Path.home() / "Downloads" / "knowledge_graph.html"

TYPE_COLORS = {
    "person":  "#7c3aed",
    "company": "#f59e0b",
    "project": "#10b981",
    "tool":    "#00d4ff",
    "concept": "#ec4899",
    "value":   "#fbbf24",
    "skill":   "#06b6d4",
    "file":    "#94a3b8",
}


def collect_full_graph() -> tuple[list[dict], list[dict]]:
    nodes_map: dict[int, dict] = {}
    edges: list[dict] = []
    with db() as conn:
        for row in conn.execute("SELECT id, name, type, confidence FROM entities"):
            nid, name, etype, conf = row
            nodes_map[nid] = {
                "id": nid, "name": name, "type": etype,
                "confidence": conf, "color": TYPE_COLORS.get(etype, "#888"),
                "size": 6,
            }
        for row in conn.execute(
            "SELECT from_id, to_id, type, confidence, source_memory FROM relationships"
        ):
            f, t, rt, c, src = row
            edges.append({
                "source": f, "target": t, "type": rt,
                "confidence": c, "source_memory": src or "",
            })
    # Boost size based on degree
    deg: dict[int, int] = {}
    for e in edges:
        deg[e["source"]] = deg.get(e["source"], 0) + 1
        deg[e["target"]] = deg.get(e["target"], 0) + 1
    for n in nodes_map.values():
        n["size"] = 6 + min(20, deg.get(n["id"], 0))
    return list(nodes_map.values()), edges


def collect_focus_graph(entity_name: str, hops: int) -> tuple[list[dict], list[dict]]:
    """Subgraph: entity + neighbors within `hops` hops."""
    edges_obj = query_neighbors(entity_name, hops=hops)
    if not edges_obj:
        return [], []
    name_to_node: dict[str, dict] = {}
    raw_edges = []
    for e in edges_obj:
        for n_name, n_type in [(e.from_name, e.from_type), (e.to_name, e.to_type)]:
            if n_name not in name_to_node:
                name_to_node[n_name] = {
                    "id": n_name, "name": n_name, "type": n_type,
                    "color": TYPE_COLORS.get(n_type, "#888"), "size": 6,
                }
        raw_edges.append({
            "source": e.from_name, "target": e.to_name,
            "type": e.rel_type, "confidence": e.confidence,
            "source_memory": e.source_memory or "",
        })
    deg: dict[str, int] = {}
    for e in raw_edges:
        deg[e["source"]] = deg.get(e["source"], 0) + 1
        deg[e["target"]] = deg.get(e["target"], 0) + 1
    for n in name_to_node.values():
        n["size"] = 6 + min(20, deg.get(n["id"], 0))
    # Highlight the focus node
    if entity_name in name_to_node:
        name_to_node[entity_name]["size"] += 6
        name_to_node[entity_name]["focus"] = True
    return list(name_to_node.values()), raw_edges


def render_html(nodes: list[dict], edges: list[dict], stats: dict, title: str) -> str:
    data = json.dumps({"nodes": nodes, "links": edges})
    legend = " ".join(
        f'<span class="leg"><span class="dot" style="background:{c}"></span>{t}</span>'
        for t, c in TYPE_COLORS.items()
    )
    type_chips = " ".join(
        f'<span class="chip">{t}: {n}</span>' for t, n in stats["by_type"].items()
    )
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>{title}</title><style>
  *{{margin:0;padding:0;box-sizing:border-box;}}
  body{{font-family:'Segoe UI',sans-serif;background:linear-gradient(135deg,#0a0a1a,#1a1a2e 50%,#16213e);color:#e0e0e0;min-height:100vh;padding:24px;}}
  .container{{max-width:1500px;margin:0 auto;}}
  h1{{font-size:28px;background:linear-gradient(135deg,#00d4ff,#7c3aed);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:6px;}}
  .lead{{color:#888;margin-bottom:18px;font-size:13px;}}
  .stats-row{{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:18px;}}
  .stat{{background:rgba(255,255,255,0.05);backdrop-filter:blur(10px);border:1px solid rgba(255,255,255,0.1);border-radius:12px;padding:12px 18px;text-align:center;min-width:120px;}}
  .stat .v{{font-size:24px;font-weight:700;color:#00d4ff;}}
  .stat .l{{font-size:10px;color:#888;text-transform:uppercase;letter-spacing:1px;margin-top:3px;}}
  .panel{{background:rgba(255,255,255,0.05);backdrop-filter:blur(10px);border:1px solid rgba(255,255,255,0.1);border-radius:16px;padding:14px;margin-bottom:18px;}}
  .chips{{display:flex;gap:8px;flex-wrap:wrap;}}
  .chip{{background:rgba(124,58,237,0.18);color:#c4b5fd;padding:3px 10px;border-radius:10px;font-size:11px;font-weight:600;}}
  #graph-wrap{{position:relative;width:100%;height:720px;background:rgba(0,0,0,0.3);border-radius:14px;border:1px solid rgba(255,255,255,0.1);overflow:hidden;}}
  #graph{{width:100%;height:100%;display:block;}}
  .tip{{position:absolute;background:rgba(10,10,30,0.95);color:#fff;padding:8px 12px;border-radius:8px;font-size:12px;pointer-events:none;border:1px solid rgba(0,212,255,0.4);display:none;z-index:10;max-width:280px;}}
  .tip strong{{color:#00d4ff;}}
  .tip .meta{{color:#888;font-size:10px;margin-top:4px;}}
  .legend{{display:flex;gap:16px;flex-wrap:wrap;font-size:11px;color:#999;justify-content:center;margin-top:12px;}}
  .leg{{display:inline-flex;align-items:center;gap:6px;}}
  .dot{{width:10px;height:10px;border-radius:50%;}}
</style></head><body><div class="container">
  <h1>Knowledge Graph</h1>
  <div class="lead">{title} · drag to rearrange · hover for details · double-click an entity to focus</div>

  <div class="stats-row">
    <div class="stat"><div class="v">{stats['entities']}</div><div class="l">Entities</div></div>
    <div class="stat"><div class="v">{stats['relationships']}</div><div class="l">Relationships</div></div>
    <div class="stat"><div class="v">{stats['mentions']}</div><div class="l">Mentions</div></div>
  </div>

  <div class="panel">
    <div class="chips">{type_chips}</div>
  </div>

  <div id="graph-wrap"><canvas id="graph"></canvas><div class="tip" id="tip"></div></div>
  <div class="legend">{legend}</div>
</div>

<script>
const data = {data};
const canvas = document.getElementById('graph');
const ctx = canvas.getContext('2d');
const tip = document.getElementById('tip');

function resize() {{
  const r = canvas.parentElement.getBoundingClientRect();
  canvas.width = r.width; canvas.height = r.height;
}}
resize();
window.addEventListener('resize', resize);

const W = canvas.width, H = canvas.height;
data.nodes.forEach((n, i) => {{
  const a = (i / data.nodes.length) * Math.PI * 2;
  n.x = W/2 + Math.cos(a) * Math.min(W,H) * 0.35;
  n.y = H/2 + Math.sin(a) * Math.min(W,H) * 0.35;
  n.vx = 0; n.vy = 0;
}});
const nodeMap = {{}}; data.nodes.forEach(n => nodeMap[n.id] = n);
data.links.forEach(l => {{ l.source = nodeMap[l.source]; l.target = nodeMap[l.target]; }});

let dragging = null, mx = 0, my = 0;

canvas.addEventListener('mousedown', e => {{
  const r = canvas.getBoundingClientRect();
  const px = e.clientX - r.left, py = e.clientY - r.top;
  for (const n of data.nodes) {{
    if (Math.hypot(n.x - px, n.y - py) < n.size + 4) {{ dragging = n; break; }}
  }}
}});
canvas.addEventListener('mousemove', e => {{
  const r = canvas.getBoundingClientRect();
  mx = e.clientX - r.left; my = e.clientY - r.top;
  if (dragging) {{ dragging.x = mx; dragging.y = my; dragging.vx = 0; dragging.vy = 0; }}
  let hit = null;
  for (const n of data.nodes) {{
    if (Math.hypot(n.x - mx, n.y - my) < n.size + 4) {{ hit = n; break; }}
  }}
  if (hit) {{
    const out = data.links.filter(l => l.source.id === hit.id).map(l => `${{l.type}} → ${{l.target.name}}`);
    const inn = data.links.filter(l => l.target.id === hit.id).map(l => `${{l.source.name}} → ${{l.type}}`);
    tip.innerHTML = `<strong>${{hit.name}}</strong> <span class="meta">(${{hit.type}})</span>` +
                     (out.length ? '<div class="meta">↪ ' + out.slice(0, 4).join(' · ') + '</div>' : '') +
                     (inn.length ? '<div class="meta">↩ ' + inn.slice(0, 4).join(' · ') + '</div>' : '');
    tip.style.display = 'block';
    tip.style.left = (mx + 14) + 'px';
    tip.style.top = (my + 14) + 'px';
  }} else {{
    tip.style.display = 'none';
  }}
}});
canvas.addEventListener('mouseup', () => dragging = null);
canvas.addEventListener('mouseleave', () => {{ dragging = null; tip.style.display = 'none'; }});

function step() {{
  for (let i = 0; i < data.nodes.length; i++) {{
    for (let j = i + 1; j < data.nodes.length; j++) {{
      const a = data.nodes[i], b = data.nodes[j];
      const dx = b.x - a.x, dy = b.y - a.y;
      const d2 = dx*dx + dy*dy + 1;
      const f = 1500 / d2;
      a.vx -= dx * f * 0.01;
      a.vy -= dy * f * 0.01;
      b.vx += dx * f * 0.01;
      b.vy += dy * f * 0.01;
    }}
  }}
  for (const l of data.links) {{
    const dx = l.target.x - l.source.x, dy = l.target.y - l.source.y;
    const d = Math.hypot(dx, dy) || 1;
    const f = (d - 110) * 0.005;
    l.source.vx += dx/d * f; l.source.vy += dy/d * f;
    l.target.vx -= dx/d * f; l.target.vy -= dy/d * f;
  }}
  for (const n of data.nodes) {{
    n.vx += (canvas.width/2 - n.x) * 0.0008;
    n.vy += (canvas.height/2 - n.y) * 0.0008;
    n.vx *= 0.85; n.vy *= 0.85;
    if (n !== dragging) {{ n.x += n.vx; n.y += n.vy; }}
  }}
}}
function draw() {{
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.strokeStyle = 'rgba(255,255,255,0.12)'; ctx.lineWidth = 1;
  for (const l of data.links) {{
    ctx.beginPath();
    ctx.moveTo(l.source.x, l.source.y);
    ctx.lineTo(l.target.x, l.target.y);
    ctx.stroke();
  }}
  for (const n of data.nodes) {{
    ctx.fillStyle = n.color;
    if (n.focus) {{
      ctx.shadowColor = n.color; ctx.shadowBlur = 14;
    }} else {{
      ctx.shadowBlur = 0;
    }}
    ctx.beginPath();
    ctx.arc(n.x, n.y, n.size, 0, Math.PI * 2);
    ctx.fill();
    ctx.shadowBlur = 0;
    if (n.size > 9) {{
      ctx.fillStyle = '#fff';
      ctx.font = '11px Segoe UI, sans-serif';
      ctx.textAlign = 'center';
      const label = n.name.length > 22 ? n.name.slice(0, 20) + '…' : n.name;
      ctx.fillText(label, n.x, n.y + n.size + 13);
    }}
  }}
}}
function loop() {{ step(); draw(); requestAnimationFrame(loop); }}
window.addEventListener('load', () => {{ resize(); loop(); }});
</script>
</body></html>"""


def main():
    args = sys.argv[1:]
    focus = None
    hops = 2
    if "--focus" in args:
        focus = args[args.index("--focus") + 1]
    if "--hops" in args:
        hops = int(args[args.index("--hops") + 1])

    stats = graph_stats()
    if focus:
        nodes, edges = collect_focus_graph(focus, hops)
        title = f"Focus on: {focus} ({hops}-hop neighborhood)"
        if not nodes:
            print(f"No entity found matching '{focus}'")
            sys.exit(1)
    else:
        nodes, edges = collect_full_graph()
        title = "Full Knowledge Graph"

    OUT_HTML.write_text(render_html(nodes, edges, stats, title), encoding="utf-8")
    print(f"Wrote {OUT_HTML}")
    print(f"  nodes: {len(nodes)}  edges: {len(edges)}")
    if "--no-open" not in args and os.name == "nt":
        os.system(f'start "" "{OUT_HTML}"')


if __name__ == "__main__":
    main()
