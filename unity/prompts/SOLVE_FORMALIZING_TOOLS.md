# Available tools for `unity solve` — Lean formalization

Start with `solve_brief(author)` and refresh frequently. The brief contains the accepted solution identity,
ready tasks, current strategies, findings, and candidate events.

- `register_strategy(author, description, target?, strategy_family?)` registers a distinct Lean approach;
  `target` must be the formal task ID. Then use `claim_strategy`, `assist_strategy`, `unclaim_strategy`, or
  `mark_strategy_incorrect` as appropriate. Own only one claimed strategy at a time.
- `publish_finding`, `report_obstacle`, `ask_question`, and `answer_question` share checked APIs, tactic
  patterns, failures, and blockers.
- `emit_formalization_candidate(strategy_id, author, task_id, commit_sha, notes?, supersedes?)` submits an
  exact worktree commit. Use targeted checks while iterating, then run one full `lake build` after the final
  edit; Unity independently rebuilds and verifies it in main. A zero exit status is success even when the
  build emits unrelated style or documentation warnings.
- `sync_from_main(author, reason?)` discards obsolete local work and synchronizes to accepted main.
- `propose_source_fix(author, path, reason, supersedes?)` snapshots corrected paper bytes and returns them
  to independent review.
- `reopen_solving(author, reason)` returns to full informal solving for a substantive paper defect.
- `forum_post`, `forum_read`, `solve_status`, `artifact_info`, and `artifact_read` provide discussion and
  bounded detail.

For backends without native MCP, run `unity mcp unity-forum <tool> '<json-args>'`.
