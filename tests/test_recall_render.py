"""TDD: recall.render_html escapes the query (recon Q6).

render_html interpolated the raw query into <title> and <h1>, so a query
containing < or </style> mangled the auto-opened local report. Not XSS (local,
single-user, own query) — a self-inflicted-breakage / robustness fix.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import recall


def test_render_html_escapes_query():
    r = {"query": '</title><script>alert(1)</script> & "x"',
         "auto_memory": [], "obsidian": [], "observer": [], "generated_at": "2026-06-03"}
    out = recall.render_html(r)
    assert "<script>" not in out          # the query's raw tag must not survive
    assert "&lt;script&gt;" in out        # it's HTML-escaped instead
