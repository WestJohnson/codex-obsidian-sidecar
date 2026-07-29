# Testing

## Acceptance Standard

The benchmark has 100 available points. Passing requires both:

- a score of at least 80; and
- every critical gate passing.

This prevents a high aggregate score from hiding a broken secret boundary,
untrusted hook, unavailable retrieval layer, or failed live curation path.

## Scored Cases

| Case | Points | Critical | Evidence |
| --- | ---: | :---: | --- |
| Transcript boundary | 10 | Yes | Only user and final-answer content survives. |
| Secret redaction | 10 | Yes | Multiple credential classes are removed. |
| Grounding guard | 8 | Yes | Unknown evidence IDs are rejected. |
| Queue batching | 4 | No | Multiple turns collapse into one session group. |
| Atomic idempotent write | 8 | Yes | A repeated session updates one note. |
| Quarantine path | 5 | No | Invalid output is isolated safely. |
| Vault doctor detection | 6 | No | Broken links and secrets reduce health. |
| Git backup snapshot | 4 | No | A restorable local commit is created. |
| Obsidian CLI search | 5 | Yes | Obsidian finds a newly written note. |
| Basic Memory retrieval | 10 | Yes | Lexical and hybrid searches find a fixture. |
| Installed integration health | 5 | Yes | Stop hook is trusted and launchd is clean. |
| Live Luna curation | 15 | Yes | Luna returns locally valid grounded output. |
| Live complete pipeline | 10 | Yes | Capture through write, private checkpoint, and doctor succeeds. |

## Deterministic Suite

Run:

```sh
.venv/bin/pytest
```

The tests cover parsing boundaries, prompt-injection context exclusion,
credential patterns, queue idempotency and batching, schema compatibility,
grounding, atomic writes, confidence-route deduplication, quarantine scrubbing,
maintenance findings, retries, package resources, shared lease exclusion,
same-host lease serialization, fenced local writers, post-convergence writer
and conflict races, online versus offline Syncthing health, non-mutating
offline staging, reconnect fingerprint validation, secret-safe archive
creation, manifested restore checks, backup rotation, postflight timeout and
conflict handling, strict cloud-agent path grounding, task-inbox completion,
bounded service retries, failure-marker reporting, reconnect-trigger rate
limiting, spend-ceiling preflight, curation chronology and contradiction
checks, canonical artifact preservation, freshness-state calculation,
canonical project identity reuse, decision-record idempotency, read-only
blast-radius previews, reference-safe same-session moves, missing freshness
source detection, plan-first knowledge migration, alert deduplication,
incremental checkpoint creation and seeding, append-only transcript cursors,
completed-hook cutoffs, bounded delta continuation, failure-safe cursor
advancement, compact checkpoint evidence, hourly Git checkpoints, cloud
carry-forward self-compaction, non-authoritative decision aging, cloud
transaction idempotency, transactional setup, hook preservation, installer
idempotency, HTTPS-only update metadata, exact-version updates, hook-path
migration, and rollback. Pytest's collected count is the source of truth as
coverage grows.

## Release Smoke Test

After building the release, run:

```sh
uv run python scripts/smoke_release.py
```

The smoke test creates an isolated home directory and Python 3.11 environment,
installs only the built wheel, validates the agent bundle, confirms setup is
read-only by default, applies a local install without host integrations, and
checks config permissions plus structural verification.

## Cloud Acceptance Score

Run on the VPS:

```sh
runuser -u obsidian-sync -- /opt/obsidian-cloud/venv/bin/obsidian-sidecar \
  --config /etc/obsidian-cloud/config.json cloud-benchmark
```

The `obsidian-cloud-v1` benchmark has 100 points and passes at 80 only when all
critical gates also pass:

| Case | Points | Critical |
| --- | ---: | :---: |
| TLS transport converged | 20 | Yes |
| Syncthing hardening | 15 | Yes |
| No sync conflicts | 15 | Yes |
| Nightly scheduler | 10 | Yes |
| Restorable recovery point within 48 hours | 10 | Yes |
| Git checkpoint clean | 10 | No |
| Recent agent cycle | 10 | No |
| Coordination clear | 10 | Yes |

The live acceptance run covers Mac-to-VPS and VPS-to-Mac writes, prior version
retention on both receivers, offline analysis with zero synced-vault writes,
staged-result publication after fingerprint-equal reconnection, a Syncthing
restart, isolated manifested archive extraction, negative writer/conflict
gates, one real Luna source-change analysis, one real queued-task analysis, and
an unchanged run that makes no model call.

## Live Suite

Run:

```sh
obsidian-sidecar benchmark
```

The command uses the installed Codex, Luna model, official Obsidian CLI, Basic
Memory index, global hook configuration, launchd job, and a temporary vault for
destructive cases. Search fixtures and the latest benchmark report are written
under the live vault's `_System` directory.

## Release Checklist

1. Run the deterministic suite.
2. Build the wheel, source distribution, agent bundle, release manifest, and
   checksums with `uv run python scripts/export_release.py`.
3. Run `uv run python scripts/smoke_release.py` against Python 3.11.
4. Install with `uv tool install --force --no-cache --python 3.13 .`.
5. Run the benchmark twice to detect state leakage and timing flakes.
6. Run one real Codex turn and confirm `hook: Stop Completed`.
7. Force-process the event and retrieve the note through Obsidian and Basic
   Memory.
8. Run `doctor --backup` and require a score of at least 80 with zero critical
   failures.
9. Kickstart the background service and require a clean exit.
10. Verify release checksums, artifact secret scan, and build provenance.
