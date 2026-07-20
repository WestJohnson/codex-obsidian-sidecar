# Cloud Sync And Overnight Maintenance

## What Is Running

The VPS is a headless Obsidian vault service, not a Linux Obsidian GUI. Obsidian
vaults are ordinary Markdown directories, so the server keeps a complete
plaintext replica at `/srv/obsidian-vault` where scheduled workers can inspect
and analyze durable notes without depending on the Mac. Synced output is written
only while both replicas are connected; offline output is staged on the VPS.

Syncthing 2.1.2 synchronizes that directory with
`~/Documents/Obsidian Vault` on the Mac. Both folders are Send & Receive. The
Mac initiates a direct TLS 1.3 connection to the configured cloud host on port
22000; the Syncthing GUI and REST API remain on `127.0.0.1:8384` on each
machine.

Self-hosted LiveSync/CouchDB was not selected. It is useful for synchronizing
Obsidian clients, but its headless filesystem client is still emerging. The VPS
worker needs normal Markdown files for validation, Git checkpoints, backups,
and bounded model evidence.

## Runtime Map

| Component | Mac | Cloud VPS |
| --- | --- | --- |
| Vault | `~/Documents/Obsidian Vault` | `/srv/obsidian-vault` |
| Syncthing state | `~/Library/Application Support/Syncthing` | `/var/lib/obsidian-sync/.local/state/syncthing` |
| Sidecar state | `~/.local/share/codex-obsidian-sidecar` | `/var/lib/obsidian-cloud` |
| Sidecar config | `~/.config/codex-obsidian-sidecar/config.json` | `/etc/obsidian-cloud/config.json` |
| Sidecar code | `~/Documents/codex-obsidian-sidecar` | `/opt/obsidian-cloud/app` |
| Python runtime | project `.venv` and local tool | `/opt/obsidian-cloud/venv` |
| Backups | local Git and Syncthing versions | `/var/backups/obsidian-vault` and server Git |
| Scheduler | launchd sidecar worker | nightly maintenance plus `obsidian-cloud-reconnect.timer` |

The server service runs as the unprivileged `obsidian-sync` user. Its OpenRouter
key is loaded by systemd from `/etc/obsidian-cloud/openrouter.env`, which is
owned by root with mode `0600`. The key is not stored in the vault, Git, project
configuration, logs, or model packet.

## Security And Consistency Controls

- Devices must mutually authorize each other's Syncthing certificate ID.
- Public discovery, local discovery, relays, NAT traversal, telemetry, and QUIC
  are disabled. Only TCP 22000 is allowed through UFW.
- Both REST APIs bind to loopback only.
- The systemd worker is unprivileged and sandboxed with no capabilities,
  restricted namespaces and address families, protected kernel and host state,
  and explicit writable paths only.
- `.git`, Obsidian workspace state, trash, versions, and quarantine files are
  excluded from synchronization. Each machine maintains an independent Git
  repository.
- Restorable backups include durable `.obsidian` settings such as appearance,
  graph, and enabled core plugins, while excluding workspace and cache state.
- Staggered Syncthing history retains received prior versions for one year.
- Server backups scan every candidate file for likely credentials before an
  archive or retention change. The exact scanned bytes are written into the
  archive, its members must exactly match a SHA-256 manifest, and the candidate
  must restore and validate before publication or rotation. The newest 30 are
  retained.
- Backup creation requires an exact match to the then-current durable vault.
  Recurring acceptance restores and checksum-verifies the newest recovery point
  and requires it to be no more than 48 hours old; later daytime work is
  protected by Syncthing and independent Git checkpoints until the next nightly
  archive.
- An OS-level process lock serializes timer and manual runs on the VPS.
- A shared cloud lease pauses the local sidecar. A shared local-writer lease
  blocks cloud maintenance. The cloud worker rechecks writers and conflicts
  after the lease reaches the peer, closing the eventual-consistency race.
  Malformed leases fail closed.
- The cloud run refuses pending sync bytes, folder errors, conflict copies,
  active writers, apparent secrets, or critical vault-health failures.
- Luna receives bounded note excerpts only. It has no filesystem tools and can
  only return strict JSON. Its output is rendered to `_System/Cloud Reports`;
  it cannot choose a path or modify a source note.
- Strict report output is capped at 3,000 completion tokens so the largest
  allowed schema remains valid without unbounded generation.
- Basic Memory permalinks are emitted by the server writer so round-trip
  indexing does not leave the server Git tree dirty.
- Connected VPS health is written to `_System/Health/cloud-latest.md` and a
  cloud-prefixed history file. It never overwrites the Mac's `latest.md`, so
  local Obsidian CLI and Basic Memory status remain authoritative on the Mac.
- The OpenRouter key is queried through `/api/v1/key` before a model call. A
  call fails closed when its configured `$0.10` reserve would cross the `$0.25` UTC-day
  or `$5.00` UTC-month ceiling, or when the key's own remaining credit is below
  the reserve. The provider's key-level credit limit remains the final account
  guardrail.

