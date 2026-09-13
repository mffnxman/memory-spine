#!/usr/bin/env python3
"""brain_viz.py — render the memory engine as a neural-network-style visualization.

Scans the live memory directory + observer/graph databases, derives nodes and
edges from real structure (observations, access log, refs, related:, slug
mentions, digest weeks, promotion records), and injects the graph into
brain_viz_template.html. The template additionally weaves a faint adjacency
mesh between neighboring layers for the full trainer look; bright strands are
always real recorded/derived connections.

Outputs:
  ~/Downloads/brain_neural_viz.html          (standalone, auto-openable)
  $OBSIDIAN_VAULT/brain/neural-viz.html  (vault copy, when OBSIDIAN_VAULT is set)

Re-run any time to refresh from live data:
  python brain_viz.py
"""

import json
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
import sys as _sys  # noqa: E402
_sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths as _paths  # noqa: E402

MEM = _paths.MEMORY_DIR  # portability v15
VAULT = _paths.OBSIDIAN_VAULT or Path("/nonexistent-vault")
VAULT_BRAIN = VAULT / "brain"
DOWNLOADS = _paths.OUTPUT_DIR
TEMPLATE = Path(__file__).parent / "brain_viz_template.html"
TODAY = date.today()
OBS_TARGET = 1300  # input-layer sample size for the 17k+ observations

DATE_IN_NAME = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:-(\d{2})(\d{2}))?")

L_OBS, L_LOG, L_EPI, L_CAND, L_PROJ, L_REF, L_IDENT, L_DIG, L_VAULT, L_BOOT = range(10)

LAYERS = [
    {"key": "observe", "title": "IN", "fn": "observe", "sub": "observations"},
    {"key": "capture", "title": "HL 1", "fn": "capture", "sub": "session logs"},
    {"key": "distill", "title": "HL 2", "fn": "distill", "sub": "epilogues"},
    {"key": "cluster", "title": "HL 3", "fn": "cluster", "sub": "promotion candidates"},
    {"key": "projects", "title": "HL 4", "fn": "curate", "sub": "projects & plans"},
    {"key": "reference", "title": "HL 5", "fn": "curate", "sub": "reference"},
    {"key": "identity", "title": "HL 6", "fn": "curate", "sub": "identity & feedback"},
    {"key": "narrate", "title": "HL 7", "fn": "narrate", "sub": "weekly digests"},
    {"key": "vault", "title": "HL 8", "fn": "mirror", "sub": "obsidian vault"},
    {"key": "boot", "title": "OUT", "fn": "boot", "sub": "boot index"},
]
LAYER_FOR_TYPE = {
    "project": L_PROJ,
    "plan": L_PROJ,
    "reference": L_REF,
    "digest": L_DIG,
    "feedback": L_IDENT,
    "user": L_IDENT,
    "self": L_IDENT,
}


def read_text(p: Path, limit: int = 200_000) -> str:
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def frontmatter(text: str) -> dict:
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.S)
    if not m:
        return {}
    fm = {}
    for line in m.group(1).splitlines():
        km = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if km:
            fm[km.group(1)] = km.group(2).strip().strip('"')
    return fm


def parse_date(s: str):
    m = DATE_IN_NAME.search(s or "")
    if m:
        try:
            return datetime(
                int(m.group(1)),
                int(m.group(2)),
                int(m.group(3)),
                int(m.group(4) or 0),
                int(m.group(5) or 0),
            )
        except ValueError:
            return None
    return None


def file_date(p: Path, fm: dict):
    for key in ("date", "created", "event_date", "week", "date_range"):
        d = parse_date(fm.get(key, ""))
        if d:
            return d
    return parse_date(p.stem) or datetime.fromtimestamp(p.stat().st_mtime)


def days_old(d: datetime) -> int:
    return max(0, (TODAY - d.date()).days)


def iso_week_range(year: int, week: int):
    monday = datetime.strptime(f"{year}-W{week:02d}-1", "%G-W%V-%u")
    return monday, monday + timedelta(days=7)


nodes, edges = [], []


