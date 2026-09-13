---
name: Reproduce with a failing test first
description: When a bug report arrives, the first step is a failing test that reproduces it; only then change code, and keep the test as the regression guard
type: procedural
weight: high
created: 2026-02-20
related: feedback_no_flattery.md, reference_git_conventions.md
---

The order Alex wants for any bug: reproduce, then fix, then verify.

- Reproduce: write the smallest failing test that shows the reported behavior. If it cannot be reproduced, say so before touching code.
- Fix: change the code until that test passes without breaking the suite.
- Verify: run the full suite and paste the summary line, not a claim.

This applies to "quick" fixes too. The bug that was "obviously" a one-liner in March took three attempts because the first two fixed a symptom the test would have caught.
