# Agent Installation Contract

## Inputs

- Existing Obsidian vault directory.
- Executable Codex CLI authenticated for a supported curation model.
- `uv` for isolated tool installation and updates.
- Optional Obsidian CLI and Basic Memory CLI.

No API key is an installation input. Cloud replication is a separate operator
workflow and is disabled by default.

## Install A Release

From the self-hosted release channel:

```sh
SIDECAR_VERSION=0.6.1
SIDECAR_WHEEL="codex_obsidian_sidecar-${SIDECAR_VERSION}-py3-none-any.whl"
SIDECAR_RELEASE="https://ai.westhawaiimarketing.com/charmfile/releases/sidecar/${SIDECAR_VERSION}"
mkdir -p artifacts
curl -fLo "artifacts/$SIDECAR_WHEEL" "$SIDECAR_RELEASE/artifacts/$SIDECAR_WHEEL"
curl -fLO "$SIDECAR_RELEASE/SHA256SUMS"
grep "artifacts/$SIDECAR_WHEEL$" SHA256SUMS | shasum -a 256 -c -
uv tool install "./artifacts/$SIDECAR_WHEEL"
```

From an offline export:

```sh
shasum -a 256 -c SHA256SUMS
uv tool install --force ./codex_obsidian_sidecar-VERSION-py3-none-any.whl
```

Do not use `pip` against the system Python and do not use `sudo`.

## Discover

```sh
obsidian-sidecar preflight
```

Required successful evidence:

- supported macOS or Linux service manager, or an explicitly accepted manual
  worker plan;
- a real vault candidate selected by the user;
- an executable Codex CLI;
- sidecar on `PATH`;
- Basic Memory present when its integration is requested.

## Plan

```sh
obsidian-sidecar setup \
  --vault '/absolute/path/to/vault' \
  --codex-bin '/absolute/path/to/codex'
```

The command is read-only unless `--apply` is present. Optional switches:

- `--no-codex-hook`
- `--no-service`
- `--no-basic-memory`
- `--disable-update-checks`
- `--model MODEL`
- `--basic-memory-project NAME`

Reject plans that require root, execute remote scripts, store secrets, or touch
files outside the reported action list.

## Apply

After user approval, rerun the exact plan with `--apply`. The installer:

- atomically writes a mode-`0600` config;
- merges one bounded Stop hook without replacing unrelated hooks;
- registers Basic Memory only when the project name is free or already points
  to the same vault;
- creates a launchd agent on macOS or user-level systemd timer on Linux;
- creates timestamped backups before replacing existing files;
- restores touched files if setup fails.

## Verify

```sh
obsidian-sidecar verify-install
obsidian-sidecar doctor
obsidian-sidecar benchmark
```

`verify-install` checks structural integration. `doctor` checks vault health.
`benchmark` is the live acceptance test and must score at least 80 with no
critical failures. Hook trust remains a deliberate human action in a fresh
Codex session.

For an existing managed vault upgrading to 0.4.0 or later, run the read-only
`knowledge-migrate` plan, review its project and decision counts, and apply only
with explicit approval on the authoritative local replica. A repeated apply
must report no changed project, decision, or runbook records. Confirm computed
freshness and a representative read-only decision impact before completion.

## Platform Boundary

- macOS: launchd integration is supported.
- Linux: user-level systemd integration is supported when a user bus exists.
- Windows: install the package only and use a manually reviewed scheduler; the
  setup command rejects automatic service installation.

## Updates

```sh
obsidian-sidecar update-check
obsidian-sidecar update --yes
```

The check is read-only. Apply downloads the exact target and rollback wheels
from the same self-hosted HTTPS origin, verifies their SHA-256 hashes before
mutation, installs through `uv`, verifies the CLI version, and restores the
prior wheel on failure.
