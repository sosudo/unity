# Available tools for `unity solve` — critic

Start with `solve_brief(author)` and use `solve_status()` for exact task/candidate state. Inspect artifacts
with `artifact_info` and bounded `artifact_read`.

- `submit_formalization_verdict(author, verdict, summary, review, reopen_tasks?, evidence?)` submits
  `approved`, `lean_reopen`, or `reopen_solving`. `review` contains the current `snapshot_id` and a
  `requirements` list of `{requirement_id, status, declarations, rationale}`. Approval requires every
  recorded requirement exactly once with status `pass`, valid declaration names and concrete rationale.
  Rejections can be partial; a Lean reopen requires exact task IDs. `evidence` alone cannot approve.
- `request_rechunk(author, reason)` requests a new protected formal specification for the same paper
  when the existing statement/definition mapping is wrong; old verification and evidence are invalidated.
- `propose_source_fix(author, path, reason, supersedes?)` submits corrected paper bytes for independent
  review when the mathematical repair is local.
- `reopen_solving(author, reason)` reopens full mathematical work immediately.
- `forum_post` and `forum_read` are available for concrete clarification.

The critic does not merge code or self-approve changed paper bytes.
