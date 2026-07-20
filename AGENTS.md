# Codex Obsidian Sidecar Contributor Guidance

- Use `.agents/skills/obsidian-sidecar-setup/SKILL.md` for installation,
  upgrade, repair, and migration work.
- Keep setup deterministic. Agents may interpret `preflight` and setup-plan
  JSON, but package code owns config, hook, and background-service writes.
- Never add credentials, private vault content, personal hostnames, absolute
  user paths, or live account identifiers to source, fixtures, docs, or release
  artifacts.
- Do not require root or public network listeners for local installation.
- Preserve unrelated hooks and integrations, back up changed files, and provide
  rollback for update or setup failures.
- Run the deterministic suite before packaging. A production candidate also
  requires clean-install, upgrade, rollback, artifact secret-scan, and live
  benchmark evidence.
