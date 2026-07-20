# Security Policy

## Supported Releases

Only the latest stable release receives security fixes before the first public
1.0 release.

## Reporting

Do not open a public issue containing credentials, private vault content, or a
working exploit. Use the repository's private security advisory flow after the
public repository is created.

## Boundaries

- Local operation requires no API key stored by the sidecar.
- The Stop hook queues metadata and does not copy raw transcripts into the
  vault.
- Model curation runs read-only with user rules, hooks, and network tools
  disabled.
- Config and runtime state are user-only.
- Setup never requires root and does not open a listening port.
- Cloud replication is optional, separately administered, and disabled by
  default.
- Updates are explicit exact-version package operations; background checks do
  not mutate the installation.

See `README.md` and `docs/TESTING.md` for the validation and safety gates.