def add_node(layer, label, path, dt, desc=""):
    nid = len(nodes)
    nodes.append(
        {
            "id": nid,
            "layer": layer,
            "label": str(label)[:90],
            "path": str(path),
            "date": dt.strftime("%Y-%m-%d"),
            "age": days_old(dt),
            "desc": str(desc)[:160],
        }
    )
    return nid


def add_edge(a, b, kind):
    if a is not None and b is not None and a != b:
        edges.append({"a": a, "b": b, "k": kind})


# ---- HL1: session logs -------------------------------------------------------
log_nodes = []  # (nid, dt)
for p in sorted((MEM / "_meta" / "session_logs").glob("*.md"), key=lambda p: p.name):
    dt = parse_date(p.stem) or datetime.fromtimestamp(p.stat().st_mtime)
    log_nodes.append((add_node(L_LOG, p.stem, p, dt), dt))


def nearest_log(dt):
    same_day = [(nid, ldt) for nid, ldt in log_nodes if ldt.date() == dt.date()]
    pool = same_day or log_nodes
    if not pool:
        return None, False
    nid = min(pool, key=lambda t: abs((t[1] - dt).total_seconds()))[0]
    return nid, bool(same_day)


# ---- IN: observations (observer db) ------------------------------------------
obs_total = 0
obs_sampled = 0
odb = MEM / "_meta" / "observations.db"
promoted_slugs = []  # (obs_dt, slug) resolved to edges after spine exists
if odb.exists():
    con = sqlite3.connect(f"file:{odb}?mode=ro", uri=True)
    cur = con.cursor()
    obs_total = cur.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    step = max(1, round(obs_total / OBS_TARGET))
    rows = cur.execute(
        "SELECT o.id, o.ts, o.tool_name, o.promoted_to_memory_id, s.started_at "
        "FROM observations o LEFT JOIN sessions s ON s.session_id = o.session_id "
        "ORDER BY o.ts"
    ).fetchall()
    con.close()
    for i, (oid, ts, tool, promoted, s_start) in enumerate(rows):
        is_promoted_slug = promoted and promoted not in ("rejected", "pending")
        if i % step and not is_promoted_slug:
            continue
        dt = datetime.fromtimestamp(ts)
        nid = add_node(L_OBS, f"{tool} · obs #{oid}", f"observations.db · id {oid}", dt)
        obs_sampled += 1
        lid, same = nearest_log(dt)
        add_edge(nid, lid, "observe" if same else "temporal")
        if is_promoted_slug:
            promoted_slugs.append((nid, str(promoted)))

# ---- HL2: epilogues ------------------------------------------------------------
epi_nodes, epi_texts = [], {}
for p in sorted((MEM / "_meta" / "epilogues").rglob("*.md"), key=lambda p: p.name):
    text = read_text(p, 8_000)
    fm = frontmatter(text)
    dt = parse_date(p.stem) or file_date(p, fm)
    nid = add_node(L_EPI, fm.get("session", p.stem), p, dt)
    epi_nodes.append((nid, dt))
    epi_texts[nid] = text

for lid, ldt in log_nodes:
    same_day = [(eid, edt) for eid, edt in epi_nodes if edt.date() == ldt.date()]
    pool = same_day or epi_nodes
    if pool:
        eid = min(pool, key=lambda t: abs((t[1] - ldt).total_seconds()))[0]
        add_edge(lid, eid, "capture" if same_day else "temporal")

# ---- HL3: promotion candidates ---------------------------------------------------
cand_nodes = []
for p in sorted(
    (MEM / "_meta" / "promotion_candidates").rglob("*.md"), key=lambda p: p.name
):
    text = read_text(p, 4_000)
    fm = frontmatter(text)
    dt = parse_date(p.stem) or file_date(p, fm)
    nid = add_node(L_CAND, fm.get("name", p.stem), p, dt)
    cand_nodes.append((nid, dt, text))
    same_day = [(eid, edt) for eid, edt in epi_nodes if edt.date() == dt.date()]
    if same_day:
        add_edge(
            min(same_day, key=lambda t: abs((t[1] - dt).total_seconds()))[0],
            nid,
            "cluster",
        )
    elif epi_nodes:
        add_edge(
            min(epi_nodes, key=lambda t: abs((t[1] - dt).total_seconds()))[0],
            nid,
            "temporal",
        )

