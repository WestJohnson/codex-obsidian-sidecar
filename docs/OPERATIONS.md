# Operations

## Runtime Topology

1. Codex runs the trusted global `Stop` hook from `~/.codex/hooks.json`.
2. The hook writes only event metadata to
   `~/.local/share/codex-obsidian-sidecar/queue/` and always exits zero.
3. The configured `service_label` runs once per minute through launchd and
   waits for the configured debounce window.
4. The worker loads the session's last validated private checkpoint and reads
   only newly appended user messages and final answers from the Codex
   transcript, stopping at the newest completed hook event so an in-progress
   next turn cannot leak into durable memory. It adds bounded Git metadata,
   redacts likely credentials, and invokes Luna in a read-only ephemeral Codex
   process.
5. Locally validated output is written atomically to the Obsidian vault and
   indexed by Basic Memory. The worker advances the checkpoint cursor only
   after a validated skip or a successful vault write.
6. Daily maintenance checks structure, links, duplicates, secrets, queues,
   freshness, decision records, Obsidian CLI, Basic Memory, and Git backup
   health. It also refreshes `_System/Knowledge/latest.md`.
   A dirty vault also receives a lightweight Git checkpoint at most once per
   hour without running the full daily maintenance pass.
7. Optional Syncthing keeps a headless replica on the configured cloud host.
8. The VPS runs fenced maintenance nightly at 03:30 HST and calls Luna only
   when source-note hashes changed or a trusted cloud task is pending.
9. If the Mac is disconnected, the VPS creates a validated backup and stages
   any Luna result outside the vault. It publishes only after reconnection and
   an exact source/task fingerprint match.
10. A five-minute reconnect timer notices a returned peer and publishes a
    matching stage within the configured ten-minute rate limit.
11. The Mac displays a deduplicated notification only for failed queue events,
    Syncthing conflicts, persistent cloud failure markers, or a staged report
    left unpublished for more than 24 hours.

Raw transcripts, tool output, reasoning, developer instructions, and hook
payload contents are not copied into the vault.

## Incremental Session Checkpoints

Each long-running Codex session has one versioned checkpoint under
`~/.local/share/codex-obsidian-sidecar/checkpoints/`. The file contains the
last validated durable curation, a byte cursor into the append-only transcript,
and artifact references. It does not contain a raw transcript. Files are
written atomically with mode `0600` and must pass the same secret boundary as
vault output.

The next curation packet contains that checkpoint as `c1`, followed by only
new user/final-answer evidence and current bounded Git facts. A first run on an
existing session can seed from its latest managed session note. Very large
deltas are processed in bounded chunks and immediately requeued. Missing,
stale, or corrupt checkpoints fall back to a bounded recovery packet instead
of blocking capture.

Numeric token and packet-size telemetry is appended to
`curator-usage.jsonl`; no prompt or response text is logged. Set
`"curator_usage_logging": false` to disable it. The reversible feature flag
`"checkpoint_enabled": false` restores bounded full-window curation without
deleting checkpoint files.

## Routine Checks

```sh
SIDECAR_CONFIG_PATH=~/.config/codex-obsidian-sidecar/config.json
SIDECAR_SERVICE_LABEL="$(jq -r .service_label "$SIDECAR_CONFIG_PATH")"
SIDECAR_CLOUD_HOST="$(jq -r .cloud_status_ssh_host "$SIDECAR_CONFIG_PATH")"
obsidian-sidecar doctor --backup
obsidian-sidecar benchmark
launchctl print "gui/$(id -u)/$SIDECAR_SERVICE_LABEL"
obsidian-sidecar cloud-doctor
obsidian-sidecar alert-status
ssh "$SIDECAR_CLOUD_HOST" 'runuser -u obsidian-sync -- /opt/obsidian-cloud/venv/bin/obsidian-sidecar --config /etc/obsidian-cloud/config.json cloud-benchmark'
```

Healthy results are:

- doctor score at least 80, with zero critical failures;
- benchmark score at least 80, with no failed critical gate;
- launchd `last exit code = 0`;
- no files in `~/.local/share/codex-obsidian-sidecar/failed/`.
- cloud benchmark score at least 80 with every critical gate passing.
- no `/var/lib/obsidian-cloud/maintenance.failed` marker on the VPS.

The cloud backup gate restores and checksum-verifies the newest recovery point
and enforces a 48-hour maximum age. Backup creation separately requires an
exact match to the then-current durable vault, so ordinary work after the
nightly snapshot does not create a false critical failure.

