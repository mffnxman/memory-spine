---
name: Bash tool on Windows takes POSIX syntax
description: On the Windows laptop the Bash tool runs Git Bash, so use POSIX commands (ls, cat, sed); PowerShell cmdlets like Get-ChildItem fail there, and Python output with emoji needs PYTHONIOENCODING=utf-8
type: procedural
weight: medium
created: 2026-02-14
related: user_profile.md
---

Two Windows-specific habits that keep paying off:

1. The Bash tool is Git Bash. `Get-ChildItem`, `Get-Content`, and backtick escapes are PowerShell and will error. Use `ls`, `cat`, `sed -n`, forward slashes, and `$VAR`. If a PowerShell cmdlet is really needed, use the PowerShell tool instead.
2. Python scripts that print check marks or emoji crash with `UnicodeEncodeError` under the default console code page. Set `PYTHONIOENCODING=utf-8` or reconfigure `sys.stdout` at the top of the script.

Both were learned the slow way, one failed command at a time, and both are cheap to remember.
