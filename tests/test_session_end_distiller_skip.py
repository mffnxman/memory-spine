"""session_end habit-distiller draft-skip (recon t8).

The procedural-L2 designer spawns headless `claude -p` sessions whose only prompt
is one of procedural_lib's distill prompts. Each tripped session_significance and
drafted an epilogue; every draft then fed the distiller, which drafted more — the
draft pile regrew itself (219 pure-noise drafts quarantined 2026-06-12). The guard
skips drafting when EVERY user prompt in the session is a distill prompt, while
never suppressing a real (or promptless) session.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import session_end as se

# Real preview lines as session_log.py writes them (`… **User:** <preview>`).
_DISTILL_TEXT = (
    "- `15:59:42` **User:** Distil ONE reusable how-to-act heuristic from this "
    "epilogue note an AI agent saved.    NOTE:  ---  date: 2026-05-26 14:02:57"
)
_DISTILL_MISTAKE = (
    "- `07:01:03` **User:** You distil a reusable heuristic from a mistake an AI "
    "agent made and then fixed. NOTE: ..."
)
_REAL_PROMPT = (
    "- `16:15:10` **User:** can we go ahead and see our memory system and obsidian"
)


def test_distiller_prompt_signatures_match():
    assert se._is_distiller_prompt(
        "Distil ONE reusable how-to-act heuristic from this epilogue note"
    )
    assert se._is_distiller_prompt(
        "You distil a reusable heuristic from a mistake an AI agent made"
    )


def test_distiller_trigram_fallback_handles_wording_drift():
    # Wording may drift but the distinctive trigram (distil + reusable + heuristic) holds.
    assert se._is_distiller_prompt("please distill a reusable how-to heuristic here")


def test_real_prompt_is_not_distiller():
    assert not se._is_distiller_prompt(
        "can we go ahead and see our memory system and obsidian"
    )
    assert not se._is_distiller_prompt(
        "fix the reranker so it doesn't reload the model each time"
    )


def test_pure_distiller_session_skipped():
    # Every prompt is a distill prompt → suppress the draft.
    assert se._session_is_pure_distiller(_DISTILL_TEXT) is True
    assert (
        se._session_is_pure_distiller(_DISTILL_TEXT + "\n" + _DISTILL_MISTAKE) is True
    )


def test_mixed_session_not_skipped():
    # One real prompt anywhere → it's a real session, draft it.
    assert se._session_is_pure_distiller(_DISTILL_TEXT + "\n" + _REAL_PROMPT) is False
    assert se._session_is_pure_distiller(_REAL_PROMPT) is False


def test_promptless_log_not_skipped():
    # Never suppress just because the log captured no prompt (precision-first).
    assert se._session_is_pure_distiller("") is False
    assert (
        se._session_is_pure_distiller(
            "- `16:15` Wrote `foo.md`\n- `16:16` Edited `bar.md`"
        )
        is False
    )