# ---- HL4-7: spine memories by type ------------------------------------------------
spine_meta = {}  # stem -> (nid, fm, dt)
digest_nodes = {}
for p in sorted(MEM.glob("*.md"), key=lambda p: p.name):
    if p.stem == "MEMORY":
        continue
    text = read_text(p, 12_000)
    fm = frontmatter(text)
    dt = file_date(p, fm)
    layer = LAYER_FOR_TYPE.get(fm.get("type", ""), L_REF)
    nid = add_node(layer, fm.get("name", p.stem), p, dt, fm.get("description", ""))
    spine_meta[p.stem] = (nid, fm, dt)
    wm = re.match(r"digest_(\d{4})-w(\d+)", p.stem)
    if wm:
        digest_nodes[f"{wm.group(1)}-w{int(wm.group(2)):02d}"] = nid

stems = {s: nid for s, (nid, _, _) in spine_meta.items()}

# observation promotions recorded in the db
for onid, slug in promoted_slugs:
    target = stems.get(Path(slug).stem)
    add_edge(onid, target, "promote")

# epilogue body mentions a spine slug
for eid, text in epi_texts.items():
    for stem, nid in stems.items():
        if stem in text:
            add_edge(eid, nid, "curate")

# candidate topic matches a spine stem
for nid, _, text in cand_nodes:
    topic = re.search(r"auto-cluster:\s*(\S+)", text)
    if topic:
        words = set(topic.group(1).lower().split("_"))
        for stem, sid in stems.items():
            if words and words.issubset(set(stem.lower().replace("-", "_").split("_"))):
                add_edge(nid, sid, "promote")

# spine <-> spine: related: frontmatter + memory.db refs table
for stem, (nid, fm, _) in spine_meta.items():
    rel = fm.get("related", "")
    for target in re.findall(r"([\w\-]+)\.md", rel) + re.findall(
        r"(digest_\d{4}-w\d+)", rel
    ):
        if target in stems:
            add_edge(nid, stems[target], "related")

mdb = MEM / "_meta" / "memory.db"
recall_pairs = 0
if mdb.exists():
    con = sqlite3.connect(f"file:{mdb}?mode=ro", uri=True)
    cur = con.cursor()
    for a, b in cur.execute("SELECT from_file, to_file FROM refs"):
        add_edge(stems.get(Path(a).stem), stems.get(Path(b).stem), "related")
    # Hebbian co-recall synapses (v15): memories that fired together in the
    # same prefetch batch, weighted + decayed. Top edges only for the render.
    try:
        for a, b in cur.execute(
            "SELECT a, b FROM co_recall ORDER BY decayed DESC LIMIT 400"
        ):
            add_edge(stems.get(Path(a).stem), stems.get(Path(b).stem), "hebbian")
    except sqlite3.OperationalError:
        pass
    # access log: each row is a recorded recall event — match to that day's log
    seen_pairs = set()
    for fname, ts in cur.execute("SELECT filename, ts FROM access"):
        snid = stems.get(Path(fname).stem)
        if snid is None:
            continue
        try:
            dt = datetime.fromtimestamp(ts)
        except (OSError, OverflowError, ValueError, TypeError):
            continue
        lid, same = nearest_log(dt)
        if lid is not None and same and (lid, snid) not in seen_pairs:
            seen_pairs.add((lid, snid))
            add_edge(lid, snid, "recall")
    recall_pairs = len(seen_pairs)
    con.close()

# spine -> digest: ISO-week membership + digest body mentions
for wk, did in digest_nodes.items():
    y, w = int(wk[:4]), int(wk.split("w")[1])
    start, end = iso_week_range(y, w)
    body = read_text(MEM / f"digest_{y}-w{w}.md", 40_000)
    for stem, (nid, fm, dt) in spine_meta.items():
        if nid == did:
            continue
        if start <= dt < end or stem in body:
            add_edge(nid, did, "narrate")

