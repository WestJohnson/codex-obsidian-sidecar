# Changelog

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
