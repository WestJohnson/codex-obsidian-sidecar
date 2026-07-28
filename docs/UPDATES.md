# Updates

## User Flow

Update checks are read-only and may run once per day when enabled:

```sh
obsidian-sidecar update-check
```

Before a version is published to the self-hosted channel, this returns
`status: not-published`. Install an offline production candidate only from its
verified release directory:

```sh
cd release
shasum -a 256 -c SHA256SUMS
uv tool install --force ./artifacts/codex_obsidian_sidecar-VERSION-py3-none-any.whl
```

An update is never applied automatically. After reviewing the current and
target versions:

```sh
obsidian-sidecar update --yes
```

The updater reads the HTTPS release index at
`https://ai.westhawaiimarketing.com/charmfile/releases/sidecar/index.json`.
It requires the target and rollback wheels to use that same HTTPS origin,
downloads both before mutation, verifies each exact SHA-256 digest, asks `uv`
to install the target wheel, and restores the prior wheel if version
verification fails. Run `verify-install`, `doctor`, and `benchmark` after an
update.

## Release Security

- Releases are built in CI from version tags.
- The canonical source mirror, release files, and update metadata are hosted
  under the Charmfile HTTPS origin.
- Release assets include SHA-256 hashes.
- GitHub build provenance attestations are generated for release artifacts.
- The installer never executes a downloaded shell script.
- The local service does not listen on a network port.

Package hashes detect corruption and mismatched downloads. HTTPS and
same-origin enforcement bind update metadata and wheels to the controlled
release host. GitHub attestations additionally bind mirrored artifacts to the
repository workflow identity. Protect the release host, Git repository, TLS
configuration, workflow, and signing key as production release infrastructure.

## Maintainer Flow

1. Work from a Git checkout with a configured `origin`, a clean tree, and a
   protected default branch; do not tag an extracted release bundle.
2. Update `pyproject.toml`, `src/obsidian_sidecar/__init__.py`, and
   `CHANGELOG.md` to the same version.
3. Run `uv run python scripts/export_release.py`.
4. Verify every generated artifact with `SHA256SUMS`.
5. Push a signed `vX.Y.Z` tag after review. CI rejects a tag that does not
   match the package version.
6. Build `index.json` with every supported rollback wheel:

   ```sh
   uv run python scripts/build_update_index.py \
     --output release/index.json \
     /path/to/codex_obsidian_sidecar-OLD-py3-none-any.whl \
     release/artifacts/codex_obsidian_sidecar-NEW-py3-none-any.whl
   ```

7. Publish the versioned release directory and replace `index.json` only after
   every immutable artifact is present. Update the bare HTTPS Git mirror with
   `git update-server-info`.
8. Confirm deterministic tests, clean-install tests, GitHub attestations,
   self-hosted checksums, update, and rollback.
9. Install the public release on a clean machine and run the live benchmark.

The self-hosted release directory is the source of truth. GitHub Releases are a
public mirror and provenance surface, not an installation dependency.
