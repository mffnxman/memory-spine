---
description: "Memory retrieval regression test — runs curated query→memory cases through hybrid search (use: /continuity-test [--weight core] [--baseline save|diff path])"
---

# Continuity Test

The user invoked `/continuity-test`. Regression harness for the memory retrieval
system — runs curated query→expected-memory cases through `search_hybrid` and
reports pass rate + MRR.

Pass criterion: any of `expected` files appears in top-K (default 3).

## Usage

- `/continuity-test`                    → run all cases, human-readable
- `/continuity-test --weight core`      → only "core" cases (the spine)
- `/continuity-test --weight project`   → only "project" cases
- `/continuity-test --baseline save baseline.json` → snapshot current results
- `/continuity-test --baseline diff baseline.json` → compare current vs baseline

## Steps

1. Run the test:
   ```bash
   python "{{SCRIPTS_DIR}}/continuity_test.py" $ARGUMENTS
   ```

2. Read the output. Summarize for the user:
   - Headline: pass count + MRR
   - Any **regressions** (cases that flipped from pass to fail) — flag specifically
   - Any **suspicious rank drift** (rank dropped a lot) — call it out
   - If everything passed, just say so concisely

3. **If failures exist**, suggest:
   - Whether it's a query-phrasing issue (case needs rewording)
   - Or a retrieval-quality issue (memory needs richer description, or scoring needs tuning)
   - Or a missing memory (the expected file doesn't capture what the query asks)

4. Don't over-narrate. This is a status check — the user wants the signal, not a wall of text.

## Notes
- Test cases live in `_scripts/continuity_cases.json` — add new cases there as
  the memory base grows. The shipped cases target the synthetic starter corpus
  in `examples/memory`; replace them with cases against your own memories.
- `weight: core` cases SHOULD always pass — those are the spine.
- `weight: project` cases are softer — drift here is informative but not a regression alarm unless it's a big drop.
- The runner uses `top_k=3` by default; bump on a per-case basis if needed
  (e.g. for queries with many valid expected hits).
