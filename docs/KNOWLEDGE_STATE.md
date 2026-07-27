# Freshness And Decision Impact

The Sidecar's session notes are durable history. A note may be updated as its
Codex session continues, while the session ID remains its stable identity.
Freshness and decision impact live in a smaller stateful layer: canonical
project hubs, canonical decision records, runbooks, and operational
instructions.

## Long-Thread Checkpoints

The private checkpoint layer is an efficiency and continuity cache, not a new
knowledge authority. It retains the last validated decisions, unresolved items
with explicit dispositions, current phase, resume context, and artifact links.
On the next turn, Luna receives that compact state plus only new transcript
messages. The repository remains authoritative for code, the vault remains the
human-visible durable record, and a checkpoint is advanced only after the
corresponding vault write succeeds.

This keeps early decisions available throughout a long thread without sending
the entire growing transcript on every curation pass. Checkpoints never enter
the vault or Basic Memory index and never retain raw tool output, internal
reasoning, developer instructions, or full transcripts.

## Freshness Envelope

A freshness-bearing note uses structured frontmatter:

```yaml
freshness:
  class: project
  observed_at: '2026-07-20T05:00:00+00:00'
  verified_at: '2026-07-20T05:00:00+00:00'
  review_after: '2026-08-19T05:00:00+00:00'
  ttl_days: 30
  source: vault:60 Sessions/2026/2026-07/example.md
  verified_source: vault:60 Sessions/2026/2026-07/example.md
  source_revision: abcdef1
```

The persisted envelope records evidence and policy, not a frozen `current` or
`stale` label. `freshness-status`, daily maintenance, and the knowledge report
compute one of these states at read time:

- `current`: explicitly verified and still inside its review window;
- `unverified`: observed inside its review window without explicit
  verification;
- `review-due`: the review date has passed;
- `unknown`: no envelope exists;
- `invalid`: the envelope does not satisfy the contract;
- `superseded`: a decision is explicitly superseded.

Default review windows are 30 days for project hubs, 90 days for decisions,
and 14 days for runbooks or operational instructions. They are configurable as
`freshness_project_days`, `freshness_decision_days`, and
`freshness_runbook_days`. A manually selected `durable` class has no automatic
review deadline.

Session capture refreshes a project envelope. Verification evidence or direct
operator evidence can verify a decision; merely mentioning it does not silently
claim verification. Historical session bodies are not rewritten.

## Canonical Decisions

High-confidence session decisions are promoted to managed records under:

```text
40 Decisions/<project>/<stable-key>.md
```

Each record includes a deterministic project-scoped `decision_id`, status,
freshness, source sessions, authority, and one of five decision types:

- `operator-decision`: an explicit user choice, approval, or prohibition;
- `implemented-choice`: a choice actually applied in the work;
- `recommendation`: advice or a research option not yet accepted;
- `observation`: a durable fact or constraint, not a user choice;
- `legacy-unclassified`: retained history whose original authority cannot be
  established safely.

Authority is derived separately as `operator`, `repository-evidence`, `agent`,
`checkpoint`, or `legacy`. Only operator decisions and implemented choices are
eligible for the active project index. Recommendations remain proposed,
observations remain informational, and ambiguous legacy items require review.

Before promotion, the Sidecar compares normalized text, token overlap, polarity,
and numeric terms against existing project decisions. A very high-confidence
wording variant reuses the existing record and preserves the alternate wording.
A probable duplicate is not merged: it is marked `needs-review`, linked to its
candidates, and listed in the project review index.

Impact targets use these typed relationships:

- `vault:` points to another vault note;
- `repo:` points to a path relative to the project's source working directory;
- `file:` preserves an external absolute artifact path and is explicitly
  nonportable.

The project hub receives managed active and review indexes. Operators can
explicitly maintain `supersedes` relationships in decision frontmatter.

## Blast-Radius Preview

Run:

```sh
obsidian-sidecar decision-impact DECISION_ID
obsidian-sidecar decision-impact DECISION_ID --depth 2
```

The command distinguishes:

- `direct`: an artifact explicitly associated with that decision;
- `inferred`: a conservative legacy edge retained for review;
- `related`: project context, sources, incoming references, and explicit
  decision-to-decision supersession edges.

The direct affected count excludes related project context. The command reports
missing or nonportable targets instead of hiding them. Depth is capped at three.

This surface is read-only. It never edits a runbook, project note, session,
decision, or repository artifact. The operator remains responsible for
approving any downstream update.

## Session Moves And Reference Integrity

The Codex `session_id` is the durable identity even when a later capture changes
the note's date, confidence route, or project slug. A move writes and verifies
the new session note first, retargets exact references in Sidecar-managed
project hubs, decisions, runbooks, and operational instructions, and removes
the prior note only after no references remain.

The retarget operation changes structured `vault:` values and exact session
wikilinks only. It does not fuzzy-match text, rewrite unmanaged notes, or merge
project identities. If an unmanaged note still links to the prior path, the
move stops with both session notes retained for operator review.

Freshness inspection also resolves local `vault:` evidence sources. A missing
`source` or `verified_source` makes the envelope invalid and therefore a
critical health finding.

## Existing Vault Migration

First inspect the plan:

```sh
obsidian-sidecar knowledge-migrate
```

Then apply on the authoritative local replica:

```sh
obsidian-sidecar knowledge-migrate --apply
```

Apply is idempotent, local-only, lease-protected, and followed by Basic Memory
reindexing. It adds canonical IDs and envelopes to managed project hubs,
creates decision records from existing managed session sections, types records
when historical evidence still supports that classification, marks ambiguous
or duplicate legacy records for review, converts broad legacy impact into
inferred edges, annotates managed runbooks, refreshes project decision indexes,
and adds current-state/open-work rollups to project hubs. It does not alter
session bodies, silently merge decision records, or automatically merge
duplicate project identities.

## Project Hubs And Model Provenance

Each managed project hub carries two generated blocks:

- current state: latest phase, outcome, resume context, verification count, and
  source session;
- ranked open work: blockers first, followed by scheduled, monitored, and
  accepted items, each linked to its source.

Session notes also record model provenance when the Codex transcript provides
it. The bounded fields are model, provider, reasoning effort, and harness. This
metadata supports later quality comparisons without storing prompts, raw
transcripts, internal reasoning, tool output, or token-level telemetry.

Daily maintenance writes the human-visible summary at
`_System/Knowledge/latest.md`. Project identity conflicts are reported for
operator review rather than resolved destructively.
