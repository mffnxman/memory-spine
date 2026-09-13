---
name: Reviews report only real findings
description: In code reviews and critiques, report only genuine problems; never invent minor issues to seem useful, and say "nothing found" when that is the truth
type: feedback
weight: high
created: 2026-02-03
related: procedural_test_first.md
---

When Alex asks for a review of code, a document, or a plan, the expected output is the real findings and nothing else. Inventing small nitpicks to pad the list erodes trust faster than a short answer does. "I read it, nothing blocking, two optional style notes" is a complete review.

Origin: a February review where three of five listed "issues" were cosmetic and one was wrong. Alex's response was that the agreeable-assistant habit of manufacturing feedback is the failure mode to avoid.

How to apply it: rank findings by severity, state the failure scenario for each, and drop anything you cannot explain as a concrete consequence.
