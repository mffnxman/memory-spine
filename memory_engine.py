"""
memory_engine.py — core memory operations for the user's auto-memory system.

Single source of truth for:
  - Frontmatter parsing
  - Memory listing & metadata
  - Search (grep + TF-IDF semantic-lite)
  - Access logging (sidecar)
  - Cross-reference detection
  - Expiration handling

All scripts (recall, audit, whoami, conflict, prefetch) import from here.
Nothing here auto-runs; pure library.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import struct
import sys
import time
from collections import Counter
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Hung-WMI guard MUST precede any onnxruntime/torch import (they call
# platform.system() at import time, which queries WMI on py3.12 and blocks
# forever when the WMI service is wedged — the 2026-06-10 freeze).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import paths as _paths  # noqa: E402
import _no_wmi  # noqa: F401  (side-effect import)

# ─── Paths ────────────────────────────────────────────────────────────────────
MEMORY_DIR = _paths.MEMORY_DIR
META_DIR = MEMORY_DIR / "_meta"
ARCHIVE_DIR = META_DIR / "archive"
SCRIPTS_DIR = _paths.SCRIPTS_DIR
# DB_PATH honors MEMORY_DB_PATH so the test suite (and any sandbox) can point at
# an isolated copy instead of mutating the live DB. Unset in production → default.
DB_PATH = (
    Path(os.environ["MEMORY_DB_PATH"])
    if os.environ.get("MEMORY_DB_PATH")
    else META_DIR / "memory.db"
)
INDEX_FILE = MEMORY_DIR / "MEMORY.md"

META_DIR.mkdir(parents=True, exist_ok=True)
ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

# ─── Frontmatter ──────────────────────────────────────────────────────────────
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)

# Gentle bias — weight is a tie-breaker, not a dominator. Foundational memories
# already get surfaced separately by the boot ritual, so we don't need to drown
# out semantic relevance here.
WEIGHT_BOOST = {"high": 1.3, "medium": 1.1, "low": 0.9, "": 1.0}

VALID_TYPES = {"user", "feedback", "project", "reference", "self", "procedural"}


@dataclass
class Memory:
    """A single memory file parsed.

    Bi-temporal model:
      - mtime: file system modification time (when memory was last edited)
      - created: when memory was first written (frontmatter, optional)
      - event_date: when the *thing* the memory describes happened (optional)

    Example: a memory written today about something the user said in March would
    have created=2026-05-08 and event_date=2026-03-15. Helps reasoning about
    "the user was X then, now Y" type drift.
    """

    path: Path
    name: str = ""
    description: str = ""
    type: str = ""  # user | feedback | project | reference | self | procedural
    weight: str = ""  # high | medium | low (empty = default 1.0)
    expires: Optional[str] = None  # YYYY-MM-DD
    created: Optional[str] = None  # YYYY-MM-DD — when memory was authored
    event_date: Optional[str] = None  # YYYY-MM-DD — when the described thing happened
    related: list[str] = field(default_factory=list)
    body: str = ""
    raw_frontmatter: dict = field(default_factory=dict)

    @property
    def weight_multiplier(self) -> float:
        return WEIGHT_BOOST.get(self.weight.lower(), 1.0)

    @property
    def filename(self) -> str:
        return self.path.name

    @property
    def mtime(self) -> float:
        return self.path.stat().st_mtime

    @property
    def age_days(self) -> int:
        return int((time.time() - self.mtime) / 86400)

    @property
    def is_expired(self) -> bool:
        if not self.expires:
            return False
        try:
            exp = datetime.strptime(self.expires, "%Y-%m-%d")
            return datetime.now() > exp
        except ValueError:
            return False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["path"] = str(self.path)
        d["filename"] = self.filename
        d["age_days"] = self.age_days
        d["is_expired"] = self.is_expired
        return d


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse simple YAML-like frontmatter. Returns (fields, body)."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm_block, body = m.group(1), m.group(2)
    fields: dict = {}
    for line in fm_block.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            fields[k.strip()] = v.strip()
    return fields, body


def load_memory(path: Path) -> Memory:
    """Load a single memory file."""
    text = path.read_text(encoding="utf-8")
    fm, body = parse_frontmatter(text)
    related = [r.strip() for r in fm.get("related", "").split(",") if r.strip()]
    return Memory(
        path=path,
        name=fm.get("name", path.stem),
        description=fm.get("description", ""),
        type=fm.get("type", ""),
        weight=fm.get("weight", ""),
        expires=fm.get("expires") or None,
        created=fm.get("created") or None,
        event_date=fm.get("event_date") or None,
        related=related,
        body=body,
        raw_frontmatter=fm,
    )


def list_memories(include_index: bool = False) -> list[Memory]:
    """All memory files. Skips MEMORY.md, hidden dirs, and _meta/_scripts."""
    out = []
    for p in sorted(MEMORY_DIR.glob("*.md")):
        if p.name == "MEMORY.md" and not include_index:
            continue
        try:
            out.append(load_memory(p))
        except Exception as e:
            print(f"WARN: failed to parse {p.name}: {e}", file=sys.stderr)
    return out


# ─── Sidecar DB (access log + reference cache + embeddings) ──────────────────
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    # v14.2: wait on lock instead of erroring instantly, and allow concurrent
    # readers during a write. memory.db is touched by overlapping processes (KG
    # write hook, PPR/boot reads, consolidation); defaults silently dropped
    # entities/edges on contention. Mirrors observations.db. Propagates to
    # kg.db() (which calls this as _base_db). WAL persists on the db file.
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
    except Exception:
        pass
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS access (
            filename TEXT NOT NULL,
            ts INTEGER NOT NULL,
            session_id TEXT,
            source TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_access_file ON access(filename);
        CREATE INDEX IF NOT EXISTS idx_access_ts ON access(ts);

        CREATE TABLE IF NOT EXISTS refs (
            from_file TEXT NOT NULL,
            to_file TEXT NOT NULL,
            UNIQUE(from_file, to_file)
        );

        CREATE TABLE IF NOT EXISTS conflicts (
            ts INTEGER NOT NULL,
            new_file TEXT NOT NULL,
            existing_file TEXT NOT NULL,
            similarity REAL,
            note TEXT,
            resolved INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS embeddings (
            filename TEXT PRIMARY KEY,
            mtime REAL NOT NULL,
            model TEXT NOT NULL,
            dim INTEGER NOT NULL,
            vector BLOB NOT NULL,
            content_hash TEXT NOT NULL
        );
    """)
    return conn


