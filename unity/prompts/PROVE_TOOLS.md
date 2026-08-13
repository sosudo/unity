# Available MCP tools for `unity prove`

Forum and Lean LSP are always available. Axle and Aristotle are available only when their API
keys are configured. When Axle offers a tool equivalent to a Lean LSP tool, prefer Axle.

If a backend does not expose custom MCP tools directly, call them through the shell:
`unity mcp <server> <tool> '<json-args>'`. The available servers are `unity-forum`, `lean-lsp`,
`axle`, and `aristotle`.

## Forum — authoritative prove coordination

Start each turn with `forum_brief(author)` and use `prove_status(decl?)` whenever exact current
state is needed.

### Strategies

- `register_strategy(author, description, decl?)` — register a proposed proof strategy.
  Registration does not claim it. Exact normalized duplicates are rejected.
- `claim_strategy(strategy_id, author)` — atomically claim a registered strategy.
- `unclaim_strategy(strategy_id, author, reason?)` — release an unsuccessful but potentially
  viable strategy.
- `mark_strategy_incorrect(strategy_id, author, reason, evidence?)` — permanently record why a
  strategy cannot work.

### Candidates

- `emit_candidate(strategy_id, author, commit_sha, decl?, notes?, supersedes?)` — submit the exact
  committed source revision produced by an owned strategy.
- `endorse_candidate(candidate_id, author, review?)` — independently approve the exact candidate
  commit.
- `object_candidate(candidate_id, author, reason, evidence?)` — block the exact candidate with a
  concrete defect.
- `resolve_candidate_objection(candidate_id, objection_id, author, resolution)` — resolve an
  objection after checking the concern.
- `sync_to_main(candidate_id, author)` — merge an acceptable candidate under Unity's merge lock
  and run `lake build`.
- `sync_from_main(author, reason?)` — discard the caller's current attempt and synchronize its
  worktree to accepted main.
- `prove_status(decl?)` — read authoritative declaration, strategy, finding, candidate, objection,
  and event state.

### Discussion and knowledge

- `artifact_info(artifact_id)` — inspect an artifact's type, producer, source, hash, and size
  without loading its content.
- `artifact_read(artifact_id, offset?, limit?)` — retrieve one bounded page of a shared artifact.
  Follow `next_offset` only when the omitted detail is actually needed.
- `artifact_snapshot_file(path, known_sha256?, offset?, limit?)` — read a bounded project file
  snapshot. Pass a previously seen hash to learn that an unchanged file need not be reinserted.
- `publish_finding(author, kind, title, content, confidence, decl?, strategy_id?, evidence?)` —
  publish concise live proof-search knowledge. Choose a short descriptive `kind`; kinds are not
  restricted to a predefined list and Unity normalizes them for exact duplicate detection.
  `confidence` is an integer from 0 through 100. Use 100 only for a directly checked fact and
  include concrete evidence or an artifact reference.
- `supersede_finding(finding_id, author, reason, replacement_id?)` — retire a finding that is
  wrong, stale, or replaced. The original remains in history but disappears from the active brief.
- `forum_post(thread_id, author, content, reply_to?)` — free-form strategy discussion or live
  discussion that does not belong in structured finding state.
- `forum_obstacle(chunk, author, goal_state, tried, hypothesis)` — report a concrete blocker.
- `forum_question(author, body, to?, chunk?)` and `forum_answer(question_id, author, body)` —
  targeted questions and answers.
- `forum_decision(author, topic, choice, rationale)` — record a binding shared decision.
- `ledger_add(...)` and `ledger_get(...)` — store and retrieve verified reusable knowledge.
- `forum_read(...)` and `forum_list()` — retrieve raw discussion only when the brief is
  insufficient.

Finding confidence guidance: 0–24 speculative, 25–49 tentative, 50–74 plausible, 75–94 strongly
supported, 95–99 near-certain but not directly verified, and 100 directly verified with evidence.
These bands guide communication; the stored value remains the exact integer supplied by the agent.

For shell commands likely to produce substantial output, use `unity capture -- <command>`, for
example `unity capture -- lake build`. Small output remains inline; large output is retained in the
shared artifact store and replaced with a bounded preview and artifact reference. Put the conclusion
in a finding and reference the artifact as evidence instead of copying full logs into the Forum.

Do not use `forum_claim`, `forum_result`, `forum_endorse`, `forum_object`, `forum_consensus`, or
`forum_handoff` for prove coordination. They remain compatibility tools for other pipelines.

## Lean LSP — inspect and drive the local Lean project

- `lean_goal` — inspect proof goals at a source position.
- `lean_term_goal` — inspect the expected type at a position.
- `lean_diagnostic_messages` — retrieve compiler diagnostics for a file.
- `lean_build` — build the project and restart the LSP when needed.
- `lean_file_outline` — inspect imports and declarations with their signatures.
- `lean_hover_info`, `lean_completions`, `lean_declaration_file`, and `lean_references` — inspect
  symbols and APIs.
- `lean_local_search` — verify that a declaration exists in this project's installed libraries.
- `lean_leansearch`, `lean_loogle`, and `lean_leanfinder` — search Mathlib by language, type, or
  mathematical meaning.
- `lean_state_search` and `lean_hammer_premise` — search from the current goal state.
- `lean_code_actions` and `lean_multi_attempt` — inspect suggestions and test multiple tactics.
- `lean_run_code` — compile a self-contained snippet with explicit imports.
- `lean_verify` — inspect theorem axioms and scan source for prohibited proof shortcuts.
- `lean_minimal_hypotheses`, `lean_profile_proof`, `lean_get_widgets`, and
  `lean_get_widget_source` — specialized proof inspection tools.

## Axle — external Lean verification and source manipulation

External services may use a different Lean or Mathlib version. Always verify their output in the
local project with `lake build` before calling `emit_candidate`.

- `verify_proof` and `check` — validate proofs or Lean code.
- `highlight` — retrieve semantic highlighting.
- `extract_decls` — inspect declarations and dependencies in a file.
- `repair_proofs` and `simplify_theorems` — repair or simplify proofs.
- `disprove` — attempt to prove a negation.
- `merge`, `rename`, and `normalize` — manipulate Lean source.
- `theorem2lemma`, `theorem2sorry`, `have2lemma`, `have2sorry`, and `sorry2lemma` — declaration
  transformations for scratch work and diagnosis. Never submit generated `sorry` placeholders.
- `list_environments` — inspect available external toolchains.
- `share_url` and `read_share_url` — exchange Axle artifacts.

## Aristotle — asynchronous external prover

- `aristotle_submit(prompt, project_dir)` — submit a proving job and continue useful local work.
- `aristotle_status(project_id)` and `aristotle_wait(project_id, timeout_seconds, poll_seconds)` —
  inspect a submitted job.
- `aristotle_result(project_id, destination)` — download completed results into the worktree.
- `aristotle_cancel(project_id)` — cancel obsolete work.
- `aristotle_list(limit)` — list recent jobs.
