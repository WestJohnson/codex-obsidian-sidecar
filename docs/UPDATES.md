# Updates

## User Flow

Update checks are read-only and may run once per day when enabled:

```sh
obsidian-sidecar update-check
```

An update is never applied automatically. After reviewing the current and
target versions:

```sh
obsidian-sidecar update --yes
```

The updater asks `uv` to install one exact package version from the PyPI HTTPS
index, checks the resulting CLI version, and attempts to reinstall the prior
exact version if verification fails. Run `verify-install`, `doctor`, and
`benchmark` after an update.

## Release Security

- Releases are built in CI from version tags.
- PyPI publication uses OIDC Trusted Publishing instead of a long-lived token.
- Release assets include SHA-256 hashes.
- GitHub build provenance attestations are generated for release artifacts.
- The installer never executes a downloaded shell script.
- The local service does not listen on a network port.

Package hashes prove artifact integrity. GitHub attestations additionally bind
an artifact to the repository workflow identity. Protect the release workflow,
tag permissions, and PyPI Trusted Publisher configuration as production
credentials.

## Maintainer Flow

1. Update `pyproject.toml`, `src/obsidian_sidecar/__init__.py`, and
   `CHANGELOG.md` to the same version.
2. Run `python scripts/export_release.py`.
3. Verify every generated artifact with `SHA256SUMS`.
4. Push a signed `vX.Y.Z` tag after review.
5. Confirm deterministic tests, clean-install tests, attestations, GitHub
   Release assets, and PyPI publication.
6. Install the public release on a clean machine and run the live benchmark.

The release workflow requires one external setup step: configure the PyPI
project's Trusted Publisher to match the GitHub repository, workflow filename,
and `pypi` environment.
