# Freshness And Decision Impact

The Sidecar's session notes are immutable history. Freshness and decision
impact live in a smaller stateful layer: canonical project hubs, canonical
decision records, runbooks, and operational instructions.

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
freshness, source sessions, and typed targets:

- `vault:` points to another vault note;
- `repo:` points to a path relative to the project's source working directory;
- `file:` preserves an external absolute artifact path and is explicitly
  nonportable.

The project hub receives a managed decision index. Exact normalized decision
text reuses the same record; fuzzy semantic merging is intentionally omitted to
avoid silently combining different choices. Operators can explicitly maintain
`supersedes` relationships in decision frontmatter.

## Blast-Radius Preview

Run:

```sh
obsidian-sidecar decision-impact DECISION_ID
obsidian-sidecar decision-impact DECISION_ID --depth 2
```

The command resolves direct affected targets, source records, incoming vault
references, and explicit decision-to-decision supersession edges. It reports
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
creates decision records from existing managed session sections, annotates
managed runbooks, and refreshes project decision indexes. It does not alter
session bodies or automatically merge duplicate project identities.

Daily maintenance writes the human-visible summary at
`_System/Knowledge/latest.md`. Project identity conflicts are reported for
operator review rather than resolved destructively.