Manual edits made directly in Obsidian cannot participate in the lease protocol.
The default cloud worker therefore creates derived reports rather than rewriting
source notes. If a person edits the same derived file concurrently, Syncthing
creates a visible `sync-conflict` copy and the next cloud run stops.

## Nightly Workflow

The timer runs at 13:30 UTC, which is 03:30 HST, with up to ten minutes of
random delay. It is persistent, so a missed run starts after the VPS comes back.

Each run:

1. Requires connected, complete replicas, no conflicts, and no active writer.
2. Acquires and synchronizes the cloud-maintenance lease.
3. Secret-scans backup candidates, creates an atomic manifested archive, and
   creates a pre-run Git checkpoint.
4. Runs deterministic vault health and secret checks.
5. Compares source-note hashes with the last successful run.
6. Calls `openai/gpt-5.6-luna` only when source notes changed or a task is pending.
7. Validates model JSON and every referenced evidence path.
8. Writes only dated/latest derived reports and completes queued tasks.
9. Runs post-write validation and commits the final Git checkpoint.
10. Releases the lease and requires Syncthing to return to a complete,
    conflict-free state; a postflight timeout fails the systemd run.

An unchanged run does not call the model. The first live analysis cost $0.00895;
the task-inbox validation cost $0.00778. Actual usage and cost are retained in
`/var/lib/obsidian-cloud/cloud-state.json` and the report records token counts.

### When The Mac Is Offline

A complete but disconnected replica is read-only with respect to the synced
vault. The VPS may create a server-local backup and run Luna, but it stores the
validated result only at
`/var/lib/obsidian-cloud/cloud-staged-report.json`. It does not create a shared
lease, health note, cloud report, Git normalization file, or completed task in
the vault.

After the Mac reconnects, the next run compares every source-note hash and
pending-task fingerprint with the staged packet. An exact match publishes the
report without another model call. Any difference discards the stale stage and
runs fresh analysis against the converged vault. This preserves useful
overnight compute without allowing disconnected replicas to mutate independently.

`obsidian-cloud-reconnect.timer` checks every five minutes with up to one minute
of jitter. It exits immediately when no stage exists, waits without error while
the peer is offline, and rate-limits publish attempts to one per ten minutes.
It invokes the full fenced transaction only after Syncthing is healthy and the
source/task fingerprints still match. A stale stage is left for the nightly
transaction to discard and recompute rather than spending from a reconnect
probe.

## Queuing An Overnight Task

The normal interface is natural language: ask Codex to queue an Obsidian cloud
organization task for tonight. Codex creates one Markdown file under
`_System/Cloud Tasks/Pending` with this contract:

```markdown
---
title: Review project links
type: cloud-task
status: pending
managed_by: codex-obsidian-sidecar
---

Find missing or weak links across current project notes. Return evidence-backed
recommendations only. Do not suggest deleting source notes.
```

Instructions are limited to 4,000 characters and blocked if they contain an
apparent credential. The task is sent separately from untrusted note evidence.
After a successful connected publication it moves to
`_System/Cloud Tasks/Processed` with a completion timestamp and durable
permalink. An offline-staged or failed task stays pending.

## Routine Checks

Local:

```sh
obsidian-sidecar cloud-doctor
brew services list | grep syncthing
```

VPS:

```sh
SIDECAR_CONFIG_PATH=~/.config/codex-obsidian-sidecar/config.json
SIDECAR_CLOUD_HOST="$(jq -r .cloud_status_ssh_host "$SIDECAR_CONFIG_PATH")"
ssh "$SIDECAR_CLOUD_HOST" 'runuser -u obsidian-sync -- /opt/obsidian-cloud/venv/bin/obsidian-sidecar --config /etc/obsidian-cloud/config.json cloud-doctor'
ssh "$SIDECAR_CLOUD_HOST" 'runuser -u obsidian-sync -- /opt/obsidian-cloud/venv/bin/obsidian-sidecar --config /etc/obsidian-cloud/config.json cloud-benchmark'
ssh "$SIDECAR_CLOUD_HOST" 'systemctl list-timers obsidian-cloud-maintenance.timer --no-pager'
ssh "$SIDECAR_CLOUD_HOST" 'systemctl list-timers obsidian-cloud-reconnect.timer --no-pager'
ssh "$SIDECAR_CLOUD_HOST" 'journalctl -u obsidian-cloud-maintenance.service -n 100 --no-pager'
```

`cloud-benchmark` is scored out of 100. Passing requires at least 80 points and
every critical gate. The current deployment scores 100.

When a peer is disconnected, `cloud-doctor` intentionally exits nonzero and
reports `offline_read_safe: true` only when the local replica is complete and
conflict-free. That state permits staged analysis, not synced-vault mutation.
Failed service runs retry after 15 minutes, up to three starts per hour, and
leave `/var/lib/obsidian-cloud/maintenance.failed` until successful completion.
The success handler removes the marker and resets the retry counter so
operator-triggered healthy runs do not exhaust the failure budget.

