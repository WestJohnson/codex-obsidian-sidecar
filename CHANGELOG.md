# Changelog

## 0.6.4 - 2026-09-07

- Defer local-writer contention without crashing or spending capture retries;
  distinguish local and cloud leases and fail closed on malformed lease data.
- Keep backup checkpoints due after contention or failure. Use process-owned
  locks so long-running local workers do not lose their lock after 15 minutes.
- Retain incomplete hook routing metadata privately and recover only from an
  exact session ID and matching transcript header. Never guess the newest
  session. Surface unresolved captures after 30 minutes and preserve failures
  after a day; no raw hook text is retained.
- Include unresolved captures and stale, failed, or persistently deferred
  workers in actionable alerts and health scoring, with a maintenance-failure
  ceiling below 80. Normal short contention is not a failure.
- Preserve existing freshness policy during setup; expose an explicit project
  review interval override. Enable disabled launchd jobs and avoid killing the
  RunAtLoad worker with a second forced start.
- Detect Basic Memory 0.23's observation-only status and explicitly complete a
  search-only index pass. Do not mistake its compatibility `--wait` for index
  readiness. Keep desktop Obsidian CLI optional in Linux health checks.
- Require every local benchmark case to pass and retain acceptance results
  when report publication is deferred; allow publication to be retried alone.
- Keep daily maintenance independent of health refreshes and retry deferred
  nightly maintenance through the existing reconnect timer, even without a
  staged report. Preserve sync-error reporting during real failures.
- Retain unfinished transcript tails, resolve transcript-only session identity
  before checkpoint lookup, and distinguish active from interrupted indexing.
- Include the testing, operations, and cloud-sync guides in offline bundles.
- Preserve real failures through contention, serialize cloud and reconnect
  status transitions, and resolve processing alerts after successful manual
  recovery without clearing unrelated failures. Publication-only retries do
  not trigger fresh analysis when a staged report becomes stale.
- Compute grouped capture cutoffs in one transcript snapshot and accept complete
  long routing headers without retaining their instructions as model evidence.

## 0.6.3 - 2026-08-19

- Made the live benchmark use a stable schema-safe working-directory name so
  model curation cannot inherit an invalid random underscore from `tempfile`.
- Made installed integration verification honor the absolute Sidecar command
  used to launch the benchmark when noninteractive SSH does not load the
  user's local-bin `PATH`.

## 0.6.2 - 2026-08-19

- Added native Linux desktop alerts through `notify-send` while retaining
  macOS Notification Center delivery through `osascript`.
- Persisted actionable alert state when a platform notifier is unavailable so
  a missing desktop integration cannot fail the worker or retry every minute.
- Accepted Arch systemd's numeric zero main-code for a successful completed
  oneshot when the result, status, and start timestamp independently pass.

## 0.6.1 - 2026-07-28

- Bounded long-thread checkpoint evidence to a recent working set so saturated
  decision lists retain durable vault history while reserving capacity for new
  turns.
- Added deterministic output compaction that removes only the oldest
  checkpoint-only carry-forward items when a curator exceeds a schema list
  limit. Current-turn evidence is never truncated, so saturated long threads
  recover without retries or operator cleanup.
- Clarified curator list limits and added regression coverage for checkpoint
  compaction after the 20-decision ceiling is reached.
- Excluded superseded and rejected decisions from duplicate detection and
  migration counts so resolved review records do not re-enter the review queue.
- Classified recommendations, informational records, and ambiguous duplicate
  candidates as non-authoritative freshness state. They remain searchable but
  never age into a health penalty or required maintenance backlog.

## 0.6.0 - 2026-07-26

- Replaced PyPI publication and package-index mutation with a self-hosted HTTPS
  release channel, exact wheel hashes, same-origin artifact enforcement, and a
  verified rollback wheel fetched before mutation.
- Typed canonical records as operator decisions, implemented choices,
  recommendations, observations, or legacy-unclassified items, with
  evidence-derived authority and promotion status.
- Added conservative pre-promotion duplicate detection: high-confidence
  wording variants reuse a canonical record, while probable duplicates are
  held in a project review index.
- Split decision impact into direct, inferred, and related edges; corrected
  checkpoint artifact association so retained decisions no longer inherit
  every retained artifact.
- Added managed project-hub rollups for current phase, latest outcome, resume
  context, verification count, and ranked open work.
