"""
verification.py — detect verified-vs-claimed signals from session activity.

Phase 5 (v14): scan a session_log text and decide whether work in this session
was *verified* (tests run + passed, smoke test confirmed) or merely *claimed*
(code shipped without verification). Outputs a frontmatter-ready field for
the epilogue draft and an evidence dict for transparency.

Three states:
  - "true"    : strong evidence of verification (tests passed, smoke verified)
  - "false"   : code shipped but no verification evidence
  - "unknown" : no shippable work in this session (planning, discussion, reading)

Heuristics are deliberately conservative. False > unknown > true means we
don't accidentally over-claim verification.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Optional


# Patterns that suggest tests were run + passed
TEST_RAN_PATTERNS = [
    r"\bpytest\b",
    r"\bjest\b",
    r"\bcargo test\b",
    r"\bgo test\b",
    r"\bnpm test\b",
    r"\byarn test\b",
    r"\bphpunit\b",
    r"\bmix test\b",
    r"\bswift test\b",
    r"continuity_test\.py",
    r"\btests? (?:passed|pass|green)\b",
]

TEST_PASSED_PATTERNS = [
    r"\b\d+ (?:passed|passing)(?:\s*,\s*0 fail)?",
    r"\b(?:passed|passing|green|ok)\b\s*\d+/\d+",
    r"\b0 (?:failed|failures|errors?)\b",
    r"No drift detected",
    r"All smoke tests? passed",
    r"All tests? pass(?:ed|ing)",
]

SMOKE_VERIFIED_PATTERNS = [
    r"\bsmoke[- ]test(?:ed|ing|s)?\b",
    r"\bverified\b",
    r"\bend[- ]to[- ]end\b",
    r"\bsanity[- ]check(?:ed)?\b",
    r"\bconfirmed (?:working|in browser|via .*)\b",
    r"\bround[- ]trip\b",
]

CODE_SHIPPED_PATTERNS = [
    r"^- `\d+:\d+:\d+` Wrote `.*\.(py|ts|tsx|js|jsx|go|rs|md|json|yaml|yml|sh|ps1)`",
    r"^- `\d+:\d+:\d+` Edited `.*\.(py|ts|tsx|js|jsx|go|rs|md|json|yaml|yml|sh|ps1)`",
    r"Wrote .*\.py",
    r"Edited .*\.py",
]


def _count_matches(text: str, patterns: list[str]) -> int:
    n = 0
    for pat in patterns:
        n += len(re.findall(pat, text, re.MULTILINE | re.IGNORECASE))
    return n


def detect(session_log_text: str) -> dict:
    """Returns {"verified": "true|false|unknown", "evidence": {...}}.

    Decision tree:
      1. If test-passed signals present -> "true"
      2. Else if smoke-verified signals present -> "true"
      3. Else if code-shipped signals present -> "false" (work was done, not verified)
      4. Else -> "unknown" (no shippable artifacts)
    """
    if not session_log_text or not session_log_text.strip():
        return {"verified": "unknown", "evidence": {"reason": "empty session log"}}

    evidence = {
        "test_ran_signals":    _count_matches(session_log_text, TEST_RAN_PATTERNS),
        "test_passed_signals": _count_matches(session_log_text, TEST_PASSED_PATTERNS),
        "smoke_signals":       _count_matches(session_log_text, SMOKE_VERIFIED_PATTERNS),
        "shipped_signals":     _count_matches(session_log_text, CODE_SHIPPED_PATTERNS),
    }

    if evidence["test_passed_signals"] >= 1:
        verified = "true"
        evidence["reason"] = "tests passed (test_passed pattern matched)"
    elif evidence["smoke_signals"] >= 1 and evidence["test_ran_signals"] >= 1:
        verified = "true"
        evidence["reason"] = "smoke verification + tests ran"
    elif evidence["smoke_signals"] >= 2:
        verified = "true"
        evidence["reason"] = "multiple smoke/verified mentions"
    elif evidence["shipped_signals"] >= 1:
        verified = "false"
        evidence["reason"] = "code shipped, no verification evidence"
    else:
        verified = "unknown"
        evidence["reason"] = "no shippable artifacts detected"

    return {"verified": verified, "evidence": evidence}


def render_frontmatter_field(result: dict) -> str:
    """Return YAML-frontmatter lines to inject into an epilogue draft."""
    v = result.get("verified", "unknown")
    reason = result.get("evidence", {}).get("reason", "")
    return f"verified: {v}\nverified_reason: \"{reason}\""


def render_section(result: dict) -> str:
    """Return a markdown section explaining the verified-vs-claimed split."""
    v = result["verified"]
    ev = result.get("evidence", {})
    lines = [
        f"**Verified status:** `{v}`",
        f"_Reason:_ {ev.get('reason', '(no reason logged)')}",
        "",
        "| Signal | Count |",
        "|---|---|",
        f"| tests ran | {ev.get('test_ran_signals', 0)} |",
        f"| tests passed | {ev.get('test_passed_signals', 0)} |",
        f"| smoke/verified mentions | {ev.get('smoke_signals', 0)} |",
        f"| code shipped | {ev.get('shipped_signals', 0)} |",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Verification signal detector")
    sub = ap.add_subparsers(dest="cmd")
    p_file = sub.add_parser("file", help="Detect from a session_log path")
    p_file.add_argument("path")
    p_stdin = sub.add_parser("stdin", help="Detect from stdin")
    sub.add_parser("test", help="Smoke tests")
    args = ap.parse_args()

    if args.cmd == "file":
        text = Path(args.path).read_text(encoding="utf-8", errors="ignore")
        print(json.dumps(detect(text), indent=2))
    elif args.cmd == "stdin":
        text = sys.stdin.read()
        print(json.dumps(detect(text), indent=2))
    elif args.cmd == "test":
        # Tests-passed case
        log_a = """
        - `10:23:45` Edited `_scripts/decay.py`
        - `10:24:01` Bash: pytest test_decay.py
        - `10:24:05` 14 passed in 0.3s
        """
        r = detect(log_a)
        print("a (tests passed):", r["verified"])
        assert r["verified"] == "true", r

        # Code shipped no verification
        log_b = """
        - `10:23:45` Wrote `_scripts/new_thing.py`
        - `10:24:01` Edited `_scripts/wired_it.py`
        """
        r = detect(log_b)
        print("b (code shipped only):", r["verified"])
        assert r["verified"] == "false", r

        # Discussion only
        log_c = """
        - `10:23:45` the user: lets think about this approach
        - `10:24:01` the user: yeah do option B
        """
        r = detect(log_c)
        print("c (discussion only):", r["verified"])
        assert r["verified"] == "unknown", r

        print("All verification tests passed.")
    else:
        ap.print_help()
