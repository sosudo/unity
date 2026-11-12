# Available tools for `unity prove`

Lean LSP is always available. Axle and Aristotle are available only when their API keys are
configured. When Axle offers a tool equivalent to a Lean LSP tool, prefer Axle.

If a backend does not expose custom MCP tools directly, use the shell bridge. Write a UTF-8 JSON
argument object with exactly the documented tool fields, using `json.dump` or `json.dumps` to
preserve quotes, newlines, backslashes, and Lean source. Call `unity mcp <server> <tool>
--args-file /path/to/request.json`. To read that file through stdin, use
`unity mcp <server> <tool> --args-file - < /path/to/request.json`. A heredoc also requires
`--args-file -`; stdin alone does not supply the arguments. Pass either positional JSON or
`--args-file` for one call. The available servers are `lean-lsp`, `axle`, and `aristotle`.

## Lean LSP — inspect and drive the local Lean project

Unity binds Lean LSP to your current checkout: your worker worktree, or main during primary
review. Each shell bridge call starts a fresh server, so a previous file call does not initialize
the next call's state. A project-context error is a failed search, not evidence of zero matches.

- `lean_goal` — inspect proof goals at a source position.
- `lean_term_goal` — inspect the expected type at a position.
- `lean_diagnostic_messages` — retrieve compiler diagnostics for a file.
- `lean_build` — build the project and restart the LSP when needed.
- `lean_file_outline` — inspect imports and declarations with their signatures.
- `lean_hover_info`, `lean_completions`, `lean_declaration_file`, and `lean_references` — inspect
  symbols and APIs.
- `lean_local_search(query, limit?, project_root?)` — search declarations in this checkout and
  its installed libraries. `query` is required; `limit` defaults to 10. Normally omit
  `project_root` to use Unity's checkout binding; an explicit override must be an absolute
  project path. Check the returned `index` field: `consulted` confirms that the index was
  searched. If `index` is `unavailable`, an empty `items` list says nothing about whether a
  declaration exists. Use direct source search or another available search tool in that case.
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
local project with `lake build` before submitting a proof commit.

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
