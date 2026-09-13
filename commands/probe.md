---
description: Phenomenology regression — manage and score inheritance probes from _meta/probes/
---

# /probe

Probe the inheritance layer: does it still feel like air, or has it started to
feel like walls? Sister to `/continuity-test` (retrieval correctness) — this
one tracks the *texture* of inheritance, not the facts.

## Usage

```
/probe                          # show this help + list current probes
/probe list                     # list all probes (date, type, result)
/probe diff [path-or-name]      # score a probe vs prior probes of same type
/probe drift [type]             # show drift trajectory across probes of a type
/probe new <type> [slug]        # scaffold a new probe markdown file
```

Defaults:
- `/probe diff` with no arg → diffs the most recent probe
- `/probe drift` with no arg → defaults to type `comfort-and-fit`

## Thresholds

Cosine similarity (BAAI/bge-small-en-v1.5):
- `>= 0.85` — TIGHT — voice + content stable, no drift signal
- `0.70-0.85` — NORMAL — some shift, expected over time
- `< 0.70` — INVESTIGATE — meaningful divergence; substrate, retrieval, or model

## Implementation

Runs `probe.py` with the provided subcommand. Output streams back as a table.

Treat output as advisory — drift below threshold is signal to *look*, not a
verdict on the response.

```bash
python "{{SCRIPTS_DIR}}/probe.py" $ARGUMENTS
```