The Mac launchd worker probes `alert-status` over the configured
`cloud_status_ssh_host` at most once every 15 minutes. A failed probe is
recorded but does not notify by itself. Desktop
notifications are reserved for failed queue entries, sync conflicts, a cloud
failure marker, or a staged report older than 24 hours, and identical alerts
are suppressed for 24 hours.

## Recovery

### Sync Is Not Healthy

Run `cloud-doctor` on both machines. Do not force an override. Confirm the peer
is connected, `need_files` and `need_bytes` are zero, and no lease is active.
Restart only the affected daemon:

```sh
brew services restart syncthing
SIDECAR_CLOUD_HOST="$(jq -r .cloud_status_ssh_host ~/.config/codex-obsidian-sidecar/config.json)"
ssh "$SIDECAR_CLOUD_HOST" 'systemctl restart syncthing@obsidian-sync.service'
```

### Conflict File Exists

The maintenance job intentionally stops. Compare the original and
`sync-conflict` copy, keep or merge the correct content, archive the other copy,
then require `cloud-doctor` and `cloud-benchmark` to pass. Never delete a
conflict without inspecting both versions.

### Restore A Server Backup

Stop the timer and Syncthing before changing the live vault. Extract into a
temporary directory first and inspect it:

```sh
SIDECAR_CLOUD_HOST="$(jq -r .cloud_status_ssh_host ~/.config/codex-obsidian-sidecar/config.json)"
ssh "$SIDECAR_CLOUD_HOST" 'systemctl stop obsidian-cloud-maintenance.timer'
ssh "$SIDECAR_CLOUD_HOST" 'systemctl stop syncthing@obsidian-sync.service'
ssh "$SIDECAR_CLOUD_HOST" 'mkdir -p /tmp/obsidian-restore-review'
ssh "$SIDECAR_CLOUD_HOST" 'tar -xzf /var/backups/obsidian-vault/obsidian-vault-YYYYMMDDTHHMMSSZ.tar.gz -C /tmp/obsidian-restore-review'
```

Restore only after comparing the archive with `/srv/obsidian-vault`. The server
Git history and each replica's `.stversions` directory provide two additional
recovery layers.

### Update The Cloud Runtime

Run local tests first, then deploy and verify:

```sh
cd ~/Documents/codex-obsidian-sidecar
.venv/bin/pytest -q
uvx ruff check src tests
python scripts/export_release.py
cd release
shasum -a 256 -c SHA256SUMS
cd ..
SIDECAR_VERSION=X.Y.Z
SIDECAR_CLOUD_HOST="$(jq -r .cloud_status_ssh_host ~/.config/codex-obsidian-sidecar/config.json)"
rsync -az --exclude .git --exclude .venv --exclude .pytest_cache --exclude .ruff_cache --exclude __pycache__ ./ "$SIDECAR_CLOUD_HOST":/opt/obsidian-cloud/app/
ssh "$SIDECAR_CLOUD_HOST" 'install -d -o root -g root -m 0755 /opt/obsidian-cloud/releases'
rsync -az "release/artifacts/codex_obsidian_sidecar-${SIDECAR_VERSION}-py3-none-any.whl" "$SIDECAR_CLOUD_HOST":/opt/obsidian-cloud/releases/
ssh "$SIDECAR_CLOUD_HOST" "/opt/obsidian-cloud/venv/bin/pip install --force-reinstall --no-deps /opt/obsidian-cloud/releases/codex_obsidian_sidecar-${SIDECAR_VERSION}-py3-none-any.whl"
ssh "$SIDECAR_CLOUD_HOST" 'cd /opt/obsidian-cloud/app && /opt/obsidian-cloud/venv/bin/pytest -q'
```

After a runtime or systemd change, run one manual maintenance cycle followed by
`cloud-benchmark`. Preserve the previous app tree and installed package as a
timestamped archive before deployment, and keep the exact prior wheel under
`/opt/obsidian-cloud/releases` as the rollback input. Syncthing itself is
installed from Homebrew on the Mac and the official Syncthing stable-v2 apt
repository on Ubuntu.

Maintenance and reconnect failures have independent markers under
`/var/lib/obsidian-cloud`. Each marker is cleared only by a successful run of
the service that owns it, preventing a recovered reconnect from hiding a real
nightly maintenance failure.

## Primary References

- Syncthing folder types: <https://docs.syncthing.net/users/foldertypes.html>
- Syncthing file versioning: <https://docs.syncthing.net/users/versioning>
- Syncthing ignore files: <https://docs.syncthing.net/users/ignoring.html>
- Syncthing systemd operation: <https://docs.syncthing.net/users/autostart>
- Official Ubuntu packages: <https://apt.syncthing.net/>
- Self-hosted LiveSync: <https://github.com/vrtmrz/obsidian-livesync>
- OpenRouter structured outputs: <https://openrouter.ai/docs/guides/features/structured-outputs>
