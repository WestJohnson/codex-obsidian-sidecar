# Runtime reliability (0.6.4)

## Operating contract

- A busy shared lease means **defer and retry**, not a failed capture. Queue
  attempts and the successful-backup clock must not advance on deferral.
- Same-machine workers use an OS-held lock, released on process exit. Stop
  older workers before upgrading: a fresh legacy directory lock is respected,
  but mixed-version long-running workers must not overlap the installation.
- Syncthing is eventually consistent. Shared files provide cooperative fencing,
  not a linearizable distributed mutex. Do not break live leases, force
  simultaneous writers on offline replicas, or claim this change guarantees
  distributed mutual exclusion. Cloud maintenance retains its sync-settle checks.

## Incomplete captures

The Stop hook stays fast and always returns zero so it cannot interrupt Codex.
When the transcript path is absent, only session/turn IDs, working directory,
capture time, and local Codex-home routing metadata enter private state.
The worker searches `sessions` and `archived_sessions` beneath that home for an
exact UUID filename and matching session metadata. Working directory must match
when provided. Multiple matches, mismatches, and out-of-tree symlinks are rejected.
No transcript contents or hook message bodies enter diagnostics.

`capture-pending/` holds unresolved records; `capture-recovered/` retains their
recovery audit after queuing; `capture-failed/` retains invalid hooks and entries
unresolved after 24 hours. Pending entries older than 30 minutes or any failed
captures trigger `capture-incomplete`. Recovery failures are never called
successfully processed. Older `capture-skips.log` entries lack session identity
and cannot be automatically reconstructed from timestamps alone.

`worker-status.json` records the most recent timer result and the start of
continuous deferral, without transcript content or exception messages. Errors,
30-minute silence, and 30-minute continuous deferral are actionable. Repeated
alerts use the existing cooldown. Unresolved capture failures and runtime
problems cap doctor health at 79; use fresh `alert-status` alongside the periodic
health report. A closed laptop will legitimately report a stale local worker
until its next successful tick. The cloud-only role has no local-worker heartbeat.

## Upgrade and rollback

Basic Memory 0.23 changed `status --wait` to an observation-only compatibility
command. Sidecar detects that response and runs a synchronous search-only index
pass instead of assuming the index is ready. Read-only inspection labels that
API `observation-only`; `doctor` runs actual indexing and the live benchmark
independently verifies retrieval. The Obsidian desktop CLI is optional on Linux.

1. Capture package version, config, hooks, service state, queue counts, and vault
   backup. Stop only the Sidecar timer while idle; leave Codex and sync running.
2. Install the exact tested wheel on each host, preserving the prior wheel and
   config. A rolling upgrade is safe only with local writers kept idle during
   package replacement; no coordination schema migration is introduced.
3. Use package-owned `setup` for config or service changes. Existing freshness
   values are preserved. `setup --freshness-project-days 90` explicitly changes
   the project review interval; without the flag a new install still defaults
   to 30 days. It does not re-verify notes or rewrite existing freshness dates.
4. Verify installation, run deterministic tests and the isolated live benchmark,
   confirm actual Basic Memory retrieval, sync convergence, clean queues, current
   backups, then observe more than one timer tick. Do not mistake an inactive
   successful oneshot for a disabled timer.
5. To roll back, wait for idle, stop the timer, reinstall the saved wheel and
   restore only the backed-up config/service files changed by this upgrade.
   Retain capture recovery directories: older versions ignore them but they are
   not disposable. Restart the timer and verify service and retrieval again.
