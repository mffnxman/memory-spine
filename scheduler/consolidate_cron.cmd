@echo off
REM consolidate_cron.cmd - weekly full consolidation sleep-cycle.
REM Runs the real sleep-cycle entry point (consolidate_worker.py run): near-dup,
REM weight-rerank, stale-functional, prefetch warm, procedural designer, genesis,
REM observer candidates, provenance prune. LLM synthesis falls back to the
REM subscription (`claude -p`) provider when no ANTHROPIC_API_KEY is set.
REM Logs stdout/stderr - a silent scheduler action is how bugs hide for weeks.
REM Check _meta\consolidate_cron.log if a run looks off.
REM CLAUDE_BRAIN_BG=1: nested claude -p runs inherit this so a Stop hook can
REM suppress desktop notifications for background work.
REM Paths are relative to this file: %~dp0 is scheduler\, so ..\ is _scripts\
REM and ..\..\_meta is the memory's _meta dir (override with MEMORY_HOME).
set "CLAUDE_BRAIN_BG=1"
cd /d "%~dp0.."
python -X utf8 consolidate_worker.py run >> "%~dp0..\..\_meta\consolidate_cron.log" 2>&1
