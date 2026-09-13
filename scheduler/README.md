# scheduler/

Windows Task Scheduler wrappers for the background lanes. Every path is
relative to this folder (`%~dp0`), so the files work wherever the repo is
cloned; set `MEMORY_HOME` in the task's environment if the memory lives
somewhere other than the parent of `_scripts/`.

| File | Cadence | What it runs |
|---|---|---|
| `drain_cron.cmd` | every 15 min | `outbox_worker.py drain --max 50` — LLM-backed jobs the hot-path hook leaves pending |
| `health_cron.cmd` | daily | `health_sentinel.py run` — data-flow invariants → `_meta/health_status.json` |
| `consolidate_cron.cmd` | weekly | `consolidate_worker.py run` — the native sleep cycle |
| `digest_cron.cmd` | weekly | `weekly_digest.py --fill-gaps 4` — narrative digest of the week's epilogues |
| `sleep_cycle_cron.cmd` | weekly | `claude -p /consolidate` then `/memory-audit` — the agentic pass on top |
| `wmi_watchdog.ps1` | every 15 min | catches a wedged WMI service live (Python 3.12 imports block on it) |

Register with `schtasks`, for example:

```
schtasks /create /tn MemorySpine-Drain /tr "%CD%\scheduler\drain_cron.cmd" /sc minute /mo 15
schtasks /create /tn MemorySpine-Health /tr "%CD%\scheduler\health_cron.cmd" /sc daily /st 07:00
```

On Linux or macOS, point cron or launchd at the same Python entry points;
the wrappers are the only Windows-specific part.
