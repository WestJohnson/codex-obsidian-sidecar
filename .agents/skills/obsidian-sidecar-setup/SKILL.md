---
name: obsidian-sidecar-setup
description: Install, configure, update, repair, migrate, or verify Codex Obsidian Sidecar on a user's machine. Use when an agent is asked to set up the sidecar from a release, connect an Obsidian vault, register Codex hooks or Basic Memory, install a background worker, diagnose an installation, or apply a sidecar update across differing macOS or Linux environments.
---

# Obsidian Sidecar Setup

Use the package's machine-readable commands as the control plane. Adapt paths
and optional integrations to the machine, but do not hand-author hooks, service
files, or config when `obsidian-sidecar setup` can do so.

## Workflow

1. Inspect the release files and read
   [references/install-contract.md](references/install-contract.md).
2. Install the exact wheel or package version with `uv tool install`.
   Never execute a network-fetched shell script.
3. Run `obsidian-sidecar preflight` and parse the JSON.
4. Resolve only missing decisions: vault path, Codex binary, model, and which
   optional integrations the user wants.
5. Run `obsidian-sidecar setup` without `--apply`. Treat the JSON plan as the
   proposed mutation set and show it to the user.
6. Obtain explicit approval before adding `--apply`.
7. Run `obsidian-sidecar verify-install`, then `obsidian-sidecar doctor`.
8. In a fresh Codex session, have the user review and trust the Stop hook.
9. Run `obsidian-sidecar benchmark` only after hook trust and live dependencies
   are available. Require the
   [acceptance standard](../../../docs/TESTING.md#acceptance-standard).

## Safety Rules

- Never request, store, print, or move API keys during local setup.
- Never use root for the local sidecar.
- Preserve unrelated Codex hooks and Basic Memory projects.
- Keep config and service files user-only and retain generated backups.
- Do not enable cloud replication, Syncthing, SSH, or public ports as part of
  the default installation.
- Do not claim completion while a required verification check is failing.
- Do not bypass Codex hook trust.
- Keep automatic update mutation disabled. Update checks may be enabled, but an
  exact release requires explicit approval before installation.

## Updates

Run `obsidian-sidecar update-check`. If an update exists, report the current and
target versions, then run `obsidian-sidecar update --yes` only after approval.
The updater installs an exact package version from HTTPS and rolls back if the
new executable fails version verification.

For offline installation and post-update verification, follow
[Updates](../../../docs/UPDATES.md#user-flow) and the
[candidate upgrade procedure](../../../docs/RUNTIME_RELIABILITY.md#upgrade-and-rollback).

For a separately requested vault migration, follow
[Existing Vault Migration](../../../docs/KNOWLEDGE_STATE.md#existing-vault-migration).

## Recovery

On setup failure, inspect the JSON error and generated backups. Correct the
specific prerequisite and rerun the read-only plan. Do not replace the user's
entire hooks file, vault, Basic Memory configuration, or service directory.
