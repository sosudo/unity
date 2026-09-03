# Available tools for `unity solve` — critic

Start with `solve_brief(author)` and use `solve_status()` for exact task/candidate state. Inspect artifacts
with `artifact_info` and bounded `artifact_read`.

- `submit_formalization_verdict(author, verdict, summary, reopen_tasks?, evidence?)` submits `approved`,
  `lean_reopen`, or `reopen_solving`. A Lean reopen requires exact task IDs.
- `propose_source_fix(author, path, reason, supersedes?)` submits corrected paper bytes for independent
  review when the mathematical repair is local.
- `reopen_solving(author, reason)` reopens full mathematical work immediately.
- `forum_post` and `forum_read` are available for concrete clarification.

The critic does not merge code or self-approve changed paper bytes.
