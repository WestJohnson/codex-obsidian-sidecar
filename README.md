# Codex Obsidian Sidecar

An unofficial, local-first memory service that turns completed Codex work into
validated, searchable Obsidian notes. The active Codex turn only queues a small
event. A debounced worker extracts durable facts, writes atomic Markdown notes,
and indexes them through Basic Memory.

The package includes a portable installer, an open Agent Skill for agent-driven
setup, a 100-point live benchmark, and an optional fenced cloud-maintenance
mode. Cloud operation is disabled by default and is not required for local
memory.

## Why It Exists

- Durable work history stays in a normal Obsidian vault.
- Codex remains the primary operator; a cheap curation pass handles summaries.
- Repositories remain authoritative for source and project documentation.
- Raw transcripts, tool output, internal reasoning, and credentials stay out of
  the vault.
- Setup and updates are deterministic even when an AI agent guides the process.

## Quick Start

Install an exact public release:

```sh
uv tool install 'codex-obsidian-sidecar==VERSION'
obsidian-sidecar preflight
```

Generate a read-only setup plan:

```sh
obsidian-sidecar setup \
  --vault "$HOME/Documents/Obsidian Vault" \
  --codex-bin "$(command -v codex)"
```

Review the plan, rerun it with `--apply`, then verify:

```sh
obsidian-sidecar verify-install
obsidian-sidecar doctor
obsidian-sidecar benchmark
```

For agent-driven setup, open the extracted release source and ask the agent to
use `$obsidian-sidecar-setup`. See [Installation](docs/INSTALL.md).

## Safety Model

- Tool output, internal reasoning, developer instructions, and raw transcripts
  are never copied into the vault.
- Likely credentials are redacted before model processing and blocked before
  writes, Git commits, archive creation, or backup rotation.
- The curator must cite packet evidence IDs for every substantive item.
- Newer evidence supersedes stale unresolved findings; contradictions and
  missing local artifact targets are quarantined.
- Canonical project hubs, decision records, and runbooks carry freshness
  envelopes whose current state is computed from verification and review dates.
- Significant high-confidence decisions become deterministic records under
  `40 Decisions`; impact previews are read-only and never rewrite downstream
  notes or repository artifacts.
- Canonical local file links are preserved deterministically.
- Notes use atomic writes and stable session-derived paths.
- Low-confidence output goes to `00 Inbox/Needs Review`.
- Invalid output goes to `_System/Quarantine` and retries at most three times.
- The curator subprocess runs ephemerally, read-only, without user rules,
  hooks, or network-dependent tools.
- Same-host runs use an OS lock; cross-device writers use synchronized leases.
- Setup requires no root access, stores no secrets, and opens no network port.
- Update checks are read-only. Exact-version installation always requires
  explicit approval and includes version verification plus rollback.

## Commands

```sh
obsidian-sidecar preflight          # machine-readable capability report
obsidian-sidecar setup ...          # read-only plan; add --apply to mutate
obsidian-sidecar verify-install     # config, hook, service, retrieval checks
obsidian-sidecar capture-hook       # hook JSON on stdin
obsidian-sidecar process --force    # process queued sessions now
obsidian-sidecar daemon-once        # background worker entry point
obsidian-sidecar doctor --backup    # health report and local Git backup
obsidian-sidecar benchmark          # weighted live end-to-end benchmark
obsidian-sidecar update-check       # read-only release check
obsidian-sidecar update --yes       # exact-version update with rollback
obsidian-sidecar cloud-doctor       # optional sync and lease health
obsidian-sidecar cloud-benchmark    # optional deployed cloud score
obsidian-sidecar cloud-reconcile    # publish a matching offline stage
obsidian-sidecar alert-status       # actionable alert conditions only
obsidian-sidecar freshness-status   # computed freshness; no note mutation
obsidian-sidecar decision-impact ID # read-only decision blast radius
obsidian-sidecar knowledge-migrate  # read-only migration plan; add --apply
```

## Runtime Paths

- Configuration: `~/.config/codex-obsidian-sidecar/config.json`
- Queue and logs: `~/.local/share/codex-obsidian-sidecar/`
- Vault: selected during setup
- Global Codex hook: `~/.codex/hooks.json`
- Installed runtime: managed by `uv tool`
- macOS service: `~/Library/LaunchAgents/<configured-service-label>.plist`
- Linux service: `~/.config/systemd/user/<configured-service-label>.*`

## Acceptance Standard

The benchmark is scored out of 100. A pass requires at least 80 points and all
critical safety gates. Live Codex, Obsidian CLI, Basic Memory, hook trust, and
background-service checks are reported separately so deterministic unit tests
cannot masquerade as a working installation.

## Documentation

- [Installation](docs/INSTALL.md)
- [Operations](docs/OPERATIONS.md)
- [Testing](docs/TESTING.md)
- [Knowledge State](docs/KNOWLEDGE_STATE.md)
- [Updates](docs/UPDATES.md)
- [Optional Cloud Sync](docs/CLOUD_SYNC.md)
- [Security](SECURITY.md)

## Status

Version `0.4.0` is a production candidate, not a hosted service. Before a
public release, configure the repository, GitHub release environment, and PyPI
Trusted Publisher described in [Updates](docs/UPDATES.md). Until publication,
`update-check` reports `not-published` and exact offline wheel installation is
the supported update path.
