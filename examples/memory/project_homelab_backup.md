---
name: Home lab NAS backup
description: Nightly restic backup of the NAS to the offsite mini PC, 14 dated generations kept, checked by a weekly restore drill after the March near-miss
type: project
weight: medium
created: 2026-03-08
event_date: 2026-03-02
related: decisions.md
---

The NAS is backed up every night at 02:00 by restic to the offsite mini PC over a WireGuard tunnel. Retention is 14 dated generations plus one monthly, pruned by the same job. Mirroring alone was rejected because a mirror faithfully copies a deletion or a ransomware wipe on the next run; dated generations do not.

The March near-miss: the job had been exiting non-zero for nine days because the tunnel key rotated, and nothing surfaced it. Two fixes came out of that: the job now writes a done-marker line that a health check keys on (never the start line, never file mtime), and a weekly restore drill pulls one random file back and diffs it.

Status: running. Next improvement, not started: alert when the newest generation is older than 48 hours.