# digest chain
chain = sorted(digest_nodes.items())
for (_, a), (_, b) in zip(chain, chain[1:]):
    add_edge(a, b, "chain")

# ---- HL8: obsidian vault notes ---------------------------------------------------
vault_nodes = []
if VAULT.exists():
    for p in sorted(VAULT.rglob("*.md"), key=lambda p: p.name):
        rel = p.relative_to(VAULT)
        if ".obsidian" in rel.parts or rel.parts[0] == "brain":
            continue
        body = read_text(p, 20_000)
        dt = datetime.fromtimestamp(p.stat().st_mtime)
        nid = add_node(L_VAULT, rel.stem, p, dt)
        vault_nodes.append(nid)
        for stem, (snid, _, _) in spine_meta.items():
            if stem != p.stem and stem in body:
                add_edge(snid, nid, "mirror")

# ---- OUT: boot index --------------------------------------------------------------
boot_nodes = []
memory_md = MEM / "MEMORY.md"
if memory_md.exists():
    boot_nodes.append(
        add_node(
            L_BOOT,
            "MEMORY.md — boot index",
            memory_md,
            datetime.fromtimestamp(memory_md.stat().st_mtime),
            "the index every session boots from",
        )
    )
for name in ("brain-full.md", "stack-snapshot.md"):
    p = VAULT_BRAIN / name
    if p.exists():
        boot_nodes.append(
            add_node(
                L_BOOT,
                f"vault/brain/{name}",
                p,
                datetime.fromtimestamp(p.stat().st_mtime),
                "mobile mirror, regenerated twice daily",
            )
        )
if boot_nodes:
    index_body = read_text(memory_md, 60_000)
    for stem, (nid, fm, dt) in spine_meta.items():
        if stem in index_body:
            add_edge(nid, boot_nodes[0], "boot")
    for _, did in sorted(digest_nodes.items()):
        add_edge(did, boot_nodes[0], "boot")
    for vid in vault_nodes:
        for b in boot_nodes:
            add_edge(vid, b, "boot")

# ---- dedupe -----------------------------------------------------------------------
seen = set()
edges = [
    e
    for e in edges
    if (key := (e["a"], e["b"], e["k"])) not in seen and not seen.add(key)
]

excluded = (
    sum(1 for _ in (MEM / "_meta" / "provenance").rglob("*.md"))
    + sum(1 for _ in (MEM / "_meta" / "archive").rglob("*.md"))
    + sum(1 for _ in (MEM / "_meta" / "probes").rglob("*.md"))
)

data = {
    "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
    "layers": LAYERS,
    "nodes": nodes,
    "edges": edges,
    "obs_total": obs_total,
    "obs_sampled": obs_sampled,
    "excluded_note": (
        f"input layer: {obs_sampled:,} of {obs_total:,} observations shown "
        f"(every ~{max(1, round(obs_total / OBS_TARGET))}th, promotions kept) · "
        f"{excluded} files excluded: provenance, archive, probes"
    ),
}

html = read_text(TEMPLATE, 2_000_000)
if "/*__DATA__*/" not in html:
    sys.exit("template missing /*__DATA__*/ placeholder")
html = html.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False))

out_main = DOWNLOADS / "brain_neural_viz.html"
out_main.write_text(html, encoding="utf-8")
targets = [out_main]
if VAULT_BRAIN.exists():
    out_vault = VAULT_BRAIN / "neural-viz.html"
    out_vault.write_text(html, encoding="utf-8")
    targets.append(out_vault)

by_layer = {}
for n in nodes:
    by_layer[n["layer"]] = by_layer.get(n["layer"], 0) + 1
by_kind = {}
for e in edges:
    by_kind[e["k"]] = by_kind.get(e["k"], 0) + 1

print(f"nodes: {len(nodes)}  real edges: {len(edges)}  (recall pairs: {recall_pairs})")
print("per layer:", {LAYERS[k]["sub"]: v for k, v in sorted(by_layer.items())})
print("per edge kind:", by_kind)
for t in targets:
    print("wrote:", t)
