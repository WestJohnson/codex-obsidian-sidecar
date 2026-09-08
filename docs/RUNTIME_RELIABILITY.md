# Runtime reliability (0.6.4)

## Operating contract

- A busy shared lease means **defer and retry**, not a failed capture. Queue
  attempts and the successful-backup clock must not advance on deferral.
  A failed backup also leaves the checkpoint due for the next timer tick;
  only a successful commit or a clean Git tree advances that clock.
- Same-machine workers use an OS-held lock, released on process exit. Stop
  older workers before upgrading: a fresh legacy directory lock is respected,
  but mixed-version long-running workers must not overlap the installation.
- Syncthing is eventually consistent. Shared files provide cooperative fencing,
  not a linearizable distributed mutex. Do not break live leases, force
  simultaneous writers on offline replicas, or claim this change guarantees
  distributed mutual exclusion. Cloud maintenance retains its sync-settle checks.
- Daily local maintenance is scheduled from `maintenance-success.json`, not
  the frequently refreshed health observation. Failed or deferred work stays
  due. The first upgraded tick without this completion record runs maintenance.
- Deferred cloud maintenance remains due in private state. The existing
  reconnect timer retries it even without a staged report. Ordinary writer
  overlap while replication progresses does not consume failure retries;
  actual sync errors and conflicts remain failures.

## Incomplete captures

The Stop hook stays fast and always returns zero so it cannot interrupt Codex.
When the transcript path is absent, only session/turn IDs, working directory,
capture time, and local Codex-home routing metadata enter private state,
alongside fixed event/reason labels. Records are written atomically with mode
`0600` in private capture directories.
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

Recovery runs during `process` and local timer ticks. Legacy queued events
without a usable transcript path or timezone-aware capture timestamp move to
`failed/` instead of being counted as processed. For retained failures, inspect
only the routing metadata, correct the exact-session routing problem, and
preserve the original capture time and audit record before retrying. Do not
substitute the newest transcript or mark a queued recovery as completed
curation. Normal retry handling is in [Operations](OPERATIONS.md#recovery).

Capture retirement requires a complete transcript cutoff covered by the committed
checkpoint, or by the validated packet cursor when checkpoints are disabled.
Baseline and recovery packets derive evidence, cursor, and incomplete
tail status from the same read, retaining the existing last-16-message and
character limits. An unfinished JSONL tail remains queued until the complete
record is covered, including when it finishes while a packet is being built.
Transcript-only hooks resolve their canonical session header and missing working
directory before checkpoint selection and retirement. Hooks with an explicit
session ID also recover a missing working directory from the validated header.
Canonical identity is resolved before grouping and debounce; each capture keeps
its original cutoff. Incremental packets retain that Git and artifact base while
preserving explicit hook routing values.
Retirement and failed-event reconciliation compute all cutoffs for a transcript
in one bounded read snapshot. An incomplete final record leaves the affected
captures pending, even if that record finishes during the scan.

## Health And Alerts

`worker-status.json` records the most recent timer result and the start of
continuous deferral, without transcript content or exception messages. Errors,
30-minute silence, and 30-minute continuous deferral are actionable. Timer
ticks retain unresolved operation failures while running or deferred. A backup
failure clears only after a successful backup; unrelated successful work does
not clear it. Repeated alerts use the existing cooldown. Stalled or failed
captures, runtime problems, and indexing failures cap doctor health at 79;
use fresh `alert-status` alongside
the periodic health report. A closed laptop will legitimately report a stale
local worker until its next successful tick. A missing status file is not itself
reported as stale; confirm the service has run using the
[platform checks](OPERATIONS.md#routine-checks).

`cloud-maintenance-status.json` records cloud errors and continuous contention.
A prior cloud failure remains visible through subsequent deferrals until a
successful connected or offline-staged run clears it. A concurrent contention
observer cannot overwrite a newer failure: status updates are serialized, and
operation results are recorded before releasing the process lock. Cloud
maintenance does not use the local worker's silence threshold because it runs
on a different schedule. Cloud-only deployments do not require a local-worker
heartbeat.

## Search Indexing

Basic Memory 0.23 changed `status --wait` to an observation-only compatibility
command. When its JSON response contains an `observed_files` list, Sidecar runs
`reindex --project <configured-project> --search` synchronously instead of
assuming the index is ready. This requests search indexing without embeddings.
Read-only inspection labels that API `observation-only`; local `doctor` runs
actual indexing, and the [live benchmark](TESTING.md#live-suite) independently
verifies retrieval under the platform's integration requirements.

`index-status.json` records pending indexing before capture writes to the vault,
without exception messages. Pending or failed indexing remains actionable even
if the worker exits after the note, checkpoint, and queue retirement are committed.
Only confirmed indexing success clears that obligation.
Capture writes and every indexing caller share a local OS-held indexing lock.
An index operation completes its state update before a later capture marks new
work pending, and indexing waits for an in-progress capture write to finish.
The lock is released on process exit and is separate from the shared writer lease.
The local worker retries indexing on an idle tick under its writer lease,
without repeating curation or advancing the checkpoint. A failed full search
rebuild retains full mode for its retry. `process` exits nonzero when indexing
fails; `daemon-once` can exit zero after a handled failure, so inspect its result,
worker status, and `alert-status` as well as the service exit code.
Fresh running indexing with a live local owner is not reported as failed.
Missing owners and operations still running after ten minutes are actionable,
so interrupted jobs remain eligible for recovery.

## Upgrade And Rollback

Verify candidates before promotion. Public releases follow the signed-tag,
CI-provenance, immutable-artifact, and update-index procedure in
[Updates](UPDATES.md#maintainer-flow); installation alone does not publish them.
macOS setup reads the prior launchd disabled state before changing files. If
service reload fails, setup restores that state along with the file snapshots.

1. Capture package version, config, hooks, service state, queue counts, and vault
   backup. Suspend only Sidecar scheduling and wait for active work to finish:
   unload the idle launchd job on macOS, stop the user timer on Linux, and stop
   both maintenance and reconnect timers on the cloud host. Leave Codex and
   sync running; do not start manual writers during package replacement.
2. Install the exact tested wheel on each host, preserving the prior wheel and
   config. A rolling upgrade is safe only with local writers kept idle during
   package replacement; no coordination schema migration is introduced.
3. On local hosts, use package-owned `setup` for config or service changes;
   review [freshness policy during setup](INSTALL.md#freshness-policy-during-setup).
   Keep unrelated hooks and the current queue intact. Cloud deployment follows
   [Cloud Sync](CLOUD_SYNC.md#update-the-cloud-runtime), preserving its cloud role.
4. Resume the saved schedules using the [local worker reload](OPERATIONS.md#recovery)
   when needed, then complete the [release checks](TESTING.md#release-checklist)
   on local hosts and [cloud checks](CLOUD_SYNC.md#routine-checks) on the server.
   Confirm actual Basic Memory retrieval locally, sync convergence, clean queues,
   and current backups, then observe more than one timer tick. Do not mistake
   an inactive successful oneshot for a disabled timer.
5. To roll back, suspend the same schedules while idle, reinstall the saved
   wheel, and restore only the backed-up config/service files changed by this upgrade.
   Retain capture recovery directories: older versions ignore them but they are
   not disposable. Preserve current queues and checkpoints rather than replacing
   them with a pre-upgrade snapshot. Resume the saved schedules and verify
   service and retrieval again.
