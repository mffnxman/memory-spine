@echo off
REM drain_cron.cmd - scheduled out-of-band drainer for the memory event bus.
REM
REM WHY: the PostToolUse Edit/Write hook runs in hot_path mode and deliberately
REM LEAVES the LLM-backed job kinds (consolidate / reflection / importance_score)
REM pending, so a file edit never blocks on a multi-minute job. This task drains
REM those heavy jobs off the tool path. Run it every 15 minutes.
REM
REM LLM policy: background synthesis uses the subscription (`claude -p`) fallback
REM when no API key is configured. To forbid the nested claude -p path, add
REM   set "MEMORY_DISABLE_SUBSCRIPTION_PROVIDER=1"
REM on its own line below.
set "CLAUDE_BRAIN_BG=1"
cd /d "%~dp0.."
python outbox_worker.py drain --max 50 >> "%~dp0..\..\_meta\drain_cron.log" 2>&1