- Added sanitized model, provider, effort, and harness provenance to packets,
  private checkpoints, and session notes when the transcript supplies it.
- Extended plan-first knowledge migration to classify legacy decisions,
  conservatively quarantine unknown authority, flag possible duplicates,
  tighten legacy impact edges, refresh project hubs, and remain idempotent.

## 0.5.1 - 2026-07-21

- Added safe, stable topic deduplication and a 12-topic metadata cap before
  strict validation while leaving malformed topics and semantic lists fail-closed.
- Made the curator's topic cardinality contract explicit to prevent avoidable
  model retries.
- Added checkpoint-proven reconciliation for failed events whose exact Stop
  boundaries are already covered by a newer committed transcript cursor.
- Kept reconciled failure records with an auditable
  `superseded-by-checkpoint` disposition instead of deleting or reprocessing
  them.
- Clarified queue alerts as curation failures rather than vault-sync failures
  and persisted the final retry count before quarantine.

## 0.5.0 - 2026-07-20

- Added private, versioned per-session checkpoints so long Codex threads reuse
  validated durable state while curating only newly appended messages.
- Added byte-cursor transcript deltas, bounded multi-chunk continuation, note
  seeding for existing sessions, and recovery fallback for missing or corrupt
  checkpoints.
- Bounded delayed processing to the completed Stop event so a new in-progress
  turn remains untouched until its own Stop hook arrives.
- Restricted chronology rejection to contradictions inside the structured
  curation, avoiding false removal of explicit caveats from mixed success and
  remaining-work sentences.
- Made checkpoint advancement transactional with validated vault writes and
  preserved stable early decisions, dispositions, current phase, resume
  context, and artifact references across turns.
- Added content-free curator usage telemetry and checkpoint error logging plus
  reversible configuration flags for both features.
- Added deterministic coverage for delta compaction, cursor safety, fallback,
  checkpoint security, and long-thread continuity.
- Extended the live complete-pipeline critical gate to require a private
  checkpoint with a full transcript cursor and mode-0600 permissions.

## 0.4.1 - 2026-07-20

- Made same-session note moves reference-safe across date, confidence-route,
  and project-slug changes by retargeting exact managed references before the
  prior note is removed.
- Added fail-safe move validation: unmanaged references block removal and leave
  both session copies recoverable for operator review.
- Restricted duplicate cleanup to managed work-session notes and added
  read-back verification for canonical reference repairs.
- Made missing local `vault:` freshness and verification sources critical
  health findings instead of silently treating their envelopes as current.

## 0.4.0 - 2026-07-19

- Added computed freshness envelopes for canonical project hubs, decisions,
  runbooks, and operational instructions, with configurable review windows.
- Added deterministic canonical project identity reuse for exact source working
  directories to reduce duplicate project hubs.
- Added canonical decision records with stable IDs, typed affected targets,
  source provenance, project indexes, and conservative exact-text reuse.
- Added read-only `freshness-status` and `decision-impact` commands plus a
  plan-first `knowledge-migrate` command for existing managed vaults.
- Added `_System/Knowledge/latest.md` as a human-visible freshness, decision,
  and project-identity status artifact.
- Extended health checks to surface missing, invalid, and review-due freshness
  without rewriting downstream notes or repository artifacts.

## 0.3.0 - 2026-07-16

- Ignore hook events without transcript files and consume legacy invalid events
  without creating persistent failed-queue alerts.
- Validate cloud benchmark backups as intact recovery points within the 48-hour
  RPO while retaining exact-current validation during backup creation.
- Report an unpublished PyPI candidate as `not-published` instead of a failed
  update check.
- Added machine-readable preflight, dry-run setup, transactional apply, and
  structural installation verification.
- Added portable macOS launchd and Linux user-systemd setup.
- Added an open Agent Skill and installation contract for agent-driven setup.
- Added explicit exact-version update checks, verification, and rollback.
- Added production release export, checksums, CI attestations, and Trusted
  Publishing workflow.
- Removed personal paths and host assumptions from public configuration.

## 0.2.0 - 2026-07-14

- Added fenced cloud maintenance, reconnect reconciliation, alerting, cost
  limits, and a 100-point cloud benchmark.

## 0.1.0 - 2026-07-13

- Added validated Codex transcript capture, Luna curation, Obsidian writes,
  Basic Memory indexing, health checks, and local benchmark coverage.
