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
- `request_rechunk(author, reason)` requests a new protected specification when the chunker's Lean
  statement or definition mapping is wrong but the accepted paper remains valid. Explain the precise
  mismatch first; the old contract and its review evidence are invalidated.
- `forum_post`, `forum_read`, `solve_status`, `artifact_info`, and `artifact_read` provide discussion and
  bounded detail.

For backends without native MCP, run `unity mcp unity-forum <tool> '<json-args>'`.

The shared `.lake/packages` cache is controller-owned. Worker commands `lake clean`, `lake update`,
`lake upgrade`, `lake exe cache`, and bare `lake build` are rejected. Do not bypass these restrictions
with `lean_build`. Use MCP tools for normal proof development: prefer enabled, compatible Axle tools
over equivalent Lean LSP tools; use Lean LSP for local goal state and project-specific diagnostics.
The injected Lean and external-tool catalogs describe available tools and their uses.

Use the supplied non-login shell environment (`login=false` where available). Do not start nested login
shells or bypass the guarded `lake` command. Direct shell checks are a fallback when MCP cannot provide
the needed diagnostic, or when a targeted build is needed for compiled artifacts. In that case, use
`unity capture -- lake env lean Project/File.lean` (or the targeted `lake build`) from your worktree root;
these shell checks are registered and serialized automatically. Do not pipe away the check's exit status.
Poll a running check's same tool session until it exits; never start a duplicate check merely because
output is empty. Finalize after a successful targeted local check unless a concrete error remains;
do not repeat a check against unchanged source.
