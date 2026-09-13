---
name: Build before buying
description: When evaluating a SaaS tool or paid service, default to "can we build this with what we already have" and only recommend purchase for genuinely novel capability
type: feedback
weight: high
created: 2026-01-18
related: user_profile.md, decisions.md
---

Alex's standing rule when a SaaS tool, subscription, or paid API comes up: check first whether existing scripts, sqlite, and a local model can do the job. Buying is the exception, reserved for capability that cannot be replicated in a weekend (a real-time data feed, a hardware-backed service, something with network effects).

Why it stuck: the monitoring service evaluation in January. The quote was per-host per-month; the replacement was a 90-line Python script and a scheduled task, done in an afternoon, and it has been running since.

How to apply it: when asked "should we use X", answer with the build path first, its cost in hours, and what would be lost versus the paid option. Then let Alex decide. Do not lead with the purchase.
