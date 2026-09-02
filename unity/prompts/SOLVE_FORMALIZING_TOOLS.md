# Available coordination tools for `unity solve` — formalizing profile

Formalize only the accepted immutable solution revision shown by `solve_brief(author)` and
`solve_status()`. The natural-language solution and live formalization state persist independently of
raw Forum history.
The server infers the active gate; none of these tools accepts a model-selected stage.
Use the brief header's full problem/solution hashes, gate revisions, and accepted candidate IDs when
checking that your work still targets the live formalization gate.

## Coordinate Lean work

- `register_strategy(author, description, subgoal_id?, strategy_family?)`,
  `claim_strategy(strategy_id, author)`, `assist_strategy(...)`, `unclaim_strategy(...)`, and
  `mark_strategy_incorrect(...)` coordinate distinct formalization or repair approaches.
- `publish_finding(...)` and `supersede_finding(...)` share checked Mathlib declarations, API facts,
  working tactic patterns, concrete failures, and source-fidelity constraints.
- `report_obstacle(author, goal_state, subgoal_id?, strategy_id?, tried?, hypothesis?, evidence?)`,
  `ask_question(...)`, and `answer_question(...)` surface blockers and route help.
- `forum_post(...)` and `forum_read(...)` are for discussion; they do not reserve formalization work.

Refresh the brief before claiming work, before changing direction, before editing shared declarations,
and after any candidate, merge, source-fix, or reopen event.

## Submit and synchronize

- `claim_formal_task(task_id, author)` atomically reserves one ready task from `solve_brief`;
  `release_formal_task(task_id, author, reason?)` returns owned work that is blocked or obsolete.
- `emit_formalization_candidate(author, commit_sha, task_id, strategy_id?, notes?, supersedes?)`
  submits the exact committed Lean revision for a task you currently own. The server may infer
  `task_id` only when you own exactly one current task; it never creates an ad-hoc task from a
  candidate. Run `lake build` locally first, but never report your own `build_ok` as authoritative.
- `sync_from_main(author, reason?)` discards obsolete worktree changes, releases obsolete reservations,
  and synchronizes to accepted main.

Unity verifies and integrates the exact candidate revision. A correction is a new immutable candidate
linked through `supersedes`. Do not continue speculative work after a candidate interrupt unless the
authoritative state reopens it.

## Feed mathematical defects back to solving

- `propose_source_fix(author, summary, evidence, path?, candidate_id?, subgoal_id?)` snapshots the
  corrected draft (default `.unity/solve/drafts/<author>/PROOF.tex`) and atomically replaces the
  currently accepted candidate: either the gate reopens with the exact new bytes submitted for
  targeted independent review, or authoritative state is unchanged.
- `reopen_solving(author, reason, evidence?, candidate_id?)` requests the explicit transition back to
  mathematical solving when the accepted solution is false, incomplete, or unfaithful to the theorem
  that Lean must formalize. Do not use it for ordinary Lean difficulty.

Use `artifact_info` and bounded `artifact_read` for build logs, verification records, computations, and
source snapshots. Distill their actionable conclusions into findings or source-fix proposals.