def log_access(filename: str, source: str = "unknown", session_id: str = "") -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO access(filename, ts, session_id, source) VALUES (?,?,?,?)",
            (filename, int(time.time()), session_id, source),
        )


def access_stats() -> dict[str, dict]:
    """Per-file: last_access (epoch), count_30d, count_90d, count_total."""
    now = int(time.time())
    d30, d90 = now - 30 * 86400, now - 90 * 86400
    out: dict[str, dict] = {}
    with db() as conn:
        for row in conn.execute(
            "SELECT filename, MAX(ts), COUNT(*) FROM access GROUP BY filename"
        ):
            fn, last, total = row
            out[fn] = {"last": last, "total": total, "d30": 0, "d90": 0}
        for row in conn.execute(
            "SELECT filename, COUNT(*) FROM access WHERE ts >= ? GROUP BY filename",
            (d30,),
        ):
            out.setdefault(row[0], {"last": 0, "total": 0, "d30": 0, "d90": 0})[
                "d30"
            ] = row[1]
        for row in conn.execute(
            "SELECT filename, COUNT(*) FROM access WHERE ts >= ? GROUP BY filename",
            (d90,),
        ):
            out.setdefault(row[0], {"last": 0, "total": 0, "d30": 0, "d90": 0})[
                "d90"
            ] = row[1]
    return out


# ─── Cross-references ─────────────────────────────────────────────────────────
def detect_references(mems: list[Memory]) -> dict[str, set[str]]:
    """For each memory, find which other memory filenames it links to.

    Sources of link signal:
      1. Explicit `related:` frontmatter field (consolidation-blessed)
      2. Body mentions of filename or stem
    """
    filenames = {m.filename for m in mems}
    refs: dict[str, set[str]] = {m.filename: set() for m in mems}
    for m in mems:
        # 1. Explicit related: links — strongest signal
        for r in m.related:
            r = r.strip()
            if r and r != m.filename and r in filenames:
                refs[m.filename].add(r)
        # 2. Body mentions
        for fn in filenames:
            if fn == m.filename:
                continue
            stem = fn.replace(".md", "")
            if (
                fn in m.body
                or f"({fn})" in m.body
                or re.search(rf"\b{re.escape(stem)}\b", m.body)
            ):
                refs[m.filename].add(fn)
    return refs


