# wmi_watchdog.ps1 — probes WMI with a hard 10s timeout; on stall, captures
# commit charge + top processes SO THE CULPRIT IS CAUGHT LIVE instead of being
# diagnosed blind after the wedge (WMI stuck-stop -> SCM timeouts -> hours of
# forensics from event logs alone).
# Healthy runs are silent (attention is a budget); a stall writes
# _meta/wmi_stall_alert.json which health_sentinel + boot surface.
# Schedule it every 15 min (Windows only; Python 3.12's platform module
# queries WMI at import time, so a wedged WMI freezes every hook).
# _meta lives two levels up from scheduler\ unless MEMORY_HOME overrides it.
$meta = if ($env:MEMORY_HOME) { Join-Path $env:MEMORY_HOME '_meta' } else { Join-Path $PSScriptRoot '..\..\_meta' }
$log = Join-Path $meta 'wmi_watchdog.log'
$alert = Join-Path $meta 'wmi_stall_alert.json'

$job = Start-Job { Get-CimInstance Win32_OperatingSystem -ErrorAction Stop | Out-Null; 'ok' }
$done = Wait-Job $job -Timeout 10
$ok = $false
if ($done) {
    try { $ok = (Receive-Job $job -ErrorAction Stop) -eq 'ok' } catch { $ok = $false }
}
if ($ok) {
    # Healthy: clear a stale alert so the sentinel goes quiet again.
    if (Test-Path $alert) {
        Remove-Item $alert -Force
        Add-Content $log "[$(Get-Date -Format s)] recovered: WMI answering again, alert cleared"
    }
} else {
    Stop-Job $job -ErrorAction SilentlyContinue
    $commit = $null
    try { $commit = [long](Get-Counter '\Memory\Committed Bytes' -ErrorAction Stop).CounterSamples[0].CookedValue } catch {}
    $top = Get-Process | Sort-Object WS -Descending | Select-Object -First 10 Name, Id, @{n = 'ws_mb'; e = { [math]::Round($_.WS / 1MB) } }
    $rec = [ordered]@{
        ts           = (Get-Date -Format s)
        commit_bytes = $commit
        top_processes = @($top | ForEach-Object { @{ name = $_.Name; pid = $_.Id; ws_mb = $_.ws_mb } })
    } | ConvertTo-Json -Depth 4 -Compress
    Add-Content $log "[$(Get-Date -Format s)] WMI STALL: $rec"
    Set-Content $alert $rec
}
Remove-Job $job -Force -ErrorAction SilentlyContinue
