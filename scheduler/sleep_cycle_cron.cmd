@echo off
REM sleep_cycle_cron.cmd - weekly memory sleep cycle via headless `claude -p`.
REM Runs /consolidate then /memory-audit as full agentic passes - the judgment
REM layer on top of the native consolidate_worker pass (consolidate_cron.cmd).
REM --permission-mode acceptEdits: file edits auto-approve; make sure python is
REM on the allowlist so the commands' shell-outs run without prompts
REM (headless -p auto-denies anything that would prompt).
REM Requires the `claude` CLI on PATH and the commands installed
REM (python install_hooks.py --commands).
set "CLAUDE_BRAIN_BG=1"
cd /d "%USERPROFILE%"
set LOG=%~dp0..\..\_meta\sleep_cycle_cron.log
echo [%date% %time%] ===== sleep cycle start >> "%LOG%"
claude -p "/consolidate" --permission-mode acceptEdits --max-turns 40 >> "%LOG%" 2>&1
echo [%date% %time%] ----- consolidate pass done, starting audit >> "%LOG%"
claude -p "/memory-audit" --permission-mode acceptEdits --max-turns 40 >> "%LOG%" 2>&1
echo [%date% %time%] ===== sleep cycle done >> "%LOG%"