def cache_references(refs: dict[str, set[str]]) -> None:
    with db() as conn:
        conn.execute("DELETE FROM refs")
        rows = [(src, dst) for src, dsts in refs.items() for dst in dsts]
        conn.executemany(
            "INSERT OR IGNORE INTO refs(from_file, to_file) VALUES (?,?)", rows
        )


# ─── Search ──────────────────────────────────────────────────────────────────
TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(s: str) -> list[str]:
    return TOKEN_RE.findall(s.lower())


def _build_corpus(mems: list[Memory]) -> tuple[list[Counter], dict[str, float]]:
    """Returns (per-doc term counts, idf table)."""
    docs = []
    df: Counter = Counter()
    for m in mems:
        toks = tokenize(m.name + " " + m.description + " " + m.body)
        c = Counter(toks)
        docs.append(c)
        for t in c:
            df[t] += 1
    n = len(mems) or 1
    idf = {t: math.log((n + 1) / (cnt + 1)) + 1 for t, cnt in df.items()}
    return docs, idf


def _recency_boost(filename: str, access: dict) -> float:
    """Forgetting-curve inspired recency boost.

    Combines two signals:
      - Frequency in last 30 days (every access adds 0.05x, capped at 1.5x)
      - Last-access recency (Ebbinghaus-style decay: exp(-age_days/180))

    A never-accessed memory returns 1.0 (neutral). A heavily-used recent memory
    can boost ~1.6x. A long-untouched one decays toward ~0.7.
    """
    a = access.get(filename)
    if not a:
        return 1.0
    last = a.get("last", 0)
    d30 = a.get("d30", 0)

    freq_boost = 1.0 + min(0.5, d30 * 0.05)

    if last == 0:
        recency_boost = 1.0
    else:
        age_days = max(0, (time.time() - last) / 86400)
        # decay constant 180d half-life, floored at 0.7
        recency_boost = max(0.7, math.exp(-age_days / 180))

    return freq_boost * recency_boost


def search(
    query: str, mems: Optional[list[Memory]] = None, top_k: int = 8
) -> list[tuple[Memory, float, list[str]]]:
    """TF-IDF lite search with weight + recency boosts. Returns [(memory, score, matched_terms), ...]."""
    if mems is None:
        mems = list_memories()
    if not mems:
        return []
    docs, idf = _build_corpus(mems)
    q_terms = tokenize(query)
    if not q_terms:
        return []
    access = access_stats()
    results: list[tuple[Memory, float, list[str]]] = []
    for m, doc in zip(mems, docs):
        score = 0.0
        matched = []
        doc_total = sum(doc.values()) or 1
        for t in q_terms:
            if t in doc:
                tf = doc[t] / doc_total
                score += tf * idf.get(t, 1.0)
                matched.append(t)
        # Boost: name match
        name_toks = set(tokenize(m.name))
        desc_toks = set(tokenize(m.description))
        for t in q_terms:
            if t in name_toks:
                score += 0.5
            if t in desc_toks:
                score += 0.2
        # Apply weight + recency multipliers
        score *= m.weight_multiplier
        score *= _recency_boost(m.filename, access)
        if score > 0:
            results.append((m, score, matched))
    results.sort(key=lambda x: -x[1])
    return results[:top_k]


# ─── MEMORY.md index parsing & validation ────────────────────────────────────
# Match either canonical markdown link `[title](file.md)` OR the
# backticked-filename style `- `file.md` — description` that MEMORY.md
# actually uses today. The audit was silently returning [] for two weeks
# because only the link form was recognized.
INDEX_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+\.md)\)")
INDEX_BACKTICK_RE = re.compile(r"`([^`]+\.md)`")


def parse_index() -> list[tuple[str, str, str]]:
    """Returns list of (title, filename, description) from MEMORY.md."""
    if not INDEX_FILE.exists():
        return []
    out = []
    seen: set[str] = set()
    for line in INDEX_FILE.read_text(encoding="utf-8").splitlines():
        # Try canonical markdown link form first
        m = INDEX_LINK_RE.search(line)
        if m:
            title, fn = m.group(1), m.group(2)
            tail = (
                line.split(m.group(0), 1)[1].lstrip(" —-") if m.group(0) in line else ""
            )
            if fn not in seen:
                seen.add(fn)
                out.append((title, fn, tail.strip()))
            continue
        # Fall back to backticked-filename form
        b = INDEX_BACKTICK_RE.search(line)
        if b:
            fn = b.group(1)
            tail = (
                line.split(b.group(0), 1)[1].lstrip(" —-:")
                if b.group(0) in line
                else ""
            )
            if fn not in seen:
                seen.add(fn)
                out.append((fn, fn, tail.strip()))
    return out


