@echo off
REM digest_cron.cmd - weekly digest lane.
REM --fill-gaps 4: self-healing - fills any missing digest in the last 4 completed
REM weeks instead of assuming last-week, so a killed run gets retried next week
REM instead of the week being skipped forever.
REM Kill-safe: weekly_digest.py writes the skeleton chapter FIRST and the LLM
REM payload upgrades it in place - a killed run degrades to a skeleton, not a gap.
REM Check _meta\digest_cron.log if a run looks off.
set "CLAUDE_BRAIN_BG=1"
cd /d "%~dp0.."
python -X utf8 weekly_digest.py --fill-gaps 4 >> "%~dp0..\..\_meta\digest_cron.log" 2>&1
