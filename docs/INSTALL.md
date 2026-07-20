# Installation

Codex Obsidian Sidecar is installed as an isolated Python tool. It does not
require root, a public server, or an API key for local operation.

## Requirements

- macOS or Linux;
- an existing Obsidian vault;
- authenticated Codex CLI with access to the configured curation model;
- `uv`;
- optional Obsidian CLI and Basic Memory CLI.

Windows can run the package and worker manually, but the production installer
does not create a Windows scheduler task.

## Agent-Driven Installation

Open the extracted release source in a compatible coding agent and ask:

```text
Use the obsidian-sidecar-setup skill in this release to install the sidecar.
Inspect this machine first, show me the read-only setup plan, and wait for my
approval before applying it. Do not use root or store any credentials.
```

The repository includes `.agents/skills/obsidian-sidecar-setup/SKILL.md`, which
follows the open Agent Skills format. The agent interprets local differences;
the package performs all configuration writes and verification.

## Direct Installation

Install an exact release:

```sh
uv tool install 'codex-obsidian-sidecar==VERSION'
```

Inspect the machine:

```sh
obsidian-sidecar preflight
```

Generate a read-only plan:

```sh
obsidian-sidecar setup \
  --vault "$HOME/Documents/Obsidian Vault" \
  --codex-bin "$(command -v codex)"
```

Review the JSON action list, then apply the same command with `--apply`.

## Verification

```sh
obsidian-sidecar verify-install
obsidian-sidecar doctor
obsidian-sidecar benchmark
```

Open a fresh Codex session and review/trust the Stop hook before relying on
automatic capture. A benchmark passes only at 80 or higher with every critical
gate passing.

## Files Touched

- `~/.config/codex-obsidian-sidecar/config.json`
- `~/.local/share/codex-obsidian-sidecar/`
- `~/.codex/hooks.json` when hook integration is selected
- `~/Library/LaunchAgents/io.github.codex-obsidian-sidecar.plist` on macOS
- `~/.config/systemd/user/io.github.codex-obsidian-sidecar.*` on Linux
- Basic Memory's project registry when retrieval integration is selected

Existing files receive timestamped backups. Setup is transactional for file
writes and never overwrites unrelated Codex hooks.