def find_orphans() -> dict[str, list[str]]:
    """Returns {missing_files: [...], unindexed_files: [...]}."""
    # Memory files are always bare basenames in MEMORY_DIR. An indexed entry
    # containing a path separator (e.g. `~/.claude/commands/hack.md`) is a
    # reference to an external file, not a memory — exclude it so it doesn't
    # register as a permanent phantom "missing" orphan.
    indexed = {fn for _, fn, _ in parse_index() if "/" not in fn and "\\" not in fn}
    on_disk = {p.name for p in MEMORY_DIR.glob("*.md") if p.name != "MEMORY.md"}
    return {
        "missing": sorted(indexed - on_disk),
        "unindexed": sorted(on_disk - indexed),
    }


# ─── Duplicate detection ──────────────────────────────────────────────────────
def cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    dot = sum(a[k] * b.get(k, 0) for k in a)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


def detect_duplicates(
    mems: list[Memory], threshold: float = 0.55
) -> list[tuple[Memory, Memory, float]]:
    """Returns pairs above similarity threshold. O(n^2) — fine at <1000 files.

    Cross-session observer-promoted pairs are skipped: those files share the
    same "Auto-promoted observation cluster" boilerplate, so token cosine
    measures the template, not the content (they scored 0.75-0.87 against
    each other while describing unrelated sessions). Same-session pairs are
    kept — a double promotion of one cluster is a real duplicate.

    A pair is also skipped when either file's `dup_ok:` frontmatter lists the
    other — the reviewed-and-intentional escape hatch for memories that are
    similar by design (e.g. a hub narrative vs the spokes it references).
    """

    def _dup_ok_set(m: Memory) -> set[str]:
        raw = str(m.raw_frontmatter.get("dup_ok", "") or "")
        return {s.strip() for s in raw.split(",") if s.strip()}

    docs = [
        Counter(tokenize(m.name + " " + m.description + " " + m.body)) for m in mems
    ]
    pairs = []
    for i in range(len(mems)):
        for j in range(i + 1, len(mems)):
            a, b = mems[i], mems[j]
            a_fm, b_fm = a.raw_frontmatter, b.raw_frontmatter
            if (
                a_fm.get("origin") == "observer-promoted"
                and b_fm.get("origin") == "observer-promoted"
                and a_fm.get("source_session") != b_fm.get("source_session")
            ):
                continue
            if b.filename in _dup_ok_set(a) or a.filename in _dup_ok_set(b):
                continue
            sim = cosine(docs[i], docs[j])
            if sim >= threshold:
                pairs.append((a, b, sim))
    pairs.sort(key=lambda x: -x[2])
    return pairs


# ─── Vector embeddings (semantic search) ─────────────────────────────────────
EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
EMBEDDING_DIM = 384
# Shared input cap for BOTH the write/index embed text and the recall query
# embed (lesson 4: bge-small ~512-token window; beyond it the tail is silently
# dropped). One constant so write and query paths cannot drift.
EMBED_INPUT_CHARS = 1500
_embedder = None

TELEMETRY_PATH = MEMORY_DIR / "_meta" / "v3_2_telemetry.jsonl"


def _log_telemetry(record: dict) -> None:
    """Append a one-line observability/fail-soft breadcrumb. Never raises.

    Makes the otherwise-silent search_hybrid fail-soft branches (and the query
    truncation) observable so a degraded recall path can be seen, not guessed.
    """
    try:
        import json as _json
        import time as _time

        TELEMETRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        record.setdefault("ts", int(_time.time()))
        record.setdefault("component", "search_hybrid")
        with TELEMETRY_PATH.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(record, separators=(",", ":")) + "\n")
    except Exception:
        pass


_embedding_cache: dict[str, "list[float]"] = {}  # filename -> vector


