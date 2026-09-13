@echo off
REM health_cron.cmd - daily brain health sentinel.
REM Runs cheap data-flow invariants (observations flowing, outbox drained,
REM sleep cycle fresh, promotion backlog bounded, embeddings complete,
REM hallucination audit clean, db sizes) and writes _meta\health_status.json.
REM boot_ritual.py surfaces any warnings at the next session start; if this
REM task itself dies, the stale status file triggers a live re-check at boot,
REM so the sentinel's own death gets noticed too.
set "CLAUDE_BRAIN_BG=1"
cd /d "%~dp0.."
python -X utf8 health_sentinel.py run >> "%~dp0..\..\_meta\health_cron.log" 2>&1