The current reports are stored at:

- `Obsidian Vault/_System/Health/latest.md`
- `Obsidian Vault/_System/Health/benchmark-latest.md`
- `Obsidian Vault/_System/Knowledge/latest.md`
- `~/.local/share/codex-obsidian-sidecar/health.json`
- `~/.local/share/codex-obsidian-sidecar/benchmark-results/latest.json`

## Knowledge State

Inspect current freshness without changing the vault:

```sh
obsidian-sidecar freshness-status
obsidian-sidecar freshness-status --path '10 Projects/example/Project.md'
```

Preview the explicit impact boundary around one decision:

```sh
obsidian-sidecar decision-impact 'example/use-reviewed-update-path-0123abcd'
```

Existing managed vaults use a plan-first migration. Apply it only on the
authoritative local replica; the command acquires the local-writer lease and
refuses an active cloud lease:

```sh
obsidian-sidecar knowledge-migrate
obsidian-sidecar knowledge-migrate --apply
```

The migration adds metadata to managed project hubs and runbooks, creates
derived canonical decision records from existing managed session notes, and
adds managed decision-index blocks to project hubs. It does not rewrite
historical session bodies or any downstream repository artifact. See
[Knowledge State](KNOWLEDGE_STATE.md).

## Trust And Authentication

Codex hashes command hooks. After any change to `~/.codex/hooks.json`, open a
fresh Codex CLI, run `/hooks`, review the Stop hook, and trust its current
definition. The benchmark's `installed-integration-health` case detects a
modified or untrusted hook.

The official Obsidian CLI must remain enabled in Obsidian under Settings,
General, Advanced. Basic Memory uses the local `codex-vault` project. Neither
integration stores an API key in this project.

Syncthing is the only vault synchronization authority. Obsidian's built-in
Sync core plugin is disabled and has no local sync configuration. Do not enable
both transports for this vault.

## Recovery

Process a delayed queue immediately:

```sh
obsidian-sidecar process --force
```

Reload the worker:

```sh
SIDECAR_CONFIG_PATH=~/.config/codex-obsidian-sidecar/config.json
SIDECAR_SERVICE_LABEL="$(jq -r .service_label "$SIDECAR_CONFIG_PATH")"
SIDECAR_PLIST_PATH=~/Library/LaunchAgents/"${SIDECAR_SERVICE_LABEL}.plist"
launchctl bootout "gui/$(id -u)/$SIDECAR_SERVICE_LABEL"
launchctl bootstrap "gui/$(id -u)" "$SIDECAR_PLIST_PATH"
launchctl kickstart -k "gui/$(id -u)/$SIDECAR_SERVICE_LABEL"
```

Hook events without a usable transcript path are skipped before queueing, and
legacy queued copies are consumed as non-errors. Other failed events retry
three times, then move to the `failed` directory. Invalid model output is also
written to `_System/Quarantine` without retained secrets. Inspect the event's
`last_error`, correct the underlying issue, move the event back to `queue`, and
run `process --force`.

A corrupt checkpoint is ignored, recorded without content in
`checkpoint-errors.jsonl`, and replaced only after the recovery curation is
validated and written. To isolate checkpoint behavior while investigating,
set `"checkpoint_enabled": false`, reload the worker, and keep the existing
checkpoint directory intact for rollback or inspection.

Cloud maintenance retries a failed run after 15 minutes, with at most three
starts per hour. A maintenance failure creates
`/var/lib/obsidian-cloud/maintenance.failed`; the next successful maintenance
run clears it and resets the retry counter. Reconnect failures use the separate
`/var/lib/obsidian-cloud/reconnect.failed` marker, which is cleared by the next
successful reconnect check. Inspect the corresponding service journal before
removing or overriding a persistent marker.

## Updating The Runtime

Run the deterministic suite before installing a changed build:

```sh
cd ~/Documents/codex-obsidian-sidecar
.venv/bin/pytest
uv tool install --force --no-cache --python 3.13 .
obsidian-sidecar benchmark
```

Use `--no-cache` during local development so `uv` cannot reuse an older wheel
with the same version. After installation, kickstart launchd and confirm a
clean exit before considering the update complete.

Cloud deployment, overnight task format, sync recovery, and backup restoration
are documented in [Cloud Sync](CLOUD_SYNC.md).