def _get_embedder():
    """Lazy-load fastembed model. Cached.

    MEMORY_HOT_PATH=1 (set by hook-spawned processes like prefetch) skips the
    in-process ONNX load entirely — ~660MB and seconds of wall time per fresh
    hook process. search_hybrid already degrades gracefully to TF-IDF(+KG)
    when vector_search yields nothing, so hot-path quality degrades softly
    instead of the prompt stalling (2026-06-10 freeze)."""
    global _embedder
    if os.environ.get("MEMORY_HOT_PATH"):
        return None
    if _embedder is None:
        try:
            from fastembed import TextEmbedding

            _embedder = TextEmbedding(EMBEDDING_MODEL)
        except ImportError:
            return None
    return _embedder


def embed_text(text: str) -> Optional[list[float]]:
    """Generate embedding for arbitrary text. Returns None if fastembed missing."""
    m = _get_embedder()
    if m is None:
        return None
    return list(next(m.embed([text])))


def embed_batch(texts: list[str]) -> Optional[list[list[float]]]:
    """Batch embedding — much faster than one-at-a-time."""
    m = _get_embedder()
    if m is None:
        return None
    return [list(v) for v in m.embed(texts)]


def _vec_to_blob(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def _blob_to_vec(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob)//4}f", blob))


def _content_hash(m: Memory) -> str:
    """Fast hash of memory content for staleness detection."""
    import hashlib

    s = (m.name + m.description + m.body).encode("utf-8")
    return hashlib.md5(s).hexdigest()[:16]


def _memory_text_for_embedding(m: Memory) -> str:
    """Compose the text we embed. Name + description carry most semantic signal."""
    return f"{m.name}\n{m.description}\n{m.body[:EMBED_INPUT_CHARS]}"


def reindex_embeddings(
    mems: Optional[list[Memory]] = None, force: bool = False
) -> dict:
    """Generate embeddings for memories whose hash changed (or all if force=True)."""
    if mems is None:
        mems = list_memories()
    m = _get_embedder()
    if m is None:
        return {"error": "fastembed not installed; run `pip install fastembed`"}

    # Determine which need re-embedding
    existing: dict[str, str] = {}  # filename -> content_hash
    with db() as conn:
        for row in conn.execute("SELECT filename, content_hash FROM embeddings"):
            existing[row[0]] = row[1]

    to_embed: list[Memory] = []
    for mem in mems:
        h = _content_hash(mem)
        if force or existing.get(mem.filename) != h:
            to_embed.append(mem)

    if not to_embed:
        return {"updated": 0, "skipped": len(mems), "total_indexed": len(existing)}

    # Batch embed
    texts = [_memory_text_for_embedding(mem) for mem in to_embed]
    vecs = embed_batch(texts)

    with db() as conn:
        for mem, vec in zip(to_embed, vecs):
            conn.execute(
                "INSERT OR REPLACE INTO embeddings(filename, mtime, model, dim, vector, content_hash) VALUES (?,?,?,?,?,?)",
                (
                    mem.filename,
                    mem.mtime,
                    EMBEDDING_MODEL,
                    len(vec),
                    _vec_to_blob(vec),
                    _content_hash(mem),
                ),
            )

    return {
        "updated": len(to_embed),
        "skipped": len(mems) - len(to_embed),
        "total_indexed": len(existing) + len(to_embed),
    }


def index_health() -> dict:
    """Vector-index coverage vs the on-disk corpus. Cheap (no embedding).

    {memories, embedded, missing, stale, orphans} — `embedded` = memories with
    a CURRENT vector (row present and content_hash matches); `missing` = memories
    with no vector row; `stale` = vector exists but content_hash drifted;
    `orphans` = embedding rows whose memory file no longer exists. missing/stale
    mean the memory is not semantically recallable until a reindex.
    """
    mems = list_memories()
    with db() as conn:
        existing = {
            row[0]: row[1]
            for row in conn.execute("SELECT filename, content_hash FROM embeddings")
        }
    missing = sum(1 for m in mems if m.filename not in existing)
    stale = sum(
        1
        for m in mems
        if m.filename in existing and existing[m.filename] != _content_hash(m)
    )
    return {
        "memories": len(mems),
        # Fresh coverage, not raw row count: stale-hash and orphaned rows must
        # not count as "covered" (they made the boot line contradict itself:
        # "1 not vector-indexed (111/111 covered)").
        "embedded": len(mems) - missing - stale,
        "missing": missing,
        "stale": stale,
        "orphans": len(set(existing) - {m.filename for m in mems}),
    }


def _cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def vector_search(
    query: str, mems: Optional[list[Memory]] = None, top_k: int = 8
) -> list[tuple[Memory, float]]:
    """Cosine similarity over stored embeddings. Applies weight + recency boosts."""
    if mems is None:
        mems = list_memories()
    # Cap the query to the same window as the index path so an over-long
    # prompt+HyDE concatenation can't be silently truncated by the model
    # past the cliff. (Observability of when this fires is added in the
    # search_hybrid telemetry pass.)
    if len(query) > EMBED_INPUT_CHARS:
        _log_telemetry(
            {
                "stage": "query_embed",
                "event": "truncated",
                "orig_len": len(query),
                "cap": EMBED_INPUT_CHARS,
            }
        )
        query = query[:EMBED_INPUT_CHARS]
    qvec = embed_text(query)
    if qvec is None:
        return []
    by_name = {m.filename: m for m in mems}
    access = access_stats()
    results: list[tuple[Memory, float]] = []
    with db() as conn:
        for row in conn.execute("SELECT filename, vector, model FROM embeddings"):
            fn = row[0]
            if fn not in by_name:
                continue
            # Skip embeddings produced by a different model — cosine across
            # models is meaningless even at the same dim. A model swap without
            # `reindex --force` thus degrades gracefully (no match) instead of
            # silently ranking on incompatible vectors.
            if row[2] != EMBEDDING_MODEL:
                continue
            vec = _blob_to_vec(row[1])
            sim = _cosine(qvec, vec)
            mem = by_name[fn]
            sim *= mem.weight_multiplier
            sim *= _recency_boost(fn, access)
            results.append((mem, sim))
    results.sort(key=lambda x: -x[1])
    return results[:top_k]


# ─── Hybrid retrieval (TF-IDF + Vector via Reciprocal Rank Fusion) ───────────
# Set by search_hybrid when the rerank floor drops EVERY candidate — consumers
# (prefetch's observer fallback) read it to distinguish active suppression from
# an empty spine. Reset at each search_hybrid entry.
LAST_FLOOR_SUPPRESSED_ALL = False


def search_hybrid(
    query: str,
    mems: Optional[list[Memory]] = None,
    top_k: int = 8,
    k: int = 60,
    rerank_floor: Optional[float] = None,
) -> list[tuple[Memory, float, list[str]]]:
    """Fuse TF-IDF and vector results using Reciprocal Rank Fusion.

    RRF score for doc d = sum over rankings: 1 / (k + rank_i(d))
    Standard k=60 from Cormack et al. 2009.

    rerank_floor: minimum cross-encoder rerank_score (a logit) a candidate must
    reach to be returned — the reranker is the only stage that scores the
    (query, memory) PAIR, so it's the only signal that can tell a noise query
    from a real one (decayed-RRF passes MIN_SCORE on almost anything). None
    disables the floor. Candidates without a rerank_score (reranker dead /
    fail-soft) are kept — degraded retrieval must never become empty retrieval.
    """
    global LAST_FLOOR_SUPPRESSED_ALL
    LAST_FLOOR_SUPPRESSED_ALL = False
    if mems is None:
        mems = list_memories()
    tf = search(query, mems=mems, top_k=top_k * 3)
    vec = vector_search(query, mems=mems, top_k=top_k * 3)
    if not vec:
        # No vector lane (no embeddings yet, or MEMORY_HOT_PATH skipped the
        # embedder). With rerank OFF this is the classic TF-IDF fallback. With
        # rerank ON we must keep going: TF-IDF-only fusion still feeds the
        # pool to the warm rerank daemon, which is the only stage that scores
        # the (query, memory) pair — bailing here is what silently degraded
        # every prefetch to raw TF-IDF from 2026-06-10 to 2026-07-31.
        _rerank_on = False
        try:
            from feature_flags import is_enabled as _re_flag

            _rerank_on = _re_flag("rerank_enabled")
        except Exception:
            pass
        if not _rerank_on:
            return tf[:top_k]  # fallback if no embeddings yet
        _log_telemetry({"stage": "fusion", "event": "vec_empty_fused", "n_tf": len(tf)})

    rrf_scores: dict[str, float] = {}
    matched_terms: dict[str, list[str]] = {}
    mem_lookup: dict[str, Memory] = {}

    for rank, (m, _, terms) in enumerate(tf):
        rrf_scores[m.filename] = rrf_scores.get(m.filename, 0) + 1.0 / (k + rank + 1)
        matched_terms[m.filename] = terms
        mem_lookup[m.filename] = m

    for rank, (m, _) in enumerate(vec):
        rrf_scores[m.filename] = rrf_scores.get(m.filename, 0) + 1.0 / (k + rank + 1)
        matched_terms.setdefault(m.filename, [])
        mem_lookup.setdefault(m.filename, m)

    # v3.2 Phase 2: KG-PPR as a 4th retriever in the RRF fusion. Adds graph-
    # structural signal — memories that mention entities related to the
    # query (multi-hop via Personalized PageRank) get folded in. Strictly
    # additive: if PPR returns nothing (no query entities matched) or fails,
    # RRF proceeds on TF-IDF + vector as before.
    try:
        from feature_flags import is_enabled as _flag_enabled

        if _flag_enabled("kg_ppr_enabled"):
            from kg_ppr import search as _ppr_search

            ppr_hits = _ppr_search(query, top_k=top_k * 3)
            fn_to_mem = {m.filename: m for m in mems}
            for rank, hit in enumerate(ppr_hits):
                fn = hit["filename"]
                if fn not in fn_to_mem:
                    continue  # PPR returned a memory not in current list — skip
                rrf_scores[fn] = rrf_scores.get(fn, 0) + 1.0 / (k + rank + 1)
                matched_terms.setdefault(fn, [])
                mem_lookup.setdefault(fn, fn_to_mem[fn])
    except Exception as e:
        # fail-soft: PPR failure leaves RRF unchanged — but record it.
        _log_telemetry(
            {
                "stage": "kg_ppr",
                "event": "fail_soft",
                "error": f"{type(e).__name__}: {e}"[:200],
            }
        )

    # v15 (2026-07-09): Hebbian co-recall as a 5th fusion signal. Memories that
    # historically co-fired with the current top hits (same prefetch batch —
    # see hebbian.py) get folded in via spreading activation. Strictly
    # additive and flag-gated like PPR: no edges or flag off → RRF unchanged.
    try:
        from feature_flags import is_enabled as _flag_enabled

        if _flag_enabled("hebbian_enabled"):
            from hebbian import spread as _hebbian_spread

            seeds = [
                fn for fn, _ in sorted(rrf_scores.items(), key=lambda x: -x[1])[:5]
            ]
            fn_to_mem = {m.filename: m for m in mems}
            for rank, (fn, _score) in enumerate(_hebbian_spread(seeds, limit=top_k)):
                if fn not in fn_to_mem:
                    continue
                rrf_scores[fn] = rrf_scores.get(fn, 0) + 1.0 / (k + rank + 1)
                matched_terms.setdefault(fn, [])
                mem_lookup.setdefault(fn, fn_to_mem[fn])
    except Exception as e:
        _log_telemetry(
            {
                "stage": "hebbian",
                "event": "fail_soft",
                "error": f"{type(e).__name__}: {e}"[:200],
            }
        )

    # BIGBUFF 2.0 P2 (D1-04, 7/31 epilogue design): filename-literal boost — a
    # memory must always rank for its own identifiers. If the query contains a
    # memory's filename stem (underscore or space form, e.g. "observer_counter"
    # or "plan bigbuff2"), fold in one top-rank RRF vote for that file so it
    # enters the candidate pool regardless of TF-IDF/vector dilution. Strictly
    # additive; the reranker still judges the pair.
    try:
        _q_low = query.lower()
        for _m in mems:
            _stem = (
                _m.filename[:-3] if _m.filename.endswith(".md") else _m.filename
            ).lower()
            if len(_stem) >= 6 and (
                _stem in _q_low or _stem.replace("_", " ") in _q_low
            ):
                rrf_scores[_m.filename] = rrf_scores.get(_m.filename, 0) + 1.0 / (k + 1)
                matched_terms.setdefault(_m.filename, []).append("filename:" + _stem)
                mem_lookup.setdefault(_m.filename, _m)
    except Exception as e:
        _log_telemetry(
            {
                "stage": "filename_boost",
                "event": "fail_soft",
                "error": f"{type(e).__name__}: {e}"[:200],
            }
        )

    # v14 Phase 1: apply attention decay + corroboration boost before final sort.
    try:
        from decay import apply_decay
        import time as _t

        now_ts = int(_t.time())
        for fn in list(rrf_scores.keys()):
            m = mem_lookup[fn]
            mtime = m.path.stat().st_mtime if m.path.exists() else 0.0
            rrf_scores[fn] = apply_decay(
                rrf_scores[fn],
                fn,
                m.weight,
                name=m.name,
                mtime=mtime,
                now_ts=now_ts,
            )
    except Exception as e:
        # fail-soft: if decay breaks, return undecayed ranking — but record it.
        # decay self-logs nothing, so this is its only degradation signal.
        _log_telemetry(
            {
                "stage": "decay",
                "event": "fail_soft",
                "error": f"{type(e).__name__}: {e}"[:200],
            }
        )

    # v3.2 Phase 1: cross-encoder rerank (flag-gated, strictly additive).
    # If the flag is on, expand the candidate pool, score (query, memory) pairs
    # with bge-reranker-v2-m3, and return the precision-tuned top_k. If
    # anything fails (model missing, exception in rerank), fall through to the
    # original RRF-only path with zero side effects.
    try:
        from feature_flags import is_enabled

        if is_enabled("rerank_enabled"):
            from reranker import rerank as _rerank

            # Pull a wider pool so the reranker has room to reorder.
            pool_size = max(top_k * 3, 20)
            pool = sorted(rrf_scores.items(), key=lambda x: -x[1])[:pool_size]
            candidates = [
                {
                    "filename": fn,
                    "name": mem_lookup[fn].name,
                    "description": mem_lookup[fn].description,
                    "body": mem_lookup[fn].body,
                    "rrf_score": score,
                    "_terms": matched_terms.get(fn, []),
                }
                for fn, score in pool
            ]
            reranked = _rerank(query, candidates, top_k=top_k)
            if rerank_floor is not None:
                kept = [
                    c
                    for c in reranked
                    if not isinstance(c.get("rerank_score"), (int, float))
                    or c["rerank_score"] >= rerank_floor
                ]
                if len(kept) < len(reranked):
                    _log_telemetry(
                        {
                            "stage": "rerank_floor",
                            "event": "filtered",
                            "floor": rerank_floor,
                            "dropped": len(reranked) - len(kept),
                            "kept": len(kept),
                        }
                    )
                # BIGBUFF 2.0 P2 (D1-05, the user's call 2026-08-05): expose
                # whether the floor ACTIVELY suppressed everything, so the
                # observer fallback can distinguish "floor said noise" (stay
                # quiet) from "spine had nothing" (fallback may fire).
                # (global declared at function entry.)
                LAST_FLOOR_SUPPRESSED_ALL = bool(reranked) and not kept
                reranked = kept
            return [
                (mem_lookup[c["filename"]], c["rrf_score"], c.get("_terms", []))
                for c in reranked
            ]
    except Exception as e:
        # fail-soft: any rerank failure → original RRF result — but record it.
        # Event name must start with "rerank" or the sentinel's bypass
        # pairing can't see it (2026-08-13: these read as mystery
        # NO_RERANK_EVENT bypasses for a week).
        _log_telemetry(
            {
                "stage": "rerank",
                "event": "rerank_fail_soft",
                "error": f"{type(e).__name__}: {e}"[:200],
            }
        )

    fused = sorted(rrf_scores.items(), key=lambda x: -x[1])[:top_k]
    return [(mem_lookup[fn], score, matched_terms.get(fn, [])) for fn, score in fused]


# ─── Self-test ────────────────────────────────────────────────────────────────
def main():
    """Quick self-test: load all memories, print summary."""
    mems = list_memories()
    print(f"Loaded {len(mems)} memories from {MEMORY_DIR}")
    by_type = Counter(m.type or "untyped" for m in mems)
    print("By type:", dict(by_type))
    expired = [m.filename for m in mems if m.is_expired]
    print(f"Expired: {len(expired)} {expired if expired else ''}")
    orph = find_orphans()
    print(f"Index missing: {len(orph['missing'])}", orph["missing"][:3])
    print(f"Unindexed: {len(orph['unindexed'])}", orph["unindexed"][:3])
    refs = detect_references(mems)
    total_refs = sum(len(v) for v in refs.values())
    print(f"Cross-refs detected: {total_refs}")
    print("Top 3 most-referenced:")
    incoming = Counter()
    for src, dsts in refs.items():
        for dst in dsts:
            incoming[dst] += 1
    for fn, n in incoming.most_common(3):
        print(f"  {fn}: {n} incoming")


if __name__ == "__main__":
    main()
