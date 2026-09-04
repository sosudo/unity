# Available tools for `unity solve` — Lean formalization

Start with `solve_brief(author)` and refresh frequently. The brief contains the accepted solution identity,
ready tasks, current strategies, findings, and candidate events.

- `register_strategy(author, description, target?, strategy_family?)` registers a materially distinct Lean
  approach; `target` must be the formal task ID. Prefer claiming a suitable registered strategy. Use
  `claim_strategy`, `assist_strategy`, `unclaim_strategy`, or `mark_strategy_incorrect` as appropriate.
  Own only one claimed strategy at a time.
- `publish_finding`, `report_obstacle`, `ask_question`, and `answer_question` share checked APIs, tactic
  patterns, failures, and blockers.
- `finalize_formalization(strategy_id, author, task_id, changed_paths?, notes?, supersedes?)` stages and
  commits the current worktree source, binds the candidate to that exact commit and diff hash, and submits
  it for Unity's authoritative main build. Call it as soon as the target appears complete. `changed_paths`
  is optional; omit it to include all non-ignored project changes.
- `emit_formalization_candidate(strategy_id, author, task_id, commit_sha, notes?, supersedes?)` remains
  available for compatibility when you already made the exact commit yourself. Prefer
  `finalize_formalization`.
- `sync_from_main(author, reason?)` discards obsolete local work and synchronizes to accepted main.
- `propose_source_fix(author, path, reason, supersedes?)` snapshots corrected paper bytes and returns them
  to independent review.
- `reopen_solving(author, reason)` returns to full informal solving for a substantive paper defect.
- `forum_post`, `forum_read`, `solve_status`, `artifact_info`, and `artifact_read` provide discussion and
  bounded detail.

For backends without native MCP, run `unity mcp unity-forum <tool> '<json-args>'`.
